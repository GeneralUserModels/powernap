# Moments Smoke Testing

Use this when changing discovery prompts or source ingestion. The goal is to run the real discovery and promotion pipeline on a small, disposable subset of logs, inspect the generated moments, then iterate.

NOTE: When editing prompts, DELETING, rewording, and simplifying is often better than adding MORE constraints.

**IMPORTANT**: make a bunch of candidate draft iterations in parallel; and test those iterations in parallel; and then pick the best; and then keep exploring. This is kind of like a genetic algorithim. It'll help you parallelize.

## What To Test

A useful smoke test should answer:

- Does discovery produce grounded, future-facing moments?
- Are candidates based on the selected log evidence, not random repo context?
- Are source paths useful enough for the executor to follow up?
- Does promotion rank the strongest moments first?
- Did the prompt change reduce the specific failure mode you are targeting?

Do not run experiments against the real `logs-tada` directory. Use a temp copied
log tree so checkpoints and accepted task files are disposable.

## Starter Examples

These are useful classes of moments to look for when choosing a smoke-test slice.
Use them to pick logs and judge outputs, not as verbatim prompt examples to
hardcode.

- The user is researching a set of inference providers because they are choosing
  one or comparing tradeoffs. A good moment might produce a current comparison,
  shortlist, pricing/capability table, or recommendation memo.
- The user is brainstorming interaction ideas for PowerNap, Tada, or a related
  project. A good moment might synthesize patterns, cluster ideas, find adjacent
  systems, or produce a prioritized design brief.
- The user is meeting someone soon and has been searching around them. A good
  moment might research that person, identify relevant work and shared context,
  and prepare meeting-specific talking points.
- The user is meeting with researchers or collaborators. A good moment might pull
  together the most relevant papers, recent work, and open questions before the
  meeting.
- The user is working on a Statement of Work for a grant. A good moment might
  find previous SOW examples, extract structure, and draft an adapted outline.
- An email says an SOW is needed by a deadline and recent browsing/log context
  contains grant details. A good trigger-style moment might gather a relevant
  prior SOW and adapt it to the current grant context.

## Build A Temp Log Slice

This example creates `/tmp/powernap-moments-smoke/logs` from selected rows. Adjust
the row filters and terms for the behavior you are testing.

```bash
rm -rf /tmp/powernap-moments-smoke
mkdir -p /tmp/powernap-moments-smoke/logs

python - <<'PY'
import json
import shutil
from pathlib import Path

src = Path("logs")
dst = Path("/tmp/powernap-moments-smoke/logs")

# Copy compact high-signal context.
for rel in ["chats", "memory"]:
    if (src / rel).exists():
        shutil.copytree(src / rel, dst / rel)

# Keep connector rows likely to contain useful next-needs.
terms = [
    "meeting", "meet", "papers", "paper", "research", "review",
    "grant", "sow", "statement of work", "deadline", "advisor",
    "advising", "powernap", "tada",
]

for rel in [
    "email/filtered.jsonl",
    "notifications/filtered.jsonl",
    "filesys/filtered.jsonl",
    "calendar/filtered.jsonl",
]:
    inp = src / rel
    out = dst / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    kept = []
    if inp.exists():
        for line in inp.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            blob = json.dumps(row, ensure_ascii=False).lower()
            if any(term in blob for term in terms):
                kept.append(line)
    out.write_text("\n".join(kept) + ("\n" if kept else ""))
    print(rel, len(kept))

# Optionally include a known screen window by original line number.
screen_in = src / "screen/filtered.jsonl"
screen_out = dst / "screen/filtered.jsonl"
screen_out.parent.mkdir(parents=True, exist_ok=True)
kept = []
if screen_in.exists():
    for line_no, line in enumerate(screen_in.read_text(errors="replace").splitlines(), start=1):
        if 45122 <= line_no <= 45189:
            kept.append(line)
screen_out.write_text("\n".join(kept) + ("\n" if kept else ""))
print("screen/filtered.jsonl", len(kept))
PY
```

## Seed The Discovery Window

Discovery is incremental. Seed a temp checkpoint so it reads the intended window.

```bash
mkdir -p /tmp/powernap-moments-smoke/logs-tada/_discovery
printf '2026-04-25T00:00:00' > /tmp/powernap-moments-smoke/logs-tada/_discovery/.last_discovery
```

If you want a first-run test, skip this checkpoint and let discovery choose its
default initial window.

## Run Discovery And Promotion

This uses the configured Gemini moments model and API key from `tada-config.json`.
It writes candidates and accepted moments under `/tmp/powernap-moments-smoke/logs-tada`.

```bash
.venv/bin/python - <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path("src").resolve()))

from apps.moments.steps import discover, promote

cfg = json.loads(Path("tada-config.json").read_text())
model = cfg.get("moments_agent_model") or "gemini/gemini-3-flash-preview"
api_key = cfg.get("moments_agent_api_key") or cfg.get("default_llm_api_key") or None
logs = "/tmp/powernap-moments-smoke/logs"

print("running discover", model)
print(discover.run(logs, model=model, api_key=api_key))

print("\nrunning promote", model)
print(promote.run(logs, model=model, api_key=api_key, n=8))
PY
```

## Inspect Candidates

Read the latest candidate JSONL before judging the prompt.

```bash
python - <<'PY'
import json
from pathlib import Path

files = sorted(Path("/tmp/powernap-moments-smoke/logs-tada/_discovery/candidates").glob("*.jsonl"))
latest = files[-1]
print(latest)

for idx, line in enumerate(latest.read_text().splitlines(), start=1):
    c = json.loads(line)
    print("\n---", idx, c["slug"])
    for key in [
        "title",
        "topic",
        "cadence",
        "likely_next_need",
        "desired_artifact",
        "specific_instructions",
        "evidence",
        "source_paths",
        "why_now",
        "user_value",
    ]:
        print(f"{key}: {c.get(key)}")
PY
```

Also inspect accepted markdown:

```bash
find /tmp/powernap-moments-smoke/logs-tada -maxdepth 3 -type f \
  ! -path '*/_discovery/*' \
  ! -path '*/results/*' \
  | sort
```

## Prompt Iteration Loop

Use this loop for prompt work. In most cases, you may need to only really focus on discover.txt

1. Create a temp log slice that contains both positive and negative examples.
2. Run discovery and promotion end to end.
3. Inspect the candidate JSON, not just the summary.
4. Edit the smallest relevant prompt:
   - `prompts/rules/discover.txt` for discovery behavior and rejection rules.
   - `prompts/shared/quality_bar.txt` for broad quality thresholds.
   - `prompts/discover.txt` for ideation task framing.
   - `prompts/discover_compile.txt` for candidate field discipline.
   - `prompts/promote.txt` or `prompts/rules/promote.txt` for ranking.
5. Rerun the exact same temp slice and compare candidate slugs, evidence,
   desired artifacts, and promotion order.
6. Record the attempt in an iteration log with the command, slice definition,
   generated candidates, and judgment.

**Prefer generic prompt rules. Do not encode one-off examples from the smoke slice unless they represent a durable class of failures.**

**Keep iterating repeatedly on prompts**: run over and over again till you're satistifed, recording progress and finalized candidates in an experiment log.

## Quality Rubric

Strong candidates usually have:

- a concrete future need;
- a concrete artifact the executor can produce;
- source paths that point back to activity evidence;
- enough specificity for execution without hardcoding a brittle implementation;
- a clear reason this helps before the user asks.

Reject or revise candidates that:

- summarize what the user already did without adding next-step value;
- cite unrelated code or files as evidence;
- propose work another assistant just completed;
- are generic productivity advice;
- require unavailable private data without a plausible source;
- are so broad the executor cannot finish in one run.
