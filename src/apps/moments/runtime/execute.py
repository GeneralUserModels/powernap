"""Execute a moment task and persist a mini-web-app output."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

# str.format() collides hard with this prompt — execute_research.txt
# documents a React-style component API full of literal `{ name, ... }`
# JSX-ish syntax. Doubling every brace in the file is fragile; instead we
# substitute only the named placeholders we control and leave every other
# `{...}` alone.
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _render_template(template: str, **values: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return str(values[key])
        return match.group(0)
    return _PLACEHOLDER_RE.sub(_sub, template)

from dotenv import load_dotenv

load_dotenv()

from agent.builder import build_agent
from agent.cli_backends import (
    CliBackendConfig,
    cli_footer,
    run_stage_via_cli,
    strip_tool_plumbing,
)

RESEARCH_WARNING_ROUND = 100
RESEARCH_MAX_ROUNDS = 150
OUTPUT_SUBDIR = "output"

OUTPUT_FILES = ["index.html", "styles.css", "app.js", "data.js", "base.css", "components.js", "meta.json"]
# Core files the runner expects to see before treating the agent's mini-app as
# "ready" and sweeping the subprocess. `data.js` is optional (only when JS data
# is split out); `meta.json` is written by the Python runtime after the agent
# exits.
EXPECTED_APP_FILES = ["index.html", "styles.css", "app.js", "base.css", "components.js"]


def _template_app_js_bytes() -> list[bytes]:
    """Cache template app.js bytes used to detect unmodified scaffolds."""
    out: list[bytes] = []
    for tdir in TEMPLATES_DIR.iterdir():
        if not tdir.is_dir() or tdir.name == "shared":
            continue
        app = tdir / "app.js"
        if app.is_file():
            out.append(app.read_bytes())
    return out


def _make_outputs_ready_check(pages_dir: Path):
    """Predicate: all expected files non-empty AND app.js is not a verbatim
    copy of any starter template. Agents always `cp templates/<x>/app.js`
    first; without the byte check the runner SIGTERMs before the agent
    rewrites it and ships the placeholder ("Moment Title") as the tada.
    """
    templates = _template_app_js_bytes()
    def _check() -> bool:
        for name in EXPECTED_APP_FILES:
            p = pages_dir / name
            if not p.is_file() or p.stat().st_size == 0:
                return False
        try:
            app_bytes = (pages_dir / "app.js").read_bytes()
        except OSError:
            return False
        return all(app_bytes != t for t in templates)
    return _check

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
OUTPUT_INSTRUCTION_TEMPLATE = (_PROMPTS / "execute_research.txt").read_text()



def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from markdown content."""
    if not content.startswith("---"):
        return {}
    try:
        end = content.index("---", 3)
    except ValueError:
        return {}
    result = {}
    for line in content[3:end].strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def _clear_generated_output(output_dir: str) -> None:
    """Remove prior generated artifacts while preserving feedback files."""
    out = Path(output_dir)
    for path in out.iterdir():
        if path.is_file() and path.name.startswith("feedback_") and path.suffix == ".md":
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _restore_backup(backup_dir: str, output_dir: str) -> None:
    """Replace output_dir contents with backup."""
    print(f"  [safety] restoring previous version from backup")
    shutil.rmtree(output_dir)
    shutil.move(backup_dir, output_dir)


def _clean_output(output_dir: str) -> None:
    """Remove all files from output_dir (first-ever run failed)."""
    print(f"  [safety] removing failed output (no previous version)")
    shutil.rmtree(output_dir)


def _cleanup_backup(backup_dir: str) -> None:
    """Remove backup after successful generation."""
    if Path(backup_dir).exists():
        shutil.rmtree(backup_dir)


def _output_ready(output_pages_dir: str) -> bool:
    index = Path(output_pages_dir) / "index.html"
    return index.is_file() and index.read_text(errors="replace").strip() != ""


def _build_agent_for_stage(
    model: str,
    logs_dir: str,
    output_dir: str,
    api_key: str | None,
    subagent_model: str | None,
    subagent_api_key: str | None,
    *,
    max_rounds: int,
    warning_round: int,
    on_round=None,
):
    agent, _ = build_agent(
        model, output_dir, api_key=api_key,
        subagent_model=subagent_model, subagent_api_key=subagent_api_key,
    )
    agent.max_rounds = max_rounds
    agent.warning_round = warning_round
    agent.on_round = on_round
    return agent


def _run_execute_via_cli(
    *,
    output_instruction: str,
    output_dir: str,
    logs_dir: str,
    cli_config: CliBackendConfig,
    on_round=None,
    label: str = "execute",
) -> None:
    out_path = Path(output_dir)
    log_dir = out_path / ".cli"
    log_dir.mkdir(parents=True, exist_ok=True)
    pages_dir = out_path / OUTPUT_SUBDIR
    prompt = strip_tool_plumbing(output_instruction) + cli_footer(
        stage="execute",
        cwd=out_path,
        output_dir=pages_dir,
    )
    run_stage_via_cli(
        stage="execute",
        config=cli_config,
        prompt=prompt,
        cwd=out_path,
        log_dir=log_dir,
        label=label,
        # Once every core mini-app file exists and is non-empty, the runner's
        # poll terminates the subprocess after a short grace period — saves us
        # from agents that keep "verifying" after the app is on disk. Requires
        # all five files (not just index.html) so the agent has a chance to
        # finish writing CSS/JS before being swept.
        expected_outputs=[pages_dir / name for name in EXPECTED_APP_FILES],
        # The agent's first move is `cp templates/<x>/{index.html,app.js,...}`,
        # which makes every expected file exist instantly with placeholder
        # ("Moment Title") content. Hold off on the ready-grace until app.js
        # diverges from the template — otherwise we SIGTERM mid-patch.
        outputs_ready_check=_make_outputs_ready_check(pages_dir),
        # Reads need access to the logs dir; writes stay confined to the
        # output dir via codex's workspace-write sandbox / claude's --cd.
        add_dirs=[Path(logs_dir).resolve()],
        on_round=on_round,
    )


def run(
    task_path: str,
    output_dir: str,
    logs_dir: str,
    model: str,
    cadence_override: str | None = None,
    schedule_override: str | None = None,
    api_key: str | None = None,
    last_run_at: float | None = None,
    on_round=None,
    subagent_model: str | None = None,
    subagent_api_key: str | None = None,
    cli_config: CliBackendConfig | None = None,
) -> bool:
    """Execute a moment task. Returns True if markdown output pages were produced."""
    task_content = Path(task_path).read_text()
    fm = _parse_frontmatter(task_content)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Back up existing output so we can restore on failure
    backup_dir = str(Path(output_dir).parent / "_backups" / Path(output_dir).name)
    had_previous = _output_ready(str(Path(output_dir) / OUTPUT_SUBDIR))
    if had_previous:
        if Path(backup_dir).exists():
            shutil.rmtree(backup_dir)
        Path(backup_dir).parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(output_dir, backup_dir)
    _clear_generated_output(output_dir)

    effective_cadence = cadence_override or fm.get("cadence", "")
    effective_schedule = schedule_override or fm.get("schedule", "")

    # Read user feedback files and state (thumbs, dismissed, etc.)
    feedback_section = ""
    slug = Path(output_dir).name
    tada_dir = Path(output_dir).parent.parent
    state_path = tada_dir / "results" / "_moment_state.json"
    if state_path.exists():
        all_state = json.loads(state_path.read_text())
        slug_state = all_state.get(slug, {})
        thumbs = slug_state.get("thumbs")
        if thumbs:
            feedback_section += f"\n\n## User Rating\n\nThe user gave this moment a **thumbs {thumbs}**."

    feedback_files = sorted(Path(output_dir).glob("feedback_*.md"))
    if feedback_files:
        parts = []
        for f in feedback_files:
            parts.append(f"### {f.stem}\n\n{f.read_text()}")
        feedback_section += (
            "\n\n## User Feedback\n\n"
            "The user has provided feedback on previous versions of this moment. Incorporate this feedback "
            "into your output — address their concerns, adjust the content or presentation accordingly.\n\n"
            + "\n\n".join(parts)
        )

    # If a previous version exists, show the agent the prior app.js + meta so
    # it preserves the chosen template, `PN.useDraft` keys, and overall layout
    # — and updates content + surfaces only. Without this the agent picks a
    # different template or renames draft keys and silently invalidates the
    # user's persisted edits.
    previous_section = ""
    if had_previous:
        prev_app = Path(backup_dir) / OUTPUT_SUBDIR / "app.js"
        prev_meta = Path(backup_dir) / "meta.json"
        prev_app_text = prev_app.read_text(errors="replace") if prev_app.exists() else ""
        prev_meta_text = prev_meta.read_text(errors="replace") if prev_meta.exists() else ""
        if prev_app_text:
            previous_section = (
                "\n\n## Previous Version\n\n"
                "This moment was generated before. Preserve the template choice, the structure of `app.js`, "
                "and every `PN.useDraft` / `PN.useChecklist` key from the previous version — the user's "
                "in-app edits are keyed off those names in localStorage and silently break if the keys "
                "change. Update the `DATA` blob and add/adjust surfaces as needed for new evidence; do not "
                "restructure the layout, rename keys, switch templates, or rewrite handlers unless the new "
                "task content materially demands it.\n\n"
                f"### Previous `meta.json`\n\n```json\n{prev_meta_text.strip()}\n```\n\n"
                f"### Previous `app.js`\n\n```js\n{prev_app_text}\n```"
            )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    output_pages_dir = str(Path(output_dir) / OUTPUT_SUBDIR)
    output_instruction = f"Current date and time: **{now}**\n\n" + _render_template(
        OUTPUT_INSTRUCTION_TEMPLATE,
        task_content=task_content,
        cadence=effective_cadence,
        schedule=effective_schedule,
        output_dir=output_pages_dir,
        logs_dir=logs_dir,
        templates_dir=str(TEMPLATES_DIR),
    ) + feedback_section + previous_section

    repair_instruction = output_instruction + (
        "\n\n## Required Repair\n\n"
        f"The required mini-web-app is not ready at `{output_pages_dir}`. Your previous attempt did not "
        f"write `index.html`. Pick a template from `{TEMPLATES_DIR}/`, copy its files plus "
        "`shared/base.css` and `shared/components.js` into the output directory, populate `app.js` "
        f"with the task content, and write `{output_pages_dir}/index.html`. Then stop."
    )

    if cli_config is None:
        output_agent = _build_agent_for_stage(
            model, logs_dir, output_dir, api_key, subagent_model, subagent_api_key,
            max_rounds=RESEARCH_MAX_ROUNDS, warning_round=RESEARCH_WARNING_ROUND, on_round=on_round,
        )
        output_agent.run([{"role": "user", "content": output_instruction}])

        if not _output_ready(output_pages_dir):
            output_agent.run([{"role": "user", "content": repair_instruction}])
    else:
        _run_execute_via_cli(
            output_instruction=output_instruction,
            output_dir=output_dir,
            logs_dir=logs_dir,
            cli_config=cli_config,
            on_round=on_round,
            label=f"execute_{slug}",
        )

        if not _output_ready(output_pages_dir):
            _run_execute_via_cli(
                output_instruction=repair_instruction,
                output_dir=output_dir,
                logs_dir=logs_dir,
                cli_config=cli_config,
                on_round=on_round,
                label=f"execute_{slug}_repair",
            )

    if not _output_ready(output_pages_dir):
        print(f"  [output] FAILED: index.html was not written to {output_pages_dir}")
        if had_previous:
            _restore_backup(backup_dir, output_dir)
            return True
        _clean_output(output_dir)
        return False

    meta_path = Path(output_dir) / "meta.json"
    meta_path.write_text(json.dumps({
        "title": fm.get("title", Path(task_path).stem),
        "description": fm.get("description", ""),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "cadence": effective_cadence,
        "schedule": effective_schedule,
    }, indent=2))

    if feedback_files:
        from apps.moments.core.state import load_state, save_state
        all_state = load_state(tada_dir)
        entry = {**all_state.get(slug, {})}
        entry["last_feedback_incorporated_at"] = datetime.now(timezone.utc).isoformat()
        all_state[slug] = entry
        save_state(tada_dir, all_state)

    _cleanup_backup(backup_dir)
    return True
