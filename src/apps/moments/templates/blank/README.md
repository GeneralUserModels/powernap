# Blank Template

**The default starting point for most tadas.** Minimal scaffold with the Tada design system — pick this whenever the artifact is a single email draft, a paper review, a meeting brief, a decision matrix, a spec, a runbook, a personal letter, or any focused single-purpose surface. The specialized templates (`dashboard/`, `feed/`, `table/`, `report/`) are for the narrower cases their names imply; everything else lives here.

## DATA Schema

```js
const DATA = {
  title: "Moment Title",       // required
  subtitle: "Description",     // optional
};
```

## Composition

Uses `PN.PageHeader` and `PN.GlassCard`. Add any shared or custom components as needed.

## When to Use

- Email drafts, message drafts, letters
- Paper / document reviews (side-by-side source ↔ editable review)
- Briefings for a meeting or new person (sections + editable questions list)
- Comparison / decision matrices (criteria × options table + recommendation)
- Specs, advisories, recaps, runbooks, single-output research summaries
- Anything else where the user is editing or acting on **one artifact**, not browsing a list
