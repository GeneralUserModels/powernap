import { useEffect, useState } from "react";

export const REPO = "GeneralUserModels/tada";
export const GH_URL = `https://github.com/${REPO}`;
const RELEASES_PAGE = `${GH_URL}/releases/latest`;
const RESEARCH_PREVIEW_WARNING =
  "This application is a research preview and can be a bit buggy!";

export function useDmgUrl(): string {
  const [url, setUrl] = useState(RELEASES_PAGE);

  useEffect(() => {
    fetch(`https://api.github.com/repos/${REPO}/releases`, {
      headers: { Accept: "application/vnd.github.v3+json" },
    })
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then(
        (
          releases: Array<{
            assets: Array<{ name: string; browser_download_url: string }>;
          }>
        ) => {
          for (const release of releases) {
            const dmg = release.assets.find(
              (asset) => asset.name.endsWith(".dmg") && asset.name.includes("arm64")
            );
            if (dmg) {
              setUrl(dmg.browser_download_url);
              return;
            }
          }
        }
      )
      .catch(() => {});
  }, []);

  return url;
}

export function HeroActions({ dmgUrl }: { dmgUrl: string }) {
  return (
    <>
      <div className="hero-actions">
        <a href={dmgUrl} className="btn btn-primary">
          <DownloadIcon />
          Download for macOS (Apple Silicon)
        </a>
        <a
          href={GH_URL}
          className="btn btn-secondary"
          target="_blank"
          rel="noreferrer"
        >
          <StarIcon />
          Star on GitHub
        </a>
      </div>
      <p className="tada-alpha-note">{RESEARCH_PREVIEW_WARNING}</p>
    </>
  );
}

function StarIcon() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </svg>
  );
}
