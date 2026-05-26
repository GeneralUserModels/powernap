// ── Data ─────────────────────────────────────────────────
// Replace with your actual data. Each tab contains a list of items.
const DATA = {
  title: "Daily Research Digest",
  subtitle: "Updated April 3, 2026",
  tags: ["Machine Learning", "Reasoning", "Agents"],
  stats: [
    { value: "12", label: "Papers" },
    { value: "8", label: "Threads" },
    { value: "3", label: "Posts" },
  ],
  tabs: [
    {
      id: "papers", label: "Papers",
      items: [
        {
          id: "p1",
          title: "Sample Paper Title",
          url: "https://arxiv.org/abs/2403.00000",
          meta: "Chen et al. — arXiv, Mar 2026",
          summary: "A brief summary of what this paper covers and why it is relevant.",
          score: 9,
          tags: ["reasoning", "agents"],
        },
        {
          id: "p2",
          title: "Another Paper Title",
          url: "https://arxiv.org/abs/2403.00001",
          meta: "Smith et al. — NeurIPS 2026",
          summary: "Another summary describing the contribution.",
          score: 7,
          tags: ["training"],
        },
      ],
    },
    {
      id: "threads", label: "Threads",
      items: [
        {
          id: "t1",
          title: "@researcher — Interesting findings on...",
          url: "https://x.com/researcher/status/123",
          meta: "2h ago — 1.2k likes",
          summary: "Key takeaway from this thread about recent developments.",
          score: 8,
          tags: ["discussion"],
        },
      ],
    },
  ],
};

// ── App ─────────────────────────────────────────────────
// `h` and React hooks are already declared globally by components.js.
const { PageHeader, StatRow, TabBar, GlassCard, EmptyState, useDraft, Actions } = PN;

function ScoreCircle({ score }) {
  if (score == null) return null;
  return h("div", { className: "score" + (score >= 8 ? " high" : "") }, score);
}

function FeedCard({ item, delay, saved, onSave, onOpen }) {
  return h(GlassCard, { delay: delay },
    h("div", { className: "card-header" },
      h("div", null,
        h("div", { className: "card-title" },
          item.url ? h("a", { href: "#", onClick: (e) => { e.preventDefault(); onOpen(item.url); } }, item.title) : item.title
        ),
        h("div", { className: "card-meta" }, item.meta || "")
      ),
      h(ScoreCircle, { score: item.score })
    ),
    item.summary ? h("div", { className: "card-summary" }, item.summary) : null,
    item.tags && item.tags.length ? h("div", { className: "card-tags" },
      item.tags.map(function(t, i) {
        return h("span", { key: i, className: "card-tag" }, t);
      })
    ) : null,
    h("div", { style: { display: "flex", gap: 8, marginTop: 10 } },
      h("button", { className: "pill-btn" + (saved ? " active" : ""), onClick: onSave }, saved ? "Saved" : "Save")
    )
  );
}

function FeedApp() {
  var [activeTab, setActiveTab] = useState(DATA.tabs[0] ? DATA.tabs[0].id : null);
  var [saved, setSaved] = useDraft("saved_items", {});

  function toggleSaved(id) {
    setSaved(function(prev) {
      var next = Object.assign({}, prev);
      next[id] = !next[id];
      return next;
    });
  }

  async function openUrl(url) {
    var res = await Actions.openExternal(url);
    if (!res.ok) window.open(url, "_blank");
  }

  var tabs = DATA.tabs.map(function(t) {
    return { id: t.id, label: t.label, count: t.items.length };
  });

  var currentTab = DATA.tabs.find(function(t) { return t.id === activeTab; });
  var items = currentTab ? currentTab.items : [];

  return h("div", { className: "container" },
    h(PageHeader, { title: DATA.title, subtitle: DATA.subtitle, badges: DATA.tags }),
    h(StatRow, { stats: DATA.stats }),
    h(TabBar, { tabs: tabs, active: activeTab, onChange: setActiveTab }),
    items.length === 0
      ? h(EmptyState, { message: "No items yet." })
      : items.map(function(item, i) {
          return h(FeedCard, {
            key: item.id || i,
            item: item,
            delay: i * 0.04,
            saved: !!saved[item.id],
            onSave: function() { toggleSaved(item.id); },
            onOpen: openUrl,
          });
        })
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(h(FeedApp));
