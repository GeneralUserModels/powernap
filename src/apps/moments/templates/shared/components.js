/**
 * Tada Moments — Shared React Component Library
 *
 * Components live on the global `PN` namespace; React-helper shortcuts
 * (`h`, `useState`, `useCallback`, `useMemo`, `useEffect`) are also
 * exposed as raw globals for convenience in `app.js`.
 *
 * Wrapped in an IIFE so the individual `function PageHeader(...)` /
 * `function GlassCard(...)` declarations stay private to this scope and
 * do not collide with `const { PageHeader, GlassCard } = PN;` in `app.js`.
 *
 * Usage in a template's app.js:
 *   const { PageHeader, GlassCard, Badge, StatRow } = PN;
 *   const App = () => h("div", { className: "container" },
 *     h(PageHeader, { title: DATA.title, subtitle: DATA.subtitle }),
 *     h(StatRow, { stats: DATA.stats }),
 *     h(GlassCard, null, h("p", null, "Hello world"))
 *   );
 *   ReactDOM.createRoot(document.getElementById("root")).render(h(App));
 */

(function () {

const h = React.createElement;
const { useState, useCallback, useMemo, useEffect } = React;

// ── Components ───────────────────────────────────────────

/** Page header with title, subtitle, optional badges and status badge */
function PageHeader({ title, subtitle, badges, status }) {
  return h("header", { style: { marginBottom: "16px" } },
    h("div", { style: { display: "flex", alignItems: "center", gap: "12px", marginBottom: "4px" } },
      h("h1", null, title),
      status ? h("span", {
        className: "badge " + (status.type || ""),
        style: { fontSize: "10px", padding: "3px 10px" }
      }, status.text) : null
    ),
    subtitle ? h("p", { className: "meta" }, subtitle) : null,
    badges && badges.length ? h("div", { style: { display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "8px" } },
      badges.map((b, i) => h(Badge, { key: i, text: typeof b === "string" ? b : b.text, type: typeof b === "string" ? null : b.type }))
    ) : null
  );
}

/** Glass card container with optional animation delay */
function GlassCard({ children, className, style, delay, onClick }) {
  const s = Object.assign({}, delay != null ? { animationDelay: delay + "s" } : {}, style);
  return h("div", {
    className: "glass-card" + (className ? " " + className : ""),
    style: Object.keys(s).length ? s : undefined,
    onClick: onClick
  }, children);
}

/** Single badge pill */
function Badge({ text, type }) {
  return h("span", { className: "badge" + (type ? " " + type : "") }, text);
}

/** Row of badges */
function BadgeRow({ badges }) {
  if (!badges || !badges.length) return null;
  return h("div", { className: "card-badges" },
    badges.map((b, i) => h(Badge, { key: i, text: b.text || b, type: b.type }))
  );
}

/** Stats row with stat pills */
function StatRow({ stats }) {
  if (!stats || !stats.length) return null;
  return h("div", { className: "stat-row" },
    stats.map((s, i) => h(StatPill, { key: i, value: s.value, label: s.label, highlight: s.highlight }))
  );
}

/** Single stat pill */
function StatPill({ value, label, highlight }) {
  return h("div", { className: "stat-pill" + (highlight ? " highlight" : "") },
    h("div", { className: "stat-value" }, value),
    h("div", { className: "stat-label" }, label)
  );
}

/** Controlled search input */
function SearchInput({ value, onChange, placeholder, className, style }) {
  return h("input", {
    type: "text",
    className: "search-input" + (className ? " " + className : ""),
    style: style,
    placeholder: placeholder || "Search...",
    value: value,
    onChange: function(e) { onChange(e.target.value); }
  });
}

/** Filter bar with pill buttons */
function FilterBar({ filters, active, onChange }) {
  return h("div", { style: { display: "flex", gap: "4px", flexWrap: "wrap" } },
    filters.map(function(f) {
      return h("button", {
        key: f.id,
        className: "pill-btn" + (f.id === active ? " active" : ""),
        onClick: function() { onChange(f.id); }
      }, f.label);
    })
  );
}

/** Tab bar with tab buttons */
function TabBar({ tabs, active, onChange }) {
  return h("div", { className: "tab-bar" },
    tabs.map(function(t) {
      return h("button", {
        key: t.id,
        className: "tab-btn" + (t.id === active ? " active" : ""),
        onClick: function() { onChange(t.id); }
      }, t.label + (t.count != null ? " (" + t.count + ")" : ""));
    })
  );
}

/** Content card with title, subtitle, description, badges, meta */
function ItemCard({ title, subtitle, description, badges, meta, url, delay, onClick }) {
  return h(GlassCard, { delay: delay, onClick: onClick },
    h("div", { className: "card-header" },
      h("div", null,
        h("div", { className: "card-title" },
          url ? h("a", { href: url, target: "_blank" }, title) : title
        ),
        subtitle ? h("div", { className: "card-subtitle" }, subtitle) : null
      ),
      badges && badges.length ? h(BadgeRow, { badges: badges }) : null
    ),
    description ? h("div", { className: "card-desc" }, description) : null,
    meta ? h("div", { className: "card-meta" }, meta) : null
  );
}

/** Empty state message */
function EmptyState({ message }) {
  return h("div", { className: "empty" }, message || "No items to display.");
}

/** Result count display */
function ResultCount({ count, total }) {
  var text = total != null
    ? count + " of " + total + " rows"
    : count + " item" + (count !== 1 ? "s" : "");
  return h("div", { className: "result-count" }, text);
}

// ── Hooks ───────────────────────────────────────────────

/** Filter items by a key matching the active filter value. "all" returns everything. */
function useFilter(items, key, activeFilter) {
  return useMemo(function() {
    if (!activeFilter || activeFilter === "all") return items;
    return items.filter(function(item) { return item[key] === activeFilter; });
  }, [items, key, activeFilter]);
}

/** Search items across the given fields by query string. */
function useSearch(items, fields, query) {
  return useMemo(function() {
    if (!query) return items;
    var q = query.toLowerCase();
    return items.filter(function(item) {
      return fields.some(function(f) {
        return String(item[f] || "").toLowerCase().includes(q);
      });
    });
  }, [items, fields, query]);
}

// ── Slug + storage ─────────────────────────────────────────

(function readSlugFromUrl() {
  try {
    var s = new URL(window.location.href).searchParams.get("slug");
    if (s) window.__TADA_MOMENT_SLUG = s;
  } catch (e) {}
})();

function _slug() {
  return window.__TADA_MOMENT_SLUG || "_unscoped";
}

function _storageKey(key) {
  return "tada:moment:" + _slug() + ":" + key;
}

function _readStored(key, initial) {
  try {
    var raw = window.localStorage.getItem(_storageKey(key));
    if (raw == null) return initial;
    return JSON.parse(raw);
  } catch (e) {
    return initial;
  }
}

function _writeStored(key, value) {
  try {
    window.localStorage.setItem(_storageKey(key), JSON.stringify(value));
  } catch (e) {}
}

function _persistDraftPatch(key, value) {
  var patch = {};
  patch[key] = value;
  try {
    _dispatch("saveDraftPatch", { patch: patch });
  } catch (e) {}
}

var _draftRegistry = {};
var _domRestoreScheduled = false;

function _domDraftKey(el) {
  if (!el || !el.tagName) return null;
  if (el.type === "hidden" || el.type === "submit" || el.type === "button" || el.type === "reset") return null;
  var explicit = el.getAttribute("data-pn-draft-key") || el.getAttribute("data-draft-key");
  if (explicit) return "dom:" + explicit;
  var stable = el.getAttribute("name") || el.getAttribute("id") || el.getAttribute("aria-label");
  if (stable) return "dom:" + el.tagName.toLowerCase() + ":" + stable;
  var placeholder = el.getAttribute("placeholder");
  if (placeholder) return "dom:" + el.tagName.toLowerCase() + ":placeholder:" + placeholder;
  var controls = Array.prototype.slice.call(document.querySelectorAll("input, textarea, select"));
  var index = controls.indexOf(el);
  if (index < 0) return null;
  return "dom:" + el.tagName.toLowerCase() + ":index:" + index;
}

function _readDomControlValue(el) {
  if (!el) return null;
  if (el.type === "checkbox") return !!el.checked;
  if (el.type === "radio") return !!el.checked;
  if (el.tagName === "SELECT" && el.multiple) {
    return Array.prototype.slice.call(el.options)
      .filter(function(opt) { return opt.selected; })
      .map(function(opt) { return opt.value; });
  }
  return el.value;
}

function _setNativeValue(el, value) {
  var proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
    : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
    : HTMLInputElement.prototype;
  var desc = Object.getOwnPropertyDescriptor(proto, "value");
  if (desc && desc.set) desc.set.call(el, value);
  else el.value = value;
}

function _writeDomControlValue(el, value) {
  if (!el) return;
  if (el.type === "checkbox" || el.type === "radio") {
    el.checked = !!value;
  } else if (el.tagName === "SELECT" && el.multiple && Array.isArray(value)) {
    Array.prototype.forEach.call(el.options, function(opt) {
      opt.selected = value.indexOf(opt.value) >= 0;
    });
  } else {
    _setNativeValue(el, value == null ? "" : String(value));
  }
  try { el.dispatchEvent(new Event("input", { bubbles: true })); } catch (e) {}
  try { el.dispatchEvent(new Event("change", { bubbles: true })); } catch (e) {}
}

function _restoreDomDraftsFromStorage() {
  var controls = Array.prototype.slice.call(document.querySelectorAll("input, textarea, select"));
  controls.forEach(function(el) {
    var key = _domDraftKey(el);
    if (!key) return;
    var saved = _readStored(key, undefined);
    if (saved !== undefined) _writeDomControlValue(el, saved);
  });
}

function _scheduleDomRestore() {
  if (_domRestoreScheduled) return;
  _domRestoreScheduled = true;
  setTimeout(function() {
    _domRestoreScheduled = false;
    _restoreDomDraftsFromStorage();
  }, 0);
}

function _applyDomDraftPatch(patch) {
  if (!patch || typeof patch !== "object") return;
  var controls = Array.prototype.slice.call(document.querySelectorAll("input, textarea, select"));
  controls.forEach(function(el) {
    var key = _domDraftKey(el);
    if (key && Object.prototype.hasOwnProperty.call(patch, key)) {
      _writeDomControlValue(el, patch[key]);
    }
  });
}

function _registerDraft(key, value, setter) {
  _draftRegistry[key] = { value: value, setter: setter };
}

function _unregisterDraft(key, setter) {
  var current = _draftRegistry[key];
  if (current && current.setter === setter) delete _draftRegistry[key];
}

function _draftSnapshot() {
  var out = {};
  try {
    var prefix = "tada:moment:" + _slug() + ":";
    for (var i = 0; i < window.localStorage.length; i++) {
      var storageKey = window.localStorage.key(i);
      if (!storageKey || storageKey.indexOf(prefix) !== 0) continue;
      var draftKey = storageKey.slice(prefix.length);
      try {
        out[draftKey] = JSON.parse(window.localStorage.getItem(storageKey));
      } catch (e) {}
    }
  } catch (e) {}
  Object.keys(_draftRegistry).forEach(function(key) {
    out[key] = _draftRegistry[key].value;
  });
  return out;
}

function _applyDraftPatch(patch) {
  if (!patch || typeof patch !== "object") return;
  Object.keys(patch).forEach(function(key) {
    var value = patch[key];
    var current = _draftRegistry[key];
    if (current && typeof current.setter === "function") {
      current.setter(value);
    } else {
      _writeStored(key, value);
      _persistDraftPatch(key, value);
    }
  });
  _applyDomDraftPatch(patch);
}

/**
 * useDraft(key, initial) — persists [value, setValue] under
 * `tada:moment:<slug>:<key>`. Edits survive reloads and re-executions.
 */
function useDraft(key, initial) {
  var stored = useMemo(function() { return _readStored(key, initial); }, [key]);
  var pair = useState(stored);
  var value = pair[0];
  var setValue = pair[1];
  var setter = useCallback(function(next) {
    setValue(function(prev) {
      var resolved = typeof next === "function" ? next(prev) : next;
      _writeStored(key, resolved);
      _persistDraftPatch(key, resolved);
      return resolved;
    });
  }, [key]);
  useEffect(function() {
    _registerDraft(key, value, setter);
    return function() { _unregisterDraft(key, setter); };
  }, [key, value, setter]);
  return [value, setter];
}

/**
 * useChecklist(key, items) → [state, toggle, reset]
 *   state: { [id]: bool }
 *   toggle(id): flip one
 *   reset(): clear all
 * items is [{ id, label, initialDone? }]
 */
function useChecklist(key, items) {
  var initial = useMemo(function() {
    var seed = {};
    (items || []).forEach(function(it) { seed[it.id] = !!it.initialDone; });
    return seed;
  }, [items]);
  var pair = useDraft(key, initial);
  var state = pair[0];
  var setState = pair[1];
  var toggle = useCallback(function(id) {
    setState(function(prev) {
      var next = Object.assign({}, prev);
      next[id] = !prev[id];
      return next;
    });
  }, [setState]);
  var reset = useCallback(function() { setState(initial); }, [setState, initial]);
  return [state, toggle, reset];
}

// ── Outbound actions (postMessage to parent renderer) ──────

var _pendingActions = {};

window.addEventListener("message", function(event) {
  var d = event.data;
  if (!d || d.source !== "tada-host") return;
  if (d.type === "getDraftSnapshot" && d.nonce) {
    try {
      window.parent.postMessage(
        { source: "tada-moment", nonce: d.nonce, ok: true, payload: { drafts: _draftSnapshot() } },
        "*"
      );
    } catch (e) {}
    return;
  }
  if (d.type === "applyDraftPatch" && d.nonce) {
    try {
      _applyDraftPatch(d.payload && d.payload.patch);
      window.parent.postMessage(
        { source: "tada-moment", nonce: d.nonce, ok: true },
        "*"
      );
    } catch (e) {
      window.parent.postMessage(
        { source: "tada-moment", nonce: d.nonce, ok: false, error: String(e && e.message || e) },
        "*"
      );
    }
    return;
  }
  var pending = _pendingActions[d.nonce];
  if (!pending) return;
  delete _pendingActions[d.nonce];
  pending(d);
});

function _dispatch(type, payload) {
  var nonce = String(Date.now()) + "-" + Math.random().toString(36).slice(2, 10);
  return new Promise(function(resolve) {
    _pendingActions[nonce] = function(reply) {
      resolve({ ok: !!reply.ok, error: reply.error });
    };
    try {
      window.parent.postMessage(
        { source: "tada-moment", type: type, nonce: nonce, payload: payload },
        "*"
      );
    } catch (e) {
      delete _pendingActions[nonce];
      resolve({ ok: false, error: String(e && e.message || e) });
    }
    // Hard fallback: if no reply in 10s, resolve as not_implemented.
    setTimeout(function() {
      if (_pendingActions[nonce]) {
        delete _pendingActions[nonce];
        resolve({ ok: false, error: "no_response" });
      }
    }, 10000);
  });
}

var Actions = Object.freeze({
  sendEmail: function(payload) { return _dispatch("sendEmail", payload); },
  addCalendarEvent: function(payload) { return _dispatch("addCalendarEvent", payload); },
  saveToMemory: function(payload) { return _dispatch("saveToMemory", payload); },
  markComplete: function(payload) { return _dispatch("markComplete", payload || {}); },
  openExternal: function(url) { return _dispatch("openExternal", { url: url }); },
  downloadFile: function(payload) { return _dispatch("downloadFile", payload); },
  copyToClipboard: function(text) { return _dispatch("copyToClipboard", { text: text }); },
});

document.addEventListener("input", function(event) {
  var el = event.target;
  if (!el || !el.matches || !el.matches("input, textarea, select")) return;
  var key = _domDraftKey(el);
  if (!key) return;
  var value = _readDomControlValue(el);
  _writeStored(key, value);
  _persistDraftPatch(key, value);
}, true);

document.addEventListener("change", function(event) {
  var el = event.target;
  if (!el || !el.matches || !el.matches("input, textarea, select")) return;
  var key = _domDraftKey(el);
  if (!key) return;
  var value = _readDomControlValue(el);
  _writeStored(key, value);
  _persistDraftPatch(key, value);
}, true);

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", _scheduleDomRestore);
} else {
  _scheduleDomRestore();
}

try {
  new MutationObserver(_scheduleDomRestore).observe(document.documentElement, {
    childList: true,
    subtree: true,
  });
} catch (e) {}

// ── Export ───────────────────────────────────────────────

window.PN = {
  // Components
  PageHeader: PageHeader,
  GlassCard: GlassCard,
  Badge: Badge,
  BadgeRow: BadgeRow,
  StatRow: StatRow,
  StatPill: StatPill,
  SearchInput: SearchInput,
  FilterBar: FilterBar,
  TabBar: TabBar,
  ItemCard: ItemCard,
  EmptyState: EmptyState,
  ResultCount: ResultCount,
  // Hooks
  useFilter: useFilter,
  useSearch: useSearch,
  useDraft: useDraft,
  useChecklist: useChecklist,
  // Outbound actions (postMessage → parent)
  Actions: Actions,
};

// Convenience globals for app.js — these are not on PN by design; app.js
// just calls `h(...)`, `useState(...)`, etc. directly. Anything component-
// or hook-shaped stays under `PN.*` to avoid global collisions when the
// agent destructures from PN.
window.h = h;
window.useState = useState;
window.useCallback = useCallback;
window.useMemo = useMemo;
window.useEffect = useEffect;

})();
