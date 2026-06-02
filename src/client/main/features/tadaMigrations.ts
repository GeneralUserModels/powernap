import * as fs from "fs";
import * as path from "path";

export type LegacyMarkdownTadaCleanupStats = {
  scannedResultDirs: number;
  legacyResultDirsDeleted: number;
  taskFilesDeleted: number;
  stateEntriesRemoved: number;
  runHistoryRowsRemoved: number;
  slugsRemoved: string[];
  errors: string[];
};

const RESERVED_TADA_DIRS = new Set(["results", "_backups", "_pre_refine"]);
const MARKDOWN_EXTS = new Set([".md", ".markdown"]);
const HTML_EXTS = new Set([".html", ".htm"]);

function emptyStats(): LegacyMarkdownTadaCleanupStats {
  return {
    scannedResultDirs: 0,
    legacyResultDirsDeleted: 0,
    taskFilesDeleted: 0,
    stateEntriesRemoved: 0,
    runHistoryRowsRemoved: 0,
    slugsRemoved: [],
    errors: [],
  };
}

function safeReadDir(dir: string): fs.Dirent[] {
  try {
    return fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return [];
  }
}

function walkFiles(dir: string): string[] {
  const files: string[] = [];
  for (const entry of safeReadDir(dir)) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...walkFiles(full));
    } else if (entry.isFile()) {
      files.push(full);
    }
  }
  return files;
}

function isLegacyMarkdownResultDir(resultDir: string): boolean {
  const outputDir = path.join(resultDir, "output");
  if (!fs.existsSync(outputDir)) return false;

  let hasMarkdown = false;
  let hasHtml = false;
  for (const file of walkFiles(outputDir)) {
    const ext = path.extname(file).toLowerCase();
    if (MARKDOWN_EXTS.has(ext)) hasMarkdown = true;
    if (HTML_EXTS.has(ext)) hasHtml = true;
  }
  return hasMarkdown && !hasHtml;
}

function isTopicDir(entry: fs.Dirent): boolean {
  return (
    entry.isDirectory() &&
    !entry.name.startsWith("_") &&
    !RESERVED_TADA_DIRS.has(entry.name)
  );
}

function findTaskFiles(tadaDir: string, slug: string): string[] {
  const matches: string[] = [];
  const topLevel = path.join(tadaDir, `${slug}.md`);
  if (fs.existsSync(topLevel)) matches.push(topLevel);

  for (const entry of safeReadDir(tadaDir)) {
    if (!isTopicDir(entry)) continue;
    const candidate = path.join(tadaDir, entry.name, `${slug}.md`);
    if (fs.existsSync(candidate)) matches.push(candidate);
  }

  return matches;
}

function cleanMomentState(resultsDir: string, slugs: Set<string>): number {
  const statePath = path.join(resultsDir, "_moment_state.json");
  if (!fs.existsSync(statePath)) return 0;

  let data: unknown;
  try {
    data = JSON.parse(fs.readFileSync(statePath, "utf-8"));
  } catch {
    return 0;
  }
  if (typeof data !== "object" || data === null || Array.isArray(data)) return 0;

  const state = data as Record<string, unknown>;
  let removed = 0;
  for (const slug of slugs) {
    if (Object.prototype.hasOwnProperty.call(state, slug)) {
      delete state[slug];
      removed += 1;
    }
  }

  if (removed > 0) {
    fs.writeFileSync(statePath, JSON.stringify(state, null, 2));
  }
  return removed;
}

function cleanRunHistory(resultsDir: string, slugs: Set<string>): number {
  const runsPath = path.join(resultsDir, "_runs.jsonl");
  if (!fs.existsSync(runsPath)) return 0;

  const kept: string[] = [];
  let removed = 0;
  for (const line of fs.readFileSync(runsPath, "utf-8").split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const row = JSON.parse(line) as { slug?: unknown };
      if (typeof row.slug === "string" && slugs.has(row.slug)) {
        removed += 1;
        continue;
      }
    } catch {
      // Preserve malformed rows; this cleanup should never corrupt diagnostics.
    }
    kept.push(line);
  }

  if (removed > 0) {
    fs.writeFileSync(runsPath, kept.length > 0 ? `${kept.join("\n")}\n` : "");
  }
  return removed;
}

export function removeLegacyMarkdownTadas(tadaDir: string): LegacyMarkdownTadaCleanupStats {
  const stats = emptyStats();
  const resultsDir = path.join(tadaDir, "results");
  if (!fs.existsSync(resultsDir)) return stats;

  const removedSlugs = new Set<string>();
  for (const entry of safeReadDir(resultsDir)) {
    if (!entry.isDirectory() || entry.name.startsWith("_")) continue;
    stats.scannedResultDirs += 1;

    const resultDir = path.join(resultsDir, entry.name);
    if (!isLegacyMarkdownResultDir(resultDir)) continue;

    try {
      fs.rmSync(resultDir, { recursive: true, force: true });
      stats.legacyResultDirsDeleted += 1;
      removedSlugs.add(entry.name);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      stats.errors.push(`failed to delete result ${entry.name}: ${message}`);
    }
  }

  for (const slug of removedSlugs) {
    for (const taskFile of findTaskFiles(tadaDir, slug)) {
      try {
        fs.unlinkSync(taskFile);
        stats.taskFilesDeleted += 1;
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        stats.errors.push(`failed to delete task ${taskFile}: ${message}`);
      }
    }
  }

  stats.stateEntriesRemoved = cleanMomentState(resultsDir, removedSlugs);
  stats.runHistoryRowsRemoved = cleanRunHistory(resultsDir, removedSlugs);
  stats.slugsRemoved = Array.from(removedSlugs).sort();
  return stats;
}
