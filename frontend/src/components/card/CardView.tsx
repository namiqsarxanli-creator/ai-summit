import { Avatar } from "@/components/ui/Avatar";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { safeExternalUrl, type PublicCard } from "@/lib/card";

// Renders exactly what the server sent: name, profession, interests and
// the ONE contact action it chose (§9.3.1). There is no phone field to
// render, and no choosing between LinkedIn and email happens here.
export function CardView({ card, heading = "h1" }: { card: PublicCard; heading?: "h1" | "h2" }) {
  const { t } = useLanguage();
  const Heading = heading;
  return (
    <article className="fm-card fm-stack-lg">
      <div className="fm-row" style={{ flexWrap: "nowrap", gap: 16 }}>
        <Avatar size="card" url={card.photo_url} />
        <div className="fm-grow">
          <Heading className="fm-h2">
            {card.first_name} {card.last_name}
          </Heading>
          <p className="fm-small">{card.profession}</p>
        </div>
      </div>

      {card.interests.length > 0 && (
        <ul aria-label={t("Interests")} className="fm-chips">
          {card.interests.map((interest) => (
            <li key={interest.slug} className="fm-chip">
              {t(interest.name)}
            </li>
          ))}
        </ul>
      )}

      <ContactButton card={card} />
    </article>
  );
}

const BUTTON = "fm-btn fm-btn-primary fm-btn-block";

function ContactButton({ card }: { card: PublicCard }) {
  const { t } = useLanguage();
  const { contact } = card;
  // The contact opens only after the mutual QR scan in the app.
  if (!contact) return <p className="fm-small fm-center">{t("Scan each other's QR in AI Summit to exchange contacts.")}</p>;
  if (contact.type === "LINKEDIN") {
    const href = safeExternalUrl(contact.value);
    if (!href) return null;
    return (
      // noreferrer: the card URL (whose path IS the token) must not
      // reach LinkedIn as a Referer (§21.11) — belt and braces with the
      // page's own Referrer-Policy: no-referrer.
      <a href={href} target="_blank" rel="noopener noreferrer" className={BUTTON}>
        {t("Connect on LinkedIn")}
      </a>
    );
  }
  return (
    <div className="fm-stack" style={{ gap: 8 }}>
      <a href={`mailto:${contact.value}`} className={BUTTON}>
        {t("Email")} {card.first_name}
      </a>
      <p className="fm-small fm-center">{contact.value}</p>
    </div>
  );
}
