"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";

// A booth's banner on the Booths page, in the summit's own poster style (the
// @aisummitbaku posts): the summit's sky with its red-and-white checkered peaks
// (/booth-sky.jpg, from the summit's own poster), a red ribbon, a round badge
// with the booth's game mark, a checkered strip. Each booth shows its own part
// of the sky and its own mark; any other booth gets a part chosen from its slug.

type Art = { at: string; icon: keyof typeof ICONS };

const BOOTHS: Record<string, Art> = {
  aiworks: { at: "50% 30%", icon: "wheel" },
  "facemark-brand-workshop": { at: "0% 70%", icon: "palette" },
  "spark-ai-b2b-os": { at: "100% 45%", icon: "target" },
  "faridteaches-ai-diagnose-lab": { at: "25% 100%", icon: "lens" },
  "handex-can-you-guess": { at: "75% 5%", icon: "brush" },
};

const SPOTS = ["15% 30%", "90% 25%", "40% 70%", "65% 5%", "0% 55%"];

const stroke = { fill: "none", stroke: "currentColor", strokeWidth: 2.4, strokeLinecap: "round", strokeLinejoin: "round" } as const;

const ICONS = {
  wheel: (
    <g {...stroke}>
      <circle cx="24" cy="25" r="16" />
      <circle cx="24" cy="25" r="3" fill="currentColor" />
      <path d="M24 9v13M24 28v13M8 25h13M27 25h13M13 14l9 9M26 27l9 9M35 14l-9 9M22 27l-9 9" />
      <path d="M24 3l-3.5 5h7z" fill="currentColor" />
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
      <circle cx="22" cy="26" r="15" />
      <circle cx="22" cy="26" r="9" />
      <circle cx="22" cy="26" r="3" fill="currentColor" />
      <path d="M22 26L39 9M33 9h6v6" />
    </g>
  ),
  lens: (
    <g {...stroke}>
      <circle cx="20" cy="20" r="12" />
      <path d="M29 29l11 11" strokeWidth={4.5} />
      <path d="M13 20h4l2-4 3 8 2-4h3" />
    </g>
  ),
  brush: (
    <g {...stroke}>
      <path d="M39 7L21 26" strokeWidth={3.4} />
      <path d="M21 26c-5-1-9 2-9 7 0 3-2 5-5 6 6 3 15 2 17-5 1-3-1-6-3-8z" fill="currentColor" />
    </g>
  ),
} as const;

function spotFor(slug: string): string {
  let h = 0;
  for (const ch of slug) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return SPOTS[h % SPOTS.length]!;
}

export function BoothArt({ slug }: { slug: string }) {
  const { t } = useLanguage();
  const art = BOOTHS[slug];
  return (
    <div className="fm-booth-art" aria-hidden="true" style={{ backgroundPosition: `center, ${art?.at ?? spotFor(slug)}` }}>
      <span className="fm-booth-ribbon">{t("AI Playground · 15 min")}</span>
      <svg className="fm-booth-badge" viewBox="0 0 48 48" width="40" height="40">{ICONS[art?.icon ?? "wheel"]}</svg>
      <span className="fm-booth-checks" />
    </div>
  );
}
