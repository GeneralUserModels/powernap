// ── Data ─────────────────────────────────────────────────
// Replace with your actual data.
const DATA = {
  title: "Weekly Security Report",
  subtitle: "Generated April 3, 2026",
  status: { text: "Resolved", type: "success" }, // success | warning | danger | info
  sections: [
    {
      title: "Summary",
      content: "<p>A brief overview of the report findings and key takeaways.</p>",
      collapsed: false,
    },
    {
      title: "Details",
      content: "<ul><li>Finding one with relevant context.</li><li>Finding two with impact analysis.</li></ul>",
      collapsed: true,
    },
    {
      title: "Recommendations",
      content: "<p>Specific recommendations based on the analysis above.</p>",
      collapsed: true,
    },
  ],
  actions: [
    { id: "review", title: "Review findings", description: "Check the detailed analysis for accuracy.", initialDone: true },
    { id: "config", title: "Update configuration", description: "Apply the recommended changes.", initialDone: false },
    { id: "monitor", title: "Monitor for 24h", description: "Watch metrics after applying changes.", initialDone: false },
  ],
  timeline: [
    { date: "Apr 1", title: "Issue detected", description: "Automated monitoring flagged anomaly." },
    { date: "Apr 2", title: "Investigation", description: "Root cause identified and documented." },
    { date: "Apr 3", title: "Resolution", description: "Fix deployed and verified." },
  ],
};

// ── App ─────────────────────────────────────────────────
// `h` and React hooks are already declared globally by components.js.
const { PageHeader, GlassCard, useDraft, useChecklist, Actions } = PN;

function CollapsibleSection({ sectionId, title, content, initialCollapsed, delay }) {
  // Persist open/closed via useDraft so the user's exploration survives reloads.
  var [collapsed, setCollapsed] = useDraft("section:" + sectionId, !!initialCollapsed);

  return h(GlassCard, { delay: delay },
    h("button", {
      className: "collapsible-toggle",
      onClick: function() { setCollapsed(!collapsed); },
    },
      h("span", { className: "section-title" }, title),
      h("span", { className: "chevron" }, collapsed ? "▶" : "▼")
    ),
    collapsed ? null : h("div", { style: { marginTop: "10px" } },
      h("div", { className: "section-content", dangerouslySetInnerHTML: { __html: content } })
    )
  );
}

function ActionItem({ item, done, onToggle }) {
  return h("div", { className: "action-item", onClick: onToggle, style: { cursor: "pointer" } },
    h("div", { className: "action-check" + (done ? " done" : "") }, done ? "✓" : ""),
    h("div", null,
      h("div", { className: "action-title", style: done ? { textDecoration: "line-through", opacity: 0.6 } : {} }, item.title),
      item.description ? h("div", { className: "action-desc" }, item.description) : null
    )
  );
}

function Timeline({ items }) {
  if (!items || !items.length) return null;
  return h("div", null,
    h("h2", { className: "timeline-header" }, "Timeline"),
    h("div", { className: "timeline" },
      items.map(function(t, i) {
        return h("div", { key: i, className: "timeline-item" },
          h("div", { className: "timeline-dot" }),
          h("div", { className: "timeline-date" }, t.date),
          h("div", { className: "timeline-title" }, t.title),
          t.description ? h("div", { className: "timeline-desc" }, t.description) : null
        );
      })
    )
  );
}

function ReportApp() {
  var checklist = useChecklist("actions", DATA.actions);
  var done = checklist[0];
  var toggle = checklist[1];
  var doneCount = DATA.actions.filter(function(a) { return !!done[a.id]; }).length;
  var [status, setStatus] = useState("");

  async function markComplete() {
    var res = await Actions.markComplete({ reason: "user closed report" });
    setStatus(res.ok ? "Marked complete." : (res.error || "Failed."));
    setTimeout(function() { setStatus(""); }, 2000);
  }

  return h("div", { className: "container" },
    h(PageHeader, { title: DATA.title, subtitle: DATA.subtitle, status: DATA.status }),

    DATA.sections.map(function(s, i) {
      return h(CollapsibleSection, {
        key: s.title,
        sectionId: s.title,
        title: s.title,
        content: s.content,
        initialCollapsed: s.collapsed,
        delay: i * 0.04,
      });
    }),

    DATA.actions && DATA.actions.length ? h("div", null,
      h("h2", { className: "actions-header" }, "Action Items (" + doneCount + "/" + DATA.actions.length + ")"),
      DATA.actions.map(function(a) {
        return h(ActionItem, { key: a.id, item: a, done: !!done[a.id], onToggle: function() { toggle(a.id); } });
      })
    ) : null,

    h(Timeline, { items: DATA.timeline }),

    h("div", { style: { marginTop: 16, display: "flex", alignItems: "center", gap: 10 } },
      h("button", { className: "pill-btn", onClick: markComplete }, "Mark complete"),
      status ? h("span", { className: "card-meta" }, status) : null
    )
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(h(ReportApp));
