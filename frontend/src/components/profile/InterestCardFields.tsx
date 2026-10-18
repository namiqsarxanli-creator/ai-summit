"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";
import {
  CARD_GROUPS,
  labelOf,
  selectedIn,
  toggleChoice,
  type CardGroupKey,
  type InterestCard,
} from "@/lib/interestCard";

// The four interest-card questions as chip groups, shared by registration and
// profile editing. A chosen chip can always be tapped again to remove it; once a
// group is full the other chips are disabled (the single-answer "my field"
// instead swaps to the newly tapped answer). An answer the list no longer has
// is shown only while it is still chosen - marked, removable, never re-addable.
export function InterestCardFields({
  idPrefix,
  card,
  onChange,
  error,
}: {
  idPrefix: string;
  card: InterestCard;
  onChange: (card: InterestCard) => void;
  error?: string;
}) {
  const { t } = useLanguage();
  const errorId = `${idPrefix}-card-error`;

  return (
    <fieldset
      className="fm-fieldset"
      aria-describedby={error ? errorId : undefined}
      data-testid="interest-card"
    >
      <legend className="fm-legend">{t("Interest card")}</legend>
      <p className="fm-hint">
        {t("Optional. Your topics shape your booth recommendations, and your answers help suggest people to meet. The card is not shown on your contact card.")}
      </p>
      {CARD_GROUPS.map((group) => {
        const chosen = selectedIn(card, group.key);
        const full = group.max > 1 && chosen.length >= group.max;
        const known = new Set(group.options.map((o) => o.id));
        // An answer the list no longer has stays visible, so it can be removed.
        const retired = chosen.filter((id) => !known.has(id));
        const options = [
          ...group.options,
          ...retired.map((id) => ({ id, label: labelOf(group.key, id) })),
        ];
        const headingId = `${idPrefix}-${group.key}-title`;
        return (
          <div
            key={group.key}
            role="group"
            aria-labelledby={headingId}
            className="fm-card-group"
            data-group={group.key}
          >
            <div className="fm-card-group-head">
              <p id={headingId} className="fm-strong">
                {t(group.title)}
              </p>
              <p className="fm-micro" aria-live="polite">
                {t(group.hint)}{" "}
                {group.max > 1 && (
                  <span>
                    {chosen.length}/{group.max} {t("selected")}
                  </span>
                )}
              </p>
            </div>
            <div className="fm-choices">
              {options.map((option) => {
                const checked = chosen.includes(option.id);
                return (
                  <label key={option.id} className="fm-choice">
                    <input
                      type="checkbox"
                      name={`${idPrefix}-${group.key}`}
                      value={option.id}
                      checked={checked}
                      disabled={full && !checked}
                      onChange={() => onChange(toggleChoice(card, group.key as CardGroupKey, option.id))}
                    />
                    {/* One text box, so the name and its mark wrap together (no
                        mid-word break) and the mark keeps the chip's own colour,
                        readable on the selected (red) chip too. */}
                    <span>
                      {t(option.label)}
                      {!known.has(option.id) && (
                        <span className="fm-choice-note"> · {t("no longer offered")}</span>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
            {full && (
              <p className="fm-micro">{t("Limit reached — remove a choice to pick another.")}</p>
            )}
          </div>
        );
      })}
      {error && (
        <p id={errorId} role="alert" className="fm-error">
          {t(error)}
        </p>
      )}
    </fieldset>
  );
}
