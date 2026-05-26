"""Lint and maintain the personal knowledge wiki."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agent.builder import build_agent
from agent.cli_backends import (
    CliBackendConfig,
    cli_footer,
    run_stage_via_cli,
    strip_tool_plumbing,
)


LINT_TEMPLATE = (Path(__file__).parent / "prompts" / "lint.txt").read_text()


def run(
    logs_dir: str,
    model: str,
    api_key: str | None = None,
    on_round=None,
    subagent_model: str | None = None,
    subagent_api_key: str | None = None,
    cli_config: CliBackendConfig | None = None,
) -> str:
    logs_path = Path(logs_dir).resolve()
    memory_dir = logs_path / "memory"

    if not memory_dir.exists():
        return "Wiki directory does not exist yet. Run ingest first."

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    instruction = f"Current date and time: **{now}**\n\n" + LINT_TEMPLATE.format(
        memory_dir=str(memory_dir),
    )

    if cli_config is None:
        agent, _ = build_agent(
            model, str(logs_path), api_key=api_key,
            subagent_model=subagent_model, subagent_api_key=subagent_api_key,
        )
        agent.max_rounds = 100
        agent.on_round = on_round
        return agent.run([{"role": "user", "content": instruction}])

    # CLI variant: the agent edits wiki pages in place; we capture a free-form
    # report so the worker has something to log/return.
    out_dir = memory_dir / ".cli"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"lint_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    extra = (
        f"When you finish, write a short markdown summary of what you fixed "
        f"(broken links, archived pages, decayed confidences, merges, "
        f"verifications) to this exact path: `{report_path}`."
    )
    prompt = strip_tool_plumbing(instruction) + cli_footer(
        stage="memory_lint",
        cwd=memory_dir,
        output_paths=[report_path],
        extra_notes=extra,
    )
    run_stage_via_cli(
        stage="memory_lint",
        config=cli_config,
        prompt=prompt,
        cwd=memory_dir,
        log_dir=out_dir,
        label="memory_lint",
        expected_outputs=[report_path],
        on_round=on_round,
    )
    return report_path.read_text()


if __name__ == "__main__":
    import json
    import logging

    from server.config import CONFIG_PATH
    from server.cost_tracker import init_cost_tracking

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Lint and maintain the personal knowledge wiki")
    parser.add_argument("logs_dir", help="Path to the logs directory")
    parser.add_argument("-m", "--model", default=None)
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args()

    tracker = init_cost_tracking()

    config = json.loads(CONFIG_PATH.read_text())
    model = args.model or config["moments_agent_model"]
    api_key = args.api_key or config.get("moments_agent_api_key") or config.get("default_llm_api_key")

    result = run(args.logs_dir, model=model, api_key=api_key)
    print(result)

    snapshot, elapsed = tracker.snapshot()
    total_cost = sum(s["cost"] for s in snapshot.values())
    total_tokens = sum(s["input_tokens"] + s["output_tokens"] for s in snapshot.values())
    logging.getLogger(__name__).info(
        "[cost] lint finished — $%.4f total, %d tokens, %.0fs", total_cost, total_tokens, elapsed
    )
