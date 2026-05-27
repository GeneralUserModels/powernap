import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  getAgentBackendStatus,
  installAgentBackend,
  loginAgentBackend,
  type AgentBackendInfo,
  type AgentBackendStatus,
} from "../../../api/client";

type BackendChoice = "gemini" | "codex" | "claude_code";

type Props = {
  onBack: () => void;
  onContinue: (backend: BackendChoice) => void;
};

const BACKENDS: { id: Exclude<BackendChoice, "gemini">; label: string; docsUrl: string }[] = [
  { id: "codex", label: "Codex CLI", docsUrl: "https://developers.openai.com/codex/cli" },
  { id: "claude_code", label: "Claude Code CLI", docsUrl: "https://code.claude.com/docs/en/getting-started" },
];

const NODE_DOWNLOAD_URL = "https://nodejs.org/en/download";

function backendStatusLabel(info?: AgentBackendInfo): string {
  if (!info) return "Checking...";
  if (!info.available) return "Not installed";
  if (info.auth === "oauth") return "Signed in";
  return "Installed";
}

function validChoice(status: AgentBackendStatus | null, choice: BackendChoice): BackendChoice {
  if (choice === "gemini") return choice;
  return status?.backends[choice]?.auth === "oauth" ? choice : "gemini";
}

function suggestedChoice(status: AgentBackendStatus): BackendChoice {
  const signed = BACKENDS
    .map((b) => b.id)
    .filter((id) => status.backends[id]?.auth === "oauth");
  if (signed.includes(status.selected as Exclude<BackendChoice, "gemini">)) {
    return status.selected;
  }
  if (signed.length === 1) return signed[0];
  return "gemini";
}

function StatusPill({ info }: { info?: AgentBackendInfo }) {
  const tone = !info ? "neutral" : !info.available ? "err" : info.auth === "oauth" ? "ok" : "warn";
  return <span className={`agent-setup-pill agent-setup-pill--${tone}`}>{backendStatusLabel(info)}</span>;
}

export function AgentSetupStep({ onBack, onContinue }: Props) {
  const [status, setStatus] = useState<AgentBackendStatus | null>(null);
  const [selected, setSelected] = useState<BackendChoice>("gemini");
  const [userSelected, setUserSelected] = useState(false);
  const [busy, setBusy] = useState<"refresh" | "install" | "codex-login" | "claude_code-login" | null>("refresh");
  const [message, setMessage] = useState("");

  const refresh = useCallback(async () => {
    const fresh = await getAgentBackendStatus();
    setStatus(fresh);
    setSelected((current) => userSelected ? validChoice(fresh, current) : suggestedChoice(fresh));
    return fresh;
  }, [userSelected]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setBusy("refresh");
      try {
        const fresh = await getAgentBackendStatus();
        if (cancelled) return;
        setStatus(fresh);
        setSelected(suggestedChoice(fresh));
      } catch {
        if (!cancelled) setMessage("Could not check agent setup yet.");
      } finally {
        if (!cancelled) setBusy(null);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const missingBackends = useMemo(
    () => BACKENDS.filter((b) => !status?.backends[b.id]?.available),
    [status],
  );
  const signedBackends = useMemo(
    () => BACKENDS.filter((b) => status?.backends[b.id]?.auth === "oauth"),
    [status],
  );

  const handleInstallMissing = async () => {
    if (!status?.npm.available || missingBackends.length === 0) return;
    setBusy("install");
    setMessage("Installing missing agent CLIs...");
    const failures: string[] = [];
    try {
      for (const backend of missingBackends) {
        const result = await installAgentBackend(backend.id);
        if (!result.ok) failures.push(`${backend.label}: ${result.reason ?? "install failed"}`);
      }
      await refresh();
      setMessage(failures.length > 0 ? `Install finished with issues: ${failures.join("; ")}` : "Agent CLIs installed.");
    } finally {
      setBusy(null);
    }
  };

  const handleLogin = async (backend: Exclude<BackendChoice, "gemini">) => {
    setBusy(`${backend}-login`);
    setMessage("Opening sign-in...");
    try {
      const result = await loginAgentBackend(backend);
      setMessage(result.detail || "Complete sign-in in the browser.");
      let fresh = await refresh();
      for (let i = 0; i < 30 && fresh.backends[backend]?.auth !== "oauth"; i++) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        fresh = await refresh();
      }
    } finally {
      setBusy(null);
    }
  };

  const choose = (choice: BackendChoice) => {
    setUserSelected(true);
    setSelected(validChoice(status, choice));
  };

  const openUrl = (url: string) => {
    void window.tada.openExternalUrl(url);
  };

  const canInstall = Boolean(status?.npm.available && missingBackends.length > 0 && busy === null);
  const effectiveChoice = validChoice(status, selected);

  return (
    <div className="page active page--scroll">
      <div className="page-icon">
        <svg width="22" height="22" viewBox="0 0 16 16" fill="none">
          <path d="M4 4.5h8M4 8h8M4 11.5h5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
          <path d="M11.3 10.7 13.3 8.7l-2-2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </div>
      <div className="page-title">Agent setup</div>
      <p className="page-desc">Install Codex and Claude Code, then sign into either, both, or neither.</p>

      <div className="glass-card agent-setup-card">
        {busy === "refresh" && !status ? (
          <div className="agent-setup-loading">
            <span className="startup-spinner" />
            <span>Checking agent tools...</span>
          </div>
        ) : (
          <>
            <div className="agent-setup-section">
              <div className="agent-setup-section-head">
                <span className="agent-setup-section-title">Install tools</span>
                {status?.npm.available ? (
                  <span className="agent-setup-muted">npm {status.npm.version ?? ""}</span>
                ) : (
                  <span className="agent-setup-pill agent-setup-pill--err">npm missing</span>
                )}
              </div>
              {!status?.npm.available ? (
                <div className="agent-setup-note">
                  Install Node.js to get npm, then come back and retry.{" "}
                  <button type="button" className="agent-setup-link" onClick={() => openUrl(status?.npm.install_url || NODE_DOWNLOAD_URL)}>
                    Open Node.js downloads
                  </button>
                </div>
              ) : missingBackends.length > 0 ? (
                <button className="btn btn-outline" disabled={!canInstall} onClick={handleInstallMissing}>
                  {busy === "install" ? "Installing..." : `Install ${missingBackends.map((b) => b.label.replace(" CLI", "")).join(" and ")}`}
                </button>
              ) : (
                <div className="agent-setup-note">Both CLIs are installed.</div>
              )}
            </div>

            <div className="agent-setup-list">
              {BACKENDS.map((backend) => {
                const info = status?.backends[backend.id];
                const loginBusy = busy === `${backend.id}-login`;
                const canLogin = Boolean(info?.available && info.auth !== "oauth" && busy === null);
                return (
                  <div className="agent-setup-row" key={backend.id}>
                    <div className="agent-setup-row-main">
                      <div className="agent-setup-row-title">
                        <span>{backend.label}</span>
                        <StatusPill info={info} />
                      </div>
                      <button type="button" className="agent-setup-link" onClick={() => openUrl(backend.docsUrl)}>
                        Setup docs
                      </button>
                    </div>
                    {info?.available && info.auth !== "oauth" && (
                      <button className="btn btn-outline btn-sm" disabled={!canLogin} onClick={() => handleLogin(backend.id)}>
                        {loginBusy ? "Signing in..." : "Sign in"}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="agent-setup-section">
              <span className="agent-setup-section-title">Use for Tadas</span>
              <div className="agent-setup-choice-list">
                <button
                  type="button"
                  className={`agent-setup-choice${effectiveChoice === "gemini" ? " selected" : ""}`}
                  onClick={() => choose("gemini")}
                >
                  <span className="agent-setup-radio" />
                  <span>Tada default</span>
                </button>
                {BACKENDS.map((backend) => {
                  const signedIn = status?.backends[backend.id]?.auth === "oauth";
                  return (
                    <button
                      key={backend.id}
                      type="button"
                      className={`agent-setup-choice${effectiveChoice === backend.id ? " selected" : ""}`}
                      disabled={!signedIn}
                      onClick={() => choose(backend.id)}
                    >
                      <span className="agent-setup-radio" />
                      <span>{backend.label.replace(" CLI", "")}</span>
                    </button>
                  );
                })}
              </div>
              {signedBackends.length === 0 && (
                <div className="agent-setup-note">No CLI sign-in is required. Tada default will be used.</div>
              )}
            </div>
          </>
        )}

        {message && <div className="agent-setup-message">{message}</div>}
      </div>

      <div className="btn-row">
        <button className="btn btn-ghost" onClick={onBack}>Back</button>
        <button className="btn btn-primary" disabled={busy !== null} onClick={() => onContinue(effectiveChoice)}>
          Continue
        </button>
      </div>
    </div>
  );
}
