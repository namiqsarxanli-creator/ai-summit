"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";
import Link from "next/link";

import type { Recommendation } from "@/lib/matching";

import { BoothArt } from "./BoothArt";

// Renders exactly what the server sent for one booth: no score is ever
// computed here (§24.5, §24.9). `match: null` renders a neutral prompt,
// never a fabricated 0%.
// `askForTopics`: say "select your interests" on a booth without a match only when
// the attendee has really picked no topics (otherwise the booth simply has no
// interest profile yet, and the prompt would be wrong).
export function RecommendationCard({ recommendation, askForTopics = false }: { recommendation: Recommendation; askForTopics?: boolean }) {
  const { booth, match } = recommendation;
  const { t, language } = useLanguage();
  // "Company — Game": the company as the eyebrow, the game as the title.
  const [company, game] = splitName(t(booth.name));
  return (
    <li className="fm-booth-tile">
      <BoothArt />
      <div className="fm-booth-tile-head">
        <div className="fm-grow">
          {company && <p className="fm-eyebrow">{company}</p>}
          <h3 className="fm-booth-name">
            <Link href={`/booths/${encodeURIComponent(booth.slug)}`}>{game}</Link>
          </h3>
          {booth.location_label && <p className="fm-small">{t(booth.location_label)}</p>}
        </div>
        {match && <ScoreBadge score={match.score} />}
      </div>
      {booth.description && <p className="fm-booth-blurb">{t(booth.description)}</p>}

      {match ? (
        <>
          {match.matched_interests.length > 0 && (
            <ul aria-label={t("Matched interests")} className="fm-chips">
              {match.matched_interests.map((interest) => (
                <li key={interest.slug} className="fm-chip">
                  {t(interest.name)}
                </li>
              ))}
            </ul>
          )}
          <p className="fm-text-soft">{language === "en" ? match.explanation : localizeExplanation(match.explanation, match.matched_interests.map((interest) => t(interest.name)), t)}</p>
        </>
      ) : askForTopics ? (
        <p className="fm-small">
          {t("Select your interests to see how well this booth matches you.")}
        </p>
      ) : null}
      <Link href={`/playground?booth=${encodeURIComponent(booth.slug)}`} className="fm-btn fm-btn-primary fm-booth-play">
        {t("Book a 15-minute session")}
      </Link>
    </li>
  );
}

function splitName(name: string): [string | null, string] {
  const at = name.indexOf(" — ");
  return at > 0 ? [name.slice(0, at), name.slice(at + 3)] : [null, name];
}

function localizeExplanation(explanation: string, names: string[], t: (text: string) => string): string {
  const separator = " based on your interests in ";
  const index = explanation.indexOf(separator);
  if (index < 0 || names.length === 0) return t(explanation);
  const listed = names.slice(0, 3);
  const joined = listed.length === 1 ? listed[0] : `${listed.slice(0, -1).join(", ")} ${t("and")} ${listed.at(-1)}`;
  return `${t(explanation.slice(0, index))} ${t("based on your interests in")} ${joined}.`;
}
function ScoreBadge({ score }: { score: number }) {
  const { t } = useLanguage();
  return (
    <span aria-label={`${score}% ${t("match")}`} className="fm-badge" style={{ flex: "none" }}>
      {score}%
    </span>
  );
}
