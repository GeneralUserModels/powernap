// ── Data ─────────────────────────────────────────────────
// Replace with your actual data. Each column defines a key + label.
const DATA = {
  title: "Dependency Inventory",
  subtitle: "Scanned April 3, 2026",
  stats: [
    { value: "48", label: "Packages" },
    { value: "3", label: "Outdated" },
    { value: "1", label: "Vulnerable" },
  ],
  columns: [
    { key: "name", label: "Name", sortable: true },
    { key: "version", label: "Version", sortable: true },
    { key: "status", label: "Status", sortable: true },
    { key: "license", label: "License" },
  ],
  rows: [
    { id: "react", name: "react", version: "19.1.0", status: "current", license: "MIT", detail: "No issues found." },
    { id: "lodash", name: "lodash", version: "4.17.20", status: "outdated", license: "MIT", detail: "Latest: 4.17.21. Minor patch." },
    { id: "express", name: "express", version: "4.18.2", status: "vulnerable", license: "MIT", detail: "CVE-2024-1234: path traversal in static middleware." },
  ],
};

// ── App ─────────────────────────────────────────────────
// `h` and React hooks are already declared globally by components.js.
const { PageHeader, StatRow, SearchInput, Badge, ResultCount, GlassCard, useDraft, Actions } = PN;

const STATUS_TYPES = { current: "success", outdated: "warning", vulnerable: "danger" };

function SortableHeader({ columns, sortKey, sortAsc, onSort }) {
  return h("thead", null,
    h("tr", null,
      columns.map(function(c) {
        var sorted = sortKey === c.key;
        var arrow = sorted ? (sortAsc ? "↑" : "↓") : "";
        return h("th", {
          key: c.key,
          className: (c.sortable ? "sortable" : "") + (sorted ? " sorted" : ""),
          onClick: c.sortable ? function() { onSort(c.key); } : undefined,
        }, c.label, c.sortable ? h("span", { className: "sort-arrow" }, arrow) : null);
      })
    )
  );
}

function TableRow({ row, columns, onToggle, expanded, reviewed, onReview }) {
  var cells = columns.map(function(c) {
    var val = row[c.key] || "";
    return h("td", { key: c.key },
      c.key === "status" ? h(Badge, { text: val, type: STATUS_TYPES[val] }) : val
    );
  });
  // One extra column: per-row "reviewed" checkbox (persists via useChecklist parent).
  cells.push(h("td", { key: "_rev" },
    h("input", { type: "checkbox", checked: !!reviewed, onChange: onReview })
  ));

  return h(React.Fragment, null,
    h("tr", {
      className: "data-row",
      style: { cursor: row.detail ? "pointer" : "default" },
      onClick: row.detail ? onToggle : undefined,
    }, cells),
    row.detail ? h("tr", {
      className: "detail-row",
      style: { display: expanded ? "" : "none" },
    }, h("td", { colSpan: columns.length + 1 },
      h("div", { className: "row-detail" }, row.detail)
    )) : null
  );
}

function TableApp() {
  var [sortKey, setSortKey] = useState(null);
  var [sortAsc, setSortAsc] = useState(true);
  var [searchQuery, setSearchQuery] = useState("");
  var [expandedRows, setExpandedRows] = useDraft("expanded_rows", {});
  var [reviewed, setReviewed] = useDraft("reviewed_rows", {});
  var [status, setStatus] = useState("");

  function handleSort(key) {
    if (sortKey === key) { setSortAsc(!sortAsc); }
    else { setSortKey(key); setSortAsc(true); }
  }

  function toggleRow(id) {
    setExpandedRows(function(prev) {
      var next = Object.assign({}, prev);
      next[id] = !next[id];
      return next;
    });
  }

  function toggleReviewed(id) {
    setReviewed(function(prev) {
      var next = Object.assign({}, prev);
      next[id] = !next[id];
      return next;
    });
  }

  // Filter
  var q = searchQuery.toLowerCase();
  var rows = DATA.rows.filter(function(r) {
    return !q || DATA.columns.some(function(c) {
      return String(r[c.key] || "").toLowerCase().includes(q);
    });
  });

  // Sort
  if (sortKey) {
    rows = rows.slice().sort(function(a, b) {
      var va = String(a[sortKey] || ""), vb = String(b[sortKey] || "");
      return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    });
  }

  async function exportCsv() {
    var headers = DATA.columns.map(function(c) { return c.label; }).join(",");
    var lines = DATA.rows.map(function(r) {
      return DATA.columns.map(function(c) {
        return JSON.stringify(String(r[c.key] || ""));
      }).join(",");
    });
    var content = [headers].concat(lines).join("\n");
    var res = await Actions.downloadFile({ filename: "table.csv", content: content, mime: "text/csv" });
    setStatus(res.ok ? "Downloaded." : (res.error || "Failed."));
    setTimeout(function() { setStatus(""); }, 2000);
  }

  var columnsWithRev = DATA.columns.concat([{ key: "_rev", label: "Reviewed" }]);

  return h("div", { className: "container" },
    h(PageHeader, { title: DATA.title, subtitle: DATA.subtitle }),
    h(StatRow, { stats: DATA.stats }),
    h("div", { className: "controls" },
      h(SearchInput, { value: searchQuery, onChange: setSearchQuery }),
      h("button", { className: "pill-btn", onClick: exportCsv }, "Export CSV"),
      status ? h("span", { className: "card-meta" }, status) : null
    ),
    h(GlassCard, { className: "table-wrap" },
      h("table", null,
        h(SortableHeader, { columns: columnsWithRev, sortKey: sortKey, sortAsc: sortAsc, onSort: handleSort }),
        h("tbody", null,
          rows.map(function(r) {
            return h(TableRow, {
              key: r.id,
              row: r,
              columns: DATA.columns,
              expanded: !!expandedRows[r.id],
              onToggle: function() { toggleRow(r.id); },
              reviewed: !!reviewed[r.id],
              onReview: function() { toggleReviewed(r.id); },
            });
          })
        )
      )
    ),
    h(ResultCount, { count: rows.length, total: DATA.rows.length })
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(h(TableApp));
