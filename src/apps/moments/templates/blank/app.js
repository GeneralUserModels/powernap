// ── Data ─────────────────────────────────────────────────
// Replace this object with your actual data.
const DATA = {
  title: "Moment Title",
  subtitle: "A brief description of what this moment shows",
  initialNote: "",
};

// ── App ─────────────────────────────────────────────────
// `h` and React hooks are already declared globally by components.js.
const { PageHeader, GlassCard, useDraft, Actions } = PN;

function BlankApp() {
  // Editable surface — persists across reloads via PN.useDraft.
  const [note, setNote] = useDraft("note", DATA.initialNote);
  const [status, setStatus] = React.useState("");

  async function copyNote() {
    const res = await Actions.copyToClipboard(note);
    setStatus(res.ok ? "Copied." : (res.error || "Failed."));
    setTimeout(() => setStatus(""), 2000);
  }

  return h("div", { className: "container" },
    h(PageHeader, { title: DATA.title, subtitle: DATA.subtitle }),
    h(GlassCard, { style: { marginTop: "16px" } },
      h("p", { className: "card-desc" }, "Your notes (saved automatically):"),
      h("textarea", {
        value: note,
        onChange: (e) => setNote(e.target.value),
        placeholder: "Type here…",
        style: { width: "100%", minHeight: 120, padding: 10, borderRadius: 8, border: "1px solid var(--glass-border)", background: "var(--glass)", color: "var(--text)" },
      }),
      h("div", { style: { display: "flex", alignItems: "center", gap: 10, marginTop: 10 } },
        h("button", { className: "pill-btn", onClick: copyNote }, "Copy"),
        status ? h("span", { className: "card-meta" }, status) : null
      )
    )
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(h(BlankApp));
