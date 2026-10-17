"use client";

import { useEffect, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { fetchFeed, safePhotoUrl } from "@/lib/pairPhotos";

const REFRESH_MS = 30_000;

// The Beyond Now Photo screen: only pictures both people agreed to show and a
// moderator approved. No names, no ids. A bounded refresh while it is open.
export function PhotoFeed() {
  const { t } = useLanguage();
  const [urls, setUrls] = useState<string[] | null>(null);

  useEffect(() => {
    let active = true;
    const load = () => void fetchFeed().then((r) => {
      if (active && r.ok) setUrls(r.data.photos.map((p) => safePhotoUrl(p.photo_url)).filter((u): u is string => u !== null));
    });
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  if (urls === null) return <p role="status" className="fm-note">{t("Loading…")}</p>;
  if (urls.length === 0) return <p className="fm-empty">{t("Photos appear here when both people agree.")}</p>;
  return (
    <ul className="fm-chips" style={{ gap: 12 }}>
      {urls.map((src) => (
        <li key={src}>
          {/* eslint-disable-next-line @next/next/no-img-element -- a server-made, fixed-shape URL */}
          <img src={src} alt="" width={240} height={240} style={{ borderRadius: 16, display: "block" }} />
        </li>
      ))}
    </ul>
  );
}
