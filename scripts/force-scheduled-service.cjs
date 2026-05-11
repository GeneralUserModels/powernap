#!/usr/bin/env node

const fs = require("fs");
const path = require("path");

const repoRoot = path.resolve(__dirname, "..");
const logDir = path.resolve(repoRoot, process.env.TADA_LOG_DIR || "logs");
const tadaDir = path.resolve(repoRoot, process.env.TADA_TADA_DIR || "logs-tada");
const oldTimestamp = "2000-01-01T00:00:00";

const targets = {
  memex: [path.join(logDir, "memory", ".last_run")],
  discovery: [path.join(tadaDir, ".last_run")],
};

const requested = process.argv[2] || "all";
const names = requested === "all" ? Object.keys(targets) : [requested];

for (const name of names) {
  const target = targets[name];
  if (!target) {
    console.error(`Unknown target "${name}". Use one of: ${Object.keys(targets).join(", ")}, all`);
    process.exit(1);
  }

  for (const file of target) {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, oldTimestamp);
    console.log(`${name}: wrote ${file}`);
  }
}

console.log("Launch or keep the app running; the next scheduler poll should treat the target as due.");
