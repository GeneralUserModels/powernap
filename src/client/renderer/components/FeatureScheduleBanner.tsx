import React, { useEffect, useMemo, useState } from "react";

interface Props {
  label: string;
  nextRunAt: string | null;
  canStart: boolean;
  starting: boolean;
  startFailed?: boolean;
  blockedReason?: string | null;
  onStart: () => void;
}

function formatUntil(dateStr: string | null, now: number): string {
  if (!dateStr) return "Next run not scheduled";
  const target = new Date(dateStr).getTime();
  if (!Number.isFinite(target)) return "Next run not scheduled";
  const diffMs = target - now;
  if (diffMs <= 60_000) return "Next run soon";
  const mins = Math.round(diffMs / 60_000);
  if (mins < 60) return `Next run in ${mins} minute${mins === 1 ? "" : "s"}`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `Next run in ${hours} hour${hours === 1 ? "" : "s"}`;
  const days = Math.round(hours / 24);
  return `Next run in ${days} day${days === 1 ? "" : "s"}`;
}

export function FeatureScheduleBanner({
  label,
  nextRunAt,
  canStart,
  starting,
  startFailed,
  blockedReason,
  onStart,
}: Props) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60000);
    return () => window.clearInterval(timer);
  }, []);

  const text = useMemo(() => formatUntil(nextRunAt, now), [nextRunAt, now]);
  const showButton = canStart || starting;
  const disabled = starting || !canStart;
  const title = !canStart && blockedReason === "first_run"
    ? "Available after the first scheduled run"
    : undefined;

  return (
    <div className="feature-schedule-banner">
      <div className="feature-schedule-row">
        <span className="feature-schedule-label">{label}</span>
        <span className="feature-schedule-text">
          {startFailed ? "Could not start. Try again later." : text}
        </span>
        {showButton && (
          <button
            type="button"
            className="feature-schedule-start"
            onClick={onStart}
            disabled={disabled}
            title={title}
          >
            {starting ? "Starting..." : "Start now"}
          </button>
        )}
      </div>
    </div>
  );
}
