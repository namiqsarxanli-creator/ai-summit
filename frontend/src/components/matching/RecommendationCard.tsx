import type { Recommendation } from "@/lib/matching";

// Renders exactly what the server sent for one booth: no score is ever
// computed here (§24.5, §24.9). `match: null` renders a neutral prompt,
// never a fabricated 0%.
export function RecommendationCard({ recommendation }: { recommendation: Recommendation }) {
  const { booth, match } = recommendation;
  return (
    <li className="flex flex-col gap-3 rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-lg font-semibold text-slate-50">{booth.name}</h3>
          {booth.location_label && <p className="text-sm text-slate-500">{booth.location_label}</p>}
        </div>
        {match && <ScoreBadge score={match.score} />}
      </div>

      {match ? (
        <>
          {match.matched_interests.length > 0 && (
            <ul aria-label="Matched interests" className="flex flex-wrap gap-2">
              {match.matched_interests.map((interest) => (
                <li
                  key={interest.slug}
                  className="rounded-full border border-indigo-500/30 bg-indigo-500/10 px-3 py-1 text-xs font-medium text-indigo-200"
                >
                  {interest.name}
                </li>
              ))}
            </ul>
          )}
          <p className="text-sm text-slate-400">{match.explanation}</p>
        </>
      ) : (
        <p className="text-sm text-slate-500">
          Select your interests to see how well this booth matches you.
        </p>
      )}
    </li>
  );
}

function ScoreBadge({ score }: { score: number }) {
  return (
    <span
      aria-label={`${score}% match`}
      className="shrink-0 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 text-sm font-semibold text-emerald-300"
    >
      {score}%
    </span>
  );
}
