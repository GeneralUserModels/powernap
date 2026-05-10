# Memory Wiki Schema

Normal wiki pages are markdown files under this directory, excluding `index.md`, `log.md`, `schema.md`, hidden files, and checkpoint files.

Every normal page must start with YAML frontmatter:

```yaml
---
title: <Page Title>
category: <people|projects|work|interests|life|notes>
confidence: <0.0-1.0>
last_updated: <YYYY-MM-DD>
---
```

Use natural page titles, stable categories, confidence scores, and `[[wiki-links]]` for cross-references. Use inline confidence annotations like `[c:0.7]` for important claims. Update existing pages instead of duplicating facts across multiple pages.

Categories should be broad enough to keep the Memex navigable:
- `people`: collaborators, friends, family, teams, organizations, and relationship pages.
- `projects`: active goals, workstreams, codebases, launches, research efforts, and plans.
- `work`: recurring professional patterns, workflows, decisions, responsibilities, and operating context.
- `interests`: durable topics, tools, hobbies, research themes, and learning areas.
- `life`: personal context, routines, places, logistics, habits, and non-work commitments.
- `notes`: useful memory that is grounded but does not yet fit a stronger category.

`index.md` catalogs pages. `log.md` records dated ingest changes. This file records the conventions the memory ingest agent should follow.
