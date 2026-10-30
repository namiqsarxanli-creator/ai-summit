// A booth's banner on the Booths page: its own colours and a mark for its game,
// so the booths are told apart at a glance (no shared robot picture). The five
// AI Playground booths have their own; any other booth gets a colour chosen from
// its slug and its initials.

type Art = { from: string; to: string; icon: keyof typeof ICONS };

const BOOTHS: Record<string, Art> = {
  aiworks: { from: "#a71923", to: "#e0503f", icon: "wheel" }, // Spin & Play
  "facemark-brand-workshop": { from: "#5b2a86", to: "#c2457a", icon: "palette" },
  "spark-ai-b2b-os": { from: "#0f4c81", to: "#2a9d8f", icon: "target" },
  "faridteaches-ai-diagnose-lab": { from: "#1d6b52", to: "#7cb342", icon: "lens" },
  "handex-can-you-guess": { from: "#c4561a", to: "#f2a541", icon: "brush" },
};

const FALLBACK: [string, string][] = [
  ["#7a1f5c", "#d14d72"],
  ["#20507a", "#4f9bd9"],
  ["#2f6f3e", "#8bbf5a"],
  ["#8a4b0f", "#e39b3c"],
  ["#3f3d8f", "#8a7fe0"],
];

const stroke = { fill: "none", stroke: "currentColor", strokeWidth: 2.2, strokeLinecap: "round", strokeLinejoin: "round" } as const;

const ICONS = {
  wheel: (
    <g {...stroke}>
      <circle cx="24" cy="24" r="17" />
      <circle cx="24" cy="24" r="3" />
      <path d="M24 7v14M24 27v14M7 24h14M27 24h14M12 12l10 10M26 26l10 10M36 12L26 22M22 26L12 36" />
      <path d="M24 2l-3 5h6z" fill="currentColor" />
    </g>
  ),
  palette: (
    <g {...stroke}>
      <path d="M24 6C13 6 6 14 6 23c0 8 6 13 12 13 3 0 4-2 4-4s2-4 5-4h4c6 0 11-4 11-10C42 12 34 6 24 6z" />
      <circle cx="15" cy="21" r="2.5" fill="currentColor" />
      <circle cx="21" cy="13" r="2.5" fill="currentColor" />
      <circle cx="30" cy="13" r="2.5" fill="currentColor" />
      <circle cx="36" cy="20" r="2.5" fill="currentColor" />
    </g>
  ),
  target: (
    <g {...stroke}>
      <circle cx="22" cy="26" r="16" />
      <circle cx="22" cy="26" r="10" />
      <circle cx="22" cy="26" r="4" />
      <path d="M22 26L40 8M34 8h6v6" />
    </g>
  ),
  lens: (
    <g {...stroke}>
      <circle cx="20" cy="20" r="12" />
      <path d="M29 29l12 12" strokeWidth={4} />
      <path d="M14 20h3l2-4 3 8 2-4h3" />
    </g>
  ),
  brush: (
    <g {...stroke}>
      <path d="M38 6L20 26" strokeWidth={3} />
      <path d="M20 26c-5-1-9 2-9 7 0 3-2 5-5 6 6 3 15 2 17-5 1-3-1-6-3-8z" />
      <path d="M30 34c3 0 6 2 8 5M9 12c2 3 5 4 8 4" />
    </g>
  ),
} as const;

function fallbackFor(slug: string): Art {
  let h = 0;
  for (const ch of slug) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const [from, to] = FALLBACK[h % FALLBACK.length]!;
  return { from, to, icon: "wheel" };
}

export function BoothArt({ slug, label }: { slug: string; label: string }) {
  const known = BOOTHS[slug];
  const art = known ?? fallbackFor(slug);
  const letters = label.replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  return (
    <div className="fm-booth-art" aria-hidden="true" style={{ background: `linear-gradient(120deg, ${art.from}, ${art.to})` }}>
      <span className="fm-booth-art-dots" />
      {known ? (
        <svg className="fm-booth-art-icon" viewBox="0 0 48 48" width="64" height="64">{ICONS[art.icon]}</svg>
      ) : (
        <span className="fm-booth-art-letters">{letters}</span>
      )}
    </div>
  );
}
