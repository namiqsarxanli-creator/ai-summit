"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";

// A booth's banner on the Booths page, in the summit's own poster style (the
// @aisummitbaku posts): the summit's sky with its red-and-white checkered peaks
// (/booth-sky.jpg, from the summit's poster), shown whole and the same on every
// booth, with a red ribbon and a checkered strip.
export function BoothArt() {
  const { t } = useLanguage();
  return (
    <div className="fm-booth-art" aria-hidden="true">
      <span className="fm-booth-ribbon">{t("AI Playground · 15 min")}</span>
      <span className="fm-booth-checks" />
    </div>
  );
}
