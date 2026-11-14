"use client";

import type { ReactNode } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";

export type RecordValue = Record<string, unknown>;

export function display(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.length ? value.map(display).join(", ") : "—";
  if (typeof value === "object") return Object.entries(value).map(([key, item]) => `${key.replaceAll("_", " ")}: ${display(item)}`).join(" · ");
  return String(value);
}

// `actions`, when given, adds whatever it returns for a row (a button) under that row.
export function Records({ data, empty, actions }: { data: unknown; empty?: string; actions?: (row: RecordValue, index: number) => ReactNode }) {
  const { t } = useLanguage();
  const rows = Array.isArray(data) ? data as RecordValue[] : data && typeof data === "object" ? [data as RecordValue] : [];
  if (!rows.length) return <p className="rounded-xl border border-(--fm-line) p-5 text-sm text-(--fm-ink-soft)">{t(empty ?? "No records to show yet.")}</p>;
  return <ul className="grid gap-3" aria-label={t("Admin records")}>{rows.map((row, index) =>
    <li key={String(row.id ?? index)} className="min-w-0 rounded-xl border border-(--fm-line) bg-(--fm-tint) p-4">
      <dl className="grid gap-2 sm:grid-cols-2">{Object.entries(row).map(([key, value]) =>
        <div key={key} className="min-w-0"><dt className="text-xs font-bold uppercase tracking-wide text-(--fm-red)">{key.replaceAll("_", " ")}</dt><dd className="break-words text-sm text-(--fm-ink)">{display(value)}</dd></div>
      )}</dl>
      {actions?.(row, index)}
    </li>
  )}</ul>;
}
