// ── Data ─────────────────────────────────────────────────
// Replace with your actual data.
const DATA = {
  title: "Status Dashboard",
  subtitle: "Last updated April 3, 2026",
  stats: [
    { value: "24", label: "Total", highlight: true },
    { value: "18", label: "Active" },
    { value: "3", label: "Pending" },
    { value: "3", label: "Resolved" },
  ],
  filters: [
    { id: "all", label: "All" },
    { id: "active", label: "Active" },
    { id: "pending", label: "Pending" },
    { id: "resolved", label: "Resolved" },
  ],
  items: [
    {
      title: "Sample Item",
      subtitle: "Source or author",
      description: "Description of this item with relevant details.",
      badges: [{ text: "Active", type: "success" }],
      meta: "March 28, 2026",
      filterKey: "active",
    },
    {
      title: "Another Item",
      subtitle: "Another source",
      description: "More details about this item.",
      badges: [{ text: "Pending", type: "warning" }],
      meta: "March 30, 2026",
      filterKey: "pending",
    },
  ],
  summaryEmail: {
    to: "",
    subject: "Status update",
    body: "Quick status check: 18 active, 3 pending, 3 resolved.",
  },
};

// ── App ─────────────────────────────────────────────────
// `h` and React hooks are already declared globally by components.js.
const { PageHeader, StatRow, FilterBar, SearchInput, ResultCount, ItemCard, EmptyState, GlassCard, useFilter, useSearch, useDraft, Actions } = PN;

function DashboardApp() {
  // UI state — ephemeral, no need to persist.
  const [activeFilter, setActiveFilter] = useState("all");
  const [searchQuery, setSearchQuery] = useState("");
  // Editable email surface — persists.
  const [emailTo, setEmailTo] = useDraft("email_to", DATA.summaryEmail.to);
  const [emailBody, setEmailBody] = useDraft("email_body", DATA.summaryEmail.body);
  const [status, setStatus] = useState("");

  const filtered = useFilter(DATA.items, "filterKey", activeFilter);
  const results = useSearch(filtered, ["title", "subtitle", "description"], searchQuery);

  async function sendSummary() {
    const res = await Actions.sendEmail({ to: emailTo, subject: DATA.summaryEmail.subject, body: emailBody });
    setStatus(res.ok ? "Sent." : (res.error === "not_implemented" ? "Email send coming soon." : (res.error || "Failed.")));
    setTimeout(() => setStatus(""), 2500);
  }

  return h("div", { className: "container" },
    h(PageHeader, { title: DATA.title, subtitle: DATA.subtitle }),
    h(StatRow, { stats: DATA.stats }),
    h("div", { className: "controls" },
      h(FilterBar, { filters: DATA.filters, active: activeFilter, onChange: setActiveFilter }),
      h(SearchInput, { value: searchQuery, onChange: setSearchQuery })
    ),
    results.length === 0
      ? h(EmptyState, { message: "No matching items." })
      : h("div", null,
          h(ResultCount, { count: results.length }),
          results.map((item, i) =>
            h(ItemCard, {
              key: i,
              title: item.title,
              subtitle: item.subtitle,
              description: item.description,
              badges: item.badges,
              meta: item.meta,
              url: item.url,
              delay: i * 0.03,
            })
          )
        ),
    // Summary-email surface — editable and sendable.
    h(GlassCard, { style: { marginTop: 16 } },
      h("div", { className: "card-title" }, "Send a status update"),
      h("input", {
        type: "text", value: emailTo, onChange: (e) => setEmailTo(e.target.value),
        placeholder: "to@example.com",
        style: { width: "100%", padding: 8, marginTop: 8, borderRadius: 8, border: "1px solid var(--glass-border)", background: "var(--glass)", color: "var(--text)" },
      }),
      h("textarea", {
        value: emailBody, onChange: (e) => setEmailBody(e.target.value),
        style: { width: "100%", minHeight: 80, padding: 8, marginTop: 8, borderRadius: 8, border: "1px solid var(--glass-border)", background: "var(--glass)", color: "var(--text)" },
      }),
      h("div", { style: { display: "flex", alignItems: "center", gap: 10, marginTop: 10 } },
        h("button", { className: "pill-btn", onClick: sendSummary }, "Send"),
        status ? h("span", { className: "card-meta" }, status) : null
      )
    )
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(h(DashboardApp));
