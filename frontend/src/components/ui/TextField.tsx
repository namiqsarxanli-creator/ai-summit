"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";
import type { InputHTMLAttributes } from "react";

type TextFieldProps = {
  id: string;
  label: string;
  error?: string;
  hint?: string;
} & Omit<InputHTMLAttributes<HTMLInputElement>, "id">;

// One labelled text input for every attendee and staff form: a 16 px field (so
// iOS does not zoom the page on focus), a 48 px target, the hint and the error
// tied to the input for screen readers. Extra classes go on the input itself.
export function TextField({ id, label, error, hint, className, ...inputProps }: TextFieldProps) {
  const { t } = useLanguage();
  const describedBy = [error ? `${id}-error` : null, hint ? `${id}-hint` : null]
    .filter(Boolean)
    .join(" ");

  return (
    <div className="fm-field">
      <label htmlFor={id} className="fm-label">
        {t(label)}
      </label>
      <input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy || undefined}
        className={`fm-input ${className ?? ""}`.trim()}
        {...inputProps}
      />
      {hint && !error && (
        <p id={`${id}-hint`} className="fm-hint">
          {t(hint)}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} role="alert" className="fm-error">
          {t(error)}
        </p>
      )}
    </div>
  );
}
