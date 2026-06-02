import React, { useState } from "react";
import type { AgentBackendInfo } from "../../api/client";
import {
  installAgentBackend,
  loginAgentBackend,
  logoutAgentBackend,
  getAgentBackendStatus,
} from "../../api/client";

type Props = {
  backend: "codex" | "claude_code";
  label: string;
  info: AgentBackendInfo | undefined;
  onRefresh: () => Promise<void> | void;
};

type Tone = "ok" | "warn" | "err" | "neutral";

function statusTone(info?: AgentBackendInfo): Tone {
  if (!info) return "neutral";
  if (!info.available) return "err";
  if (info.auth === "oauth") return "ok";
  return "warn";
}

function statusLabel(info?: AgentBackendInfo): string {
  if (!info) return "Checking…";
  if (!info.available) return "Not installed";
  if (info.auth === "oauth") return "Signed in";
  return "Not signed in";
}

const DOT_COLOR: Record<Tone, string> = {
  ok: "var(--sage, #84B179)",
  warn: "#d59d3a",
  err: "var(--danger, #c45a5a)",
  neutral: "var(--text-tertiary, #9BA896)",
};

function StatusDot({ tone }: { tone: Tone }) {
  return (
    <span
      style={{
        display: "inline-block",
        width: 8,
        height: 8,
        borderRadius: "50%",
        background: DOT_COLOR[tone],
        flexShrink: 0,
      }}
    />
  );
}

export function AgentBackendStatusPanel({ backend, label, info, onRefresh }: Props) {
  const [busy, setBusy] = useState<"install" | "login" | "logout" | null>(null);
  const [message, setMessage] = useState<string>("");

  const handleInstall = async () => {
    setBusy("install");
    setMessage("Installing…");
    try {
      const result = await installAgentBackend(backend);
      setMessage(result.ok ? "Installed." : `Install failed: ${result.reason ?? "see logs"}`);
      await onRefresh();
    } finally {
      setBusy(null);
    }
  };

  const handleLogin = async () => {
    setBusy("login");
    setMessage("Opening browser…");
    try {
      const result = await loginAgentBackend(backend);
      setMessage(result.detail);
      for (let i = 0; i < 30; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        const fresh = await getAgentBackendStatus();
        if (fresh.backends[backend]?.auth === "oauth") break;
      }
      await onRefresh();
    } finally {
      setBusy(null);
    }
  };

  const handleLogout = async () => {
    setBusy("logout");
    setMessage("Signing out…");
    try {
      await logoutAgentBackend(backend);
      setMessage("");
      await onRefresh();
    } finally {
      setBusy(null);
    }
  };

  const tone = statusTone(info);
  const showInstall = info && !info.available;
  const showLogin = !!info?.available && info.auth !== "oauth";
  const showLogout = !!info?.available && info.auth === "oauth";

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 6,
        padding: "10px 12px",
        borderRadius: 8,
        background: "rgba(155, 168, 150, 0.06)",
        border: "1px solid rgba(132, 177, 121, 0.12)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <StatusDot tone={tone} />
        <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--text)" }}>{label}</span>
        <span style={{ fontSize: 11.5, color: "var(--text-tertiary)" }}>{statusLabel(info)}</span>
        <div style={{ flex: 1 }} />
        {showInstall && (
          <button
            className="pill-btn pill-start"
            style={{ fontSize: 11 }}
            disabled={busy !== null}
            onClick={handleInstall}
          >
            {busy === "install" ? "Installing…" : "Install"}
          </button>
        )}
        {showLogin && (
          <button
            className="pill-btn pill-start"
            style={{ fontSize: 11 }}
            disabled={busy !== null}
            onClick={handleLogin}
          >
            {busy === "login" ? "Signing in…" : "Sign in"}
          </button>
        )}
        {showLogout && (
          <button
            className="pill-btn"
            style={{ fontSize: 11 }}
            disabled={busy !== null}
            onClick={handleLogout}
          >
            {busy === "logout" ? "Signing out…" : "Sign out"}
          </button>
        )}
      </div>
      {message && (
        <div style={{ fontSize: 11, color: "var(--text-tertiary)", paddingLeft: 18 }}>{message}</div>
      )}
    </div>
  );
}
