const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { removeLegacyMarkdownTadas } = require("../dist/main/features/tadaMigrations.js");

function write(file, content) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}

function exists(file) {
  return fs.existsSync(file);
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf-8"));
}

function run() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "tada-migration-"));
  const tada = path.join(root, "logs-tada");
  const results = path.join(tada, "results");

  write(path.join(tada, "research", "old-brief.md"), "---\ntitle: Old\n---\n");
  write(path.join(results, "old-brief", "output", "index.md"), "# Old\n");
  write(path.join(results, "old-brief", "output", "notes.md"), "# Notes\n");
  write(path.join(results, "old-brief", "meta.json"), JSON.stringify({ title: "Old" }));

  write(path.join(tada, "research", "html-brief.md"), "---\ntitle: HTML\n---\n");
  write(path.join(results, "html-brief", "output", "index.html"), "<!doctype html>");
  write(path.join(results, "html-brief", "output", "app.js"), "console.log('ok');\n");
  write(path.join(results, "html-brief", "meta.json"), JSON.stringify({ title: "HTML" }));

  write(path.join(tada, "research", "mixed-brief.md"), "---\ntitle: Mixed\n---\n");
  write(path.join(results, "mixed-brief", "output", "index.html"), "<!doctype html>");
  write(path.join(results, "mixed-brief", "output", "legacy.md"), "# Legacy sidecar\n");
  write(path.join(results, "mixed-brief", "meta.json"), JSON.stringify({ title: "Mixed" }));

  write(path.join(tada, "research", "orphan-task.md"), "---\ntitle: Orphan\n---\n");
  write(path.join(results, "_moment_state.json"), JSON.stringify({
    "old-brief": { pinned: true },
    "html-brief": { pinned: false },
  }, null, 2));
  write(path.join(results, "_runs.jsonl"), [
    JSON.stringify({ slug: "old-brief", status: "success", completed_at: 1 }),
    JSON.stringify({ slug: "html-brief", status: "success", completed_at: 2 }),
    "{malformed",
    "",
  ].join("\n"));

  const first = removeLegacyMarkdownTadas(tada);
  assert.equal(first.legacyResultDirsDeleted, 1);
  assert.equal(first.taskFilesDeleted, 1);
  assert.equal(first.stateEntriesRemoved, 1);
  assert.equal(first.runHistoryRowsRemoved, 1);
  assert.deepEqual(first.slugsRemoved, ["old-brief"]);
  assert.deepEqual(first.errors, []);

  assert.equal(exists(path.join(results, "old-brief")), false);
  assert.equal(exists(path.join(tada, "research", "old-brief.md")), false);
  assert.equal(exists(path.join(results, "html-brief", "output", "index.html")), true);
  assert.equal(exists(path.join(tada, "research", "html-brief.md")), true);
  assert.equal(exists(path.join(results, "mixed-brief", "output", "legacy.md")), true);
  assert.equal(exists(path.join(tada, "research", "mixed-brief.md")), true);
  assert.equal(exists(path.join(tada, "research", "orphan-task.md")), true);

  const state = readJson(path.join(results, "_moment_state.json"));
  assert.equal(Object.hasOwn(state, "old-brief"), false);
  assert.equal(Object.hasOwn(state, "html-brief"), true);

  const runs = fs.readFileSync(path.join(results, "_runs.jsonl"), "utf-8");
  assert.equal(runs.includes("old-brief"), false);
  assert.equal(runs.includes("html-brief"), true);
  assert.equal(runs.includes("{malformed"), true);

  const second = removeLegacyMarkdownTadas(tada);
  assert.equal(second.legacyResultDirsDeleted, 0);
  assert.equal(second.taskFilesDeleted, 0);
  assert.equal(second.stateEntriesRemoved, 0);
  assert.equal(second.runHistoryRowsRemoved, 0);

  fs.rmSync(root, { recursive: true, force: true });
}

run();
console.log("tada migration tests passed");
