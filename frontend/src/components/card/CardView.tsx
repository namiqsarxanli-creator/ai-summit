import { safeExternalUrl, type PublicCard } from "@/lib/card";

// Renders exactly what the server sent: name, profession, interests and
// the ONE contact action it chose (§9.3.1). There is no phone field to
// render, and no choosing between LinkedIn and email happens here.
export function CardView({ card, heading = "h1" }: { card: PublicCard; heading?: "h1" | "h2" }) {
  const Heading = heading;
  return (
    <article className="flex flex-col gap-5 rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex flex-col gap-1">
        <Heading className="text-2xl font-semibold tracking-tight text-slate-50">
          {card.first_name} {card.last_name}
        </Heading>
        <p className="text-sm text-slate-400">{card.profession}</p>
      </div>

      {card.interests.length > 0 && (
        <ul aria-label="Interests" className="flex flex-wrap gap-2">
          {card.interests.map((interest) => (
            <li
              key={interest.slug}
              className="rounded-full border border-indigo-500/30 bg-indigo-500/10 px-3 py-1 text-xs font-medium text-indigo-200"
            >
              {interest.name}
            </li>
          ))}
        </ul>
      )}

      <ContactButton card={card} />
    </article>
  );
}

const BUTTON =
  "w-full rounded-lg bg-indigo-500 px-4 py-3 text-center font-semibold text-white transition-colors hover:bg-indigo-400";

function ContactButton({ card }: { card: PublicCard }) {
  const { contact } = card;
  if (contact.type === "LINKEDIN") {
    const href = safeExternalUrl(contact.value);
    if (!href) return null;
    return (
      // noreferrer: the card URL (whose path IS the token) must not
      // reach LinkedIn as a Referer (§21.11) — belt and braces with the
      // page's own Referrer-Policy: no-referrer.
      <a href={href} target="_blank" rel="noopener noreferrer" className={BUTTON}>
        Connect on LinkedIn
      </a>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <a href={`mailto:${contact.value}`} className={BUTTON}>
        Email {card.first_name}
      </a>
      <p className="text-center text-sm text-slate-400">{contact.value}</p>
    </div>
  );
}
