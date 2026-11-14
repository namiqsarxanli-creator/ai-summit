"use client";

import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";

import {
  isLanguage,
  LANGUAGE_STORAGE_KEY,
  languages,
  localiseTitle,
  translate,
  type Language,
} from "@/lib/i18n";

type LanguageContextValue = {
  language: Language;
  setLanguage: (language: Language) => void;
  t: (text: string) => string;
};

const LanguageContext = createContext<LanguageContextValue>({
  language: "en",
  setLanguage: () => {},
  t: (text) => text,
});

export function LanguageProvider({ children }: { children: React.ReactNode }) {
  const [language, setLanguage] = useState<Language>("en");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const saved = window.localStorage.getItem(LANGUAGE_STORAGE_KEY);
      const browserLanguage = window.navigator.language.toLowerCase().split("-")[0];
      setLanguage(isLanguage(saved) ? saved : isLanguage(browserLanguage) ? browserLanguage : "en");
      setReady(true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!ready) return;
    document.documentElement.lang = language;
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, language);
  }, [language, ready]);

  useLocalisedDocumentTitle(language, ready);

  const value = useMemo<LanguageContextValue>(
    () => ({ language, setLanguage, t: (text) => translate(text, language) }),
    [language],
  );
  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

/** The browser tab shows the page title in the visitor's language. The server
 * writes it in English ("Register · AI Summit"), and Next.js rewrites it on
 * every navigation; whenever it does, the English original is remembered and the
 * tab shows its translation. A title this hook wrote itself is recognised and
 * never treated as a new original. */
function useLocalisedDocumentTitle(language: Language, ready: boolean) {
  const original = useRef<string | null>(null);
  const written = useRef<string | null>(null);
  useEffect(() => {
    if (!ready) return;
    const apply = () => {
      const current = document.title;
      if (current !== written.current || original.current === null) original.current = current;
      const next = localiseTitle(original.current, language);
      written.current = next;
      if (next !== current) document.title = next;
    };
    apply();
    const observer = new MutationObserver(apply);
    observer.observe(document.head, { subtree: true, childList: true, characterData: true });
    return () => observer.disconnect();
  }, [language, ready]);
}

export function useLanguage() {
  return useContext(LanguageContext);
}

export function T({ id }: { id: string }) {
  const { t } = useLanguage();
  return <>{t(id)}</>;
}

export function LanguageSwitcher() {
  const { language, setLanguage, t } = useLanguage();
  return (
    <div className="fm-language-switcher">
      <div className="fm-language-options" role="group" aria-label={t("Language")}>
        {languages.map(({ code, label }) => (
          <button
            key={code}
            type="button"
            lang={code}
            className="fm-language-option"
            aria-label={label}
            aria-pressed={language === code}
            onClick={() => {
              window.localStorage.setItem(LANGUAGE_STORAGE_KEY, code);
              document.documentElement.lang = code;
              setLanguage(code);
            }}
          >
            {code.toUpperCase()}
          </button>
        ))}
      </div>
    </div>
  );
}
