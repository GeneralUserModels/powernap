"""Execute a moment task and persist markdown research pages."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()

from agent.builder import build_agent

RESEARCH_WARNING_ROUND = 60
RESEARCH_MAX_ROUNDS = 90

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
RESEARCH_INSTRUCTION_TEMPLATE = (_PROMPTS / "execute_research.txt").read_text()



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


def _research_ready(research_dir: str) -> bool:
    path = Path(research_dir)
    if not path.is_dir():
        return False
    md_files = _research_pages(path)
    return len(md_files) >= 2 and all(p.read_text().strip() for p in md_files)


def _research_pages(research_dir: Path) -> list[Path]:
    return sorted(
        p for p in research_dir.rglob("*.md")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(research_dir).parts)
    )


def _markdown_title(path: Path) -> str:
    text = path.read_text(errors="replace")
    fm = _parse_frontmatter(text)
    title = (fm.get("title") or "").strip().strip("\"'")
    if title:
        return title
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or path.stem.replace("-", " ").title()
    return path.stem.replace("-", " ").title()


def _markdown_link_target(path: Path) -> str:
    return quote(path.as_posix(), safe="/#")


def _ensure_index_page(research_dir: str, *, title: str, description: str) -> None:
    """Create a quick navigation index if the agent did not write one."""
    root = Path(research_dir)
    index_path = root / "index.md"
    if index_path.exists():
        return
    pages = [p for p in _research_pages(root) if p.name != "index.md"]
    lines = [f"# {title}", ""]
    if description:
        lines.extend([description, ""])
    lines.extend(["## Pages", ""])
    for page in pages:
        rel = page.relative_to(root)
        lines.append(f"- [{_markdown_title(page)}]({_markdown_link_target(rel)})")
    lines.append("")
    index_path.write_text("\n".join(lines))


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
) -> bool:
    """Execute a moment task. Returns True if markdown research pages were produced."""
    task_content = Path(task_path).read_text()
    fm = _parse_frontmatter(task_content)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Back up existing output so we can restore on failure
    backup_dir = str(Path(output_dir).parent / "_backups" / Path(output_dir).name)
    had_previous = _research_ready(str(Path(output_dir) / "research"))
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

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    research_dir = str(Path(output_dir) / "research")
    research_instruction = f"Current date and time: **{now}**\n\n" + RESEARCH_INSTRUCTION_TEMPLATE.format(
        task_content=task_content,
        cadence=effective_cadence,
        schedule=effective_schedule,
        research_dir=research_dir,
        logs_dir=logs_dir,
    ) + feedback_section

    research_agent = _build_agent_for_stage(
        model, logs_dir, output_dir, api_key, subagent_model, subagent_api_key,
        max_rounds=RESEARCH_MAX_ROUNDS, warning_round=RESEARCH_WARNING_ROUND, on_round=on_round,
    )
    research_agent.run([{"role": "user", "content": research_instruction}])

    if not _research_ready(research_dir):
        research_repair_instruction = research_instruction + (
            "\n\n## Required Repair\n\n"
            f"The required research folder is not ready at `{research_dir}`. Your previous attempt did not "
            "write the required markdown files. Do not plan, do not only create directories, and do not build "
            "the website. Use the `write_file` tool now to write at least two substantive non-empty markdown "
            f"files inside `{research_dir}`, verify they exist, and then stop."
        )
        research_agent.run([{"role": "user", "content": research_repair_instruction}])

    if not _research_ready(research_dir):
        print("  [research] FAILED: research markdown files were not written")
        if had_previous:
            _restore_backup(backup_dir, output_dir)
            return True
        _clean_output(output_dir)
        return False

    _ensure_index_page(
        research_dir,
        title=fm.get("title", Path(task_path).stem),
        description=fm.get("description", ""),
    )

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
