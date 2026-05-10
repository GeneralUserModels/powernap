# Moments Smoke Testing

Use this when changing discovery prompts or source ingestion. The goal is to run the real discovery and promotion pipeline on a small, disposable subset of logs, inspect the generated moments, then iterate.

NOTE: When editing prompts, DELETING, rewording, and simplifying is often better than adding MORE constraints.

**IMPORTANT**: make a bunch of candidate draft iterations in parallel by making copies of prompts; and test those copies in parallel; and then pick the best; and then keep exploring. This is kind of like a genetic algorithim. It'll help you parallelize.

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

## How To Choose Good Slices

Prefer roughly one day of real activity. A full-day slice is usually better than
a tiny handpicked cluster because it preserves the normal mix of screen activity,
notifications, files, email, calendar events, stale leads, and already-completed
work that discovery must handle in production.

Good smoke slices usually have:

- At least three connector types, usually `screen`, one sparse source
  (`email`, `calendar`, or `notifications`), and one artifact/source-context
  stream such as `filesys`.
- Both positive and negative examples. Include at least one signal that should
  become a useful moment or update, and at least one tempting signal that should
  be rejected, marked weak, or routed as a duplicate.
- Natural noise from the same day. Do not remove nearby unrelated activity just
  because it makes the test harder; unrelated strong signals are how you catch
  bad narrative chaining.
- Prior-art visibility. Copy accepted Tada definitions and relevant completed
  one-offs into the temp tree when the failure mode involves duplicates or
  already-done work.
- A clear expected judgment before running the test: for example, "should produce
  one meeting-prep update and no Cursor-output roadmap" or "should return zero
  candidates from travel receipts unless a new reimbursement request appears."

Good adversarial day slices mix weak and strong signals. Examples of useful
mixtures:

- A completed coding-agent notification, nearby screen debugging, and a real
  project signal. This tests whether discovery proposes active work already being
  handled.
- PDF/download/file events, paper or citation emails, and a visible writing or
  review context. This tests whether downloads alone get inflated into broad
  literature summaries.
- Travel receipts or calendar events, reimbursement emails, and unrelated work
  activity. This tests whether logistics artifacts duplicate existing one-offs or
  get chained into unrelated professional prep.
- A meeting/advising notification, recent browsing about the person or topic,
  and unrelated high-volume screen work. This tests whether the meeting moment
  stays bounded and grounded.
- Existing accepted moments visible in Tada plus fresh adjacent activity. This
  tests whether discovery routes updates instead of minting duplicate moments.

Avoid slices that are too clean:

- Do not test only one connector type unless you are isolating a deterministic
  source-ingestion bug.
- Do not include only rows that mention the target topic. Include adjacent rows
  from the same day so source ranking, merging, and rejection behavior are tested.
- Do not judge from the generated summary alone. Inspect candidate JSON, evidence,
  source paths, and promotion ordering.

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

## Run Discovery Only

Use this when testing the discovery prompt before tuning promotion. It writes
candidate JSONL files but does not accept moments or create promoted markdown.

```bash
.venv/bin/python - <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path("src").resolve()))

from apps.moments.steps import discover

cfg = json.loads(Path("tada-config.json").read_text())
model = cfg.get("moments_agent_model") or "gemini/gemini-3-flash-preview"
api_key = cfg.get("moments_agent_api_key") or cfg.get("default_llm_api_key") or None
logs = "/tmp/powernap-moments-smoke/logs"

print("running discover only", model)
print(discover.run(logs, model=model, api_key=api_key))
PY
```

For this pass, judge only the latest
`logs-tada/_discovery/candidates/*.jsonl`. Good discovery candidates should be
forward-looking and non-repetitive. They should create new leverage such as:

- a meeting brief before the user meets a new person, with recent work, shared
  context, likely agenda, and specific questions;
- a current comparison of products, providers, models, APIs, or tools the user
  appears to be choosing between, with decision criteria and a recommendation;
- a brainstorm document for research directions, experiments, paper trails, or
  project ideas that extends beyond what the user already wrote;
- a planning brief for an upcoming draft, grant, talk, demo, review, or
  milestone, with prior examples, constraints, and open decisions.

Reject candidates that merely summarize past activity, rebuild an artifact the
user already made, or turn old recurring work into another copy of the same
moment.

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

Use this loop for prompt work. Discovery is now flattened into one editable
prompt per discovery phase.

1. Create a temp log slice that contains both positive and negative examples.
2. Run discovery and promotion end to end.
3. Inspect the candidate JSON, not just the summary.
4. Edit the smallest relevant prompt:
   - `prompts/discover.txt` for ideation behavior, source use, quality bar, and examples.
   - `prompts/discover_compile.txt` for candidate field discipline.
   - `prompts/reconcile.txt` for duplicate/update routing after chunk discovery.
   - `prompts/promote.txt` for ranking.
5. Rerun the exact same temp slice and compare candidate slugs, evidence,
   desired artifacts, and promotion order.
6. Record the attempt in an iteration log with the command, slice definition,
   generated candidates, and judgment.

**Prefer generic prompt rules. Do not encode one-off examples from the smoke slice unless they represent a durable class of failures.**

**Keep iterating repeatedly on prompts**: run over and over again till you're satistifed, recording progress and finalized candidates in an experiment log.

## Parallel Failure-Mode Exploration Loop

Use this when a smoke run shows a specific class of bad moments. The goal is to
test several small, generic prompt changes against the same temp slice, compare
the actual discovered moments, then keep only the smallest durable improvement.

1. Name the failure modes from the rubric before writing variants. Useful
   failure-mode targets include:
   - unrelated strong signals getting chained into one narrative;
   - specific venues, deadlines, collaborators, organizations, or deliverables
     being inferred without cited evidence;
   - high-volume screen activity crowding out explicit low-volume commitments;
   - active work already being handled by the user or another assistant being
     proposed as a future moment;
   - broad artifacts that cannot be completed in one executor run.
2. Create prompt-copy variants in a temp directory. Keep each variant focused on
   one failure mode when possible, plus one combined variant to test whether the
   rules interact badly.
3. Run each variant in its own subprocess and its own copied `logs/` directory.
   Discovery prompt text is loaded into module globals, and discovery writes
   checkpoints under `Path(logs_dir).parent / "logs-tada"`, so do not run
   variants in one Python interpreter or one shared run directory.
4. For each run, run discovery and promotion end to end. Promotion order is part
   of the judgment because a prompt can produce a useful candidate that still
   loses to a worse, louder one.
5. Compare candidate JSON across variants, not only run summaries. For each
   candidate, inspect:
   - `title`, `likely_next_need`, and `desired_artifact`;
   - `specific_instructions` for unsupported facts or over-broad scope;
   - `evidence` and `source_paths` for useful follow-up pointers;
   - `why_now` for causal overreach;
   - promotion rank and reason.
6. Pick winners by behavior, not by rule count. If a combined variant becomes
   self-referential, over-constrained, or starts proposing active debugging work,
   prefer the narrower variant even if it misses one secondary opportunity.
7. Record the exact slice, variants, candidates, promotion order, and judgment in
   `src/apps/moments/experiments/`. That directory is intentionally ignored, so
   keep concrete smoke examples there rather than in reusable prompts.

Good generic variants to try:

- **Shared-use test**: Treat each strong signal as independent unless the source
  text itself names the same project, person, deadline, artifact, or decision.
  Before merging signals, check the single future use they all support. If that
  use only exists after inference, do not merge them; keep the strongest signal
  as one idea and mark the others weak or separate.
- **Specific fact discipline**: Only name a venue, deadline, collaborator,
  organization, meeting purpose, or requested deliverable when that exact fact
  appears in the cited activity evidence. If evidence suggests only a general
  upcoming use, keep the candidate generic and phrase unknown specifics as
  verification work for the executor, not as facts.
- **Commitment tie-breaker**: Scan sparse and non-screen sources for explicit
  meetings, deadlines, due dates, promised follow-ups, scheduled events, or
  requests from other people. Test this carefully: it can improve meeting/deadline
  recall, but it can also promote work already being actively handled.

Minimal harness pattern:

```bash
ROUND_ROOT=/tmp/powernap-moments-failure-round
BASE_SLICE=/tmp/powernap-moments-smoke

# For each variant, create:
# $ROUND_ROOT/variants/<name>/overrides.json
# with keys like DISCOVER_TEMPLATE, DISCOVER_COMPILE_TEMPLATE, or RECONCILE_TEMPLATE.

VARIANT_NAME=shared_use ROUND_ROOT="$ROUND_ROOT" BASE_SLICE="$BASE_SLICE" \
.venv/bin/python - <<'PY'
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path("src").resolve()))

from apps.moments.steps import discover, promote

name = os.environ["VARIANT_NAME"]
round_root = Path(os.environ["ROUND_ROOT"])
base = Path(os.environ["BASE_SLICE"])
run_root = round_root / "runs" / name

if run_root.exists():
    shutil.rmtree(run_root)
shutil.copytree(base / "logs", run_root / "logs")

state = run_root / "logs-tada" / "_discovery"
state.mkdir(parents=True, exist_ok=True)
(state / ".last_discovery").write_text("2026-04-25T00:00:00")

for attr, value in json.loads((round_root / "variants" / name / "overrides.json").read_text()).items():
    setattr(discover, attr, value)

cfg = json.loads(Path("tada-config.json").read_text())
model = cfg.get("moments_agent_model") or "gemini/gemini-3-flash-preview"
api_key = cfg.get("moments_agent_api_key") or cfg.get("default_llm_api_key") or None
logs = str(run_root / "logs")

print(discover.run(logs, model=model, api_key=api_key))
print(promote.run(logs, model=model, api_key=api_key, n=8))
PY
```

Run the same harness for multiple variant names in parallel with separate stdout
logs, then compare the latest candidate JSONL under each variant's
`logs-tada/_discovery/candidates/`.

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
