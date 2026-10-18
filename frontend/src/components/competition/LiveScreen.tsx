"use client";

import { useEffect, useRef, useState } from "react";

import { FinalScreen } from "@/components/competition/FinalScreen";
import { Avatar } from "@/components/ui/Avatar";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { CATEGORY_LABEL } from "@/lib/competition";
import { eventTime } from "@/lib/masterclasses";
import { safePhotoUrl } from "@/lib/pairPhotos";
import { fetchLiveScreen, formatCountdown, MISSING_LABEL, remainingMs, type LiveScreen as Live } from "@/lib/screen";

// One read per REFRESH_MS while the page is visible; nothing while it is hidden.
// The ranking is the server's shared snapshot, so a few screens cost the server
// a few small reads a minute. After a failure the screen keeps what it showed,
// says the connection is lost, and retries with a growing pause.
export const REFRESH_MS = 15_000;
export const PHOTO_REFRESH_MS = 8_000;
const MAX_RETRY_MS = 30_000;
const RETRY_MS = [5_000, 10_000, 20_000, MAX_RETRY_MS];

// The venue's big screen during the day: the live top 10, the latest points, the
// countdown to the 17:30 close (by the server's clock) and the shared photos.
// After the close it says the results are being reviewed; once a Game Master
// publishes, it shows the published final. It never ranks anything itself.
export function LiveScreen() {
  const { t, language } = useLanguage();
  const [data, setData] = useState<Live | null>(null);
  const [lost, setLost] = useState(false);
  const [offset, setOffset] = useState(0); // server time minus this device's time
  const [now, setNow] = useState(() => Date.now());
  const reload = useRef<() => void>(() => {});

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    let failures = 0;
    let inFlight = false;
    const schedule = (ms: number) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(load, ms);
    };
    function load() {
      window.clearTimeout(timer);
      if (document.visibilityState === "hidden" || inFlight) return;
      inFlight = true;
      void fetchLiveScreen().then((r) => {
        inFlight = false;
        if (!active) return;
        if (r.ok) {
          failures = 0;
          setLost(false);
          setData(r.data);
          setOffset(Date.parse(r.data.server_time) - Date.now());
          if (r.data.state !== "published") schedule(REFRESH_MS); // the final refreshes itself
        } else {
          setLost(true);
          schedule(RETRY_MS[Math.min(failures, RETRY_MS.length - 1)] ?? MAX_RETRY_MS);
          failures += 1;
        }
      });
    }
    const onVisible = () => {
      if (document.visibilityState === "visible") load();
    };
    reload.current = load;
    load();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      active = false;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  const counting = data?.state === "open" && data.closes_at !== null;
  useEffect(() => {
    if (!counting) return;
    const tick = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(tick);
  }, [counting]);

  const left = counting && data?.closes_at ? remainingMs(data.closes_at, offset, now) : null;
  // The close has come by the server's clock: ask the server for the next state.
  useEffect(() => {
    if (left === 0) reload.current();
  }, [left]);

  if (data === null) {
    return lost
      ? <p role="alert" className="fm-alert fm-alert-warn">{t("No connection to the server. Retrying…")}</p>
      : <p role="status" className="fm-note">{t("Loading…")}</p>;
  }

  const banner = lost && (
    <p role="alert" className="fm-alert fm-alert-warn">{t("Connection lost. Showing the last data; reconnecting…")}</p>
  );
  if (data.state === "published") {
    return (
      <div className="fm-stack-lg">
        {banner}
        <FinalScreen />
      </div>
    );
  }
  if (data.state === "not_configured") {
    return (
      <div className="fm-stack-lg">
        {banner}
        <section className="fm-alert fm-alert-info" role="status">
          <p className="fm-strong">{t("The competition programme is not set up yet.")}</p>
          <p>{t("The organisers still need to set:")}</p>
          <ul style={{ paddingLeft: 20, listStyle: "disc" }}>{data.missing.map((m) => <li key={m}>{t(MISSING_LABEL[m] ?? m)}</li>)}</ul>
        </section>
        <Photos urls={data.photos} />
      </div>
    );
  }

  const zone = data.timezone;
  return (
    <div className="fm-live">
      {banner}
      <section className="fm-live-clock" aria-labelledby="live-clock">
        <h2 id="live-clock" className="fm-eyebrow">
          {data.state === "open" ? t("Ranking closes at {time}").replace("{time}", eventTime(data.closes_at ?? "", zone, language)) : t("The ranking has closed")}
        </h2>
        {data.state === "open" && left !== null
          ? <p className="fm-live-countdown" aria-live="off" data-testid="countdown">{formatCountdown(left)}</p>
          : <p className="fm-strong">{t("The ranking has closed. The Game Master is reviewing the results.")}</p>}
      </section>

      {data.state === "open" && (
        <>
          <section className="fm-live-top" aria-labelledby="live-top">
            <h2 id="live-top" className="fm-eyebrow">{t("Live top 10")}</h2>
            {data.top.length === 0
              ? <p className="fm-empty">{t("No points yet. The first points appear here.")}</p>
              : (
                <ol className="fm-list" aria-label={t("Live top 10")}>
                  {data.top.map((p) => (
                    <li key={p.place} data-rank={p.place} className="fm-rank-row">
                      <div className="fm-rank-main">
                        <span className="fm-rank-position">#{p.place}</span>
                        <Avatar size="rank" url={p.photo_url} />
                        <p className="fm-rank-name">{p.name}</p>
                      </div>
                      <span className="fm-rank-points">{p.points} {t("pts")}</span>
                    </li>
                  ))}
                </ol>
              )}
          </section>
          <section className="fm-live-recent" aria-labelledby="live-recent">
            <h2 id="live-recent" className="fm-eyebrow">{t("Latest points")}</h2>
            {data.recent.length === 0
              ? <p className="fm-empty">{t("No points yet.")}</p>
              : (
                <ul className="fm-list" aria-label={t("Latest points")}>
                  {data.recent.map((a, i) => (
                    <li key={`${a.at}-${i}`} className="fm-small">
                      <span className="fm-strong">{a.name}</span> +{a.points} · {t(CATEGORY_LABEL[a.category] ?? a.category)} · {eventTime(a.at, zone, language)}
                    </li>
                  ))}
                </ul>
              )}
          </section>
        </>
      )}
      <Photos urls={data.photos} />
    </div>
  );
}

function Photos({ urls }: { urls: { photo_url: string }[] }) {
  const { t } = useLanguage();
  const safe = urls.map((p) => safePhotoUrl(p.photo_url)).filter((u): u is string => u !== null);
  const [index, setIndex] = useState(0);
  const count = safe.length;
  useEffect(() => {
    if (count < 2) return;
    const timer = window.setInterval(() => setIndex((value) => (value + 1) % count), PHOTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [count]);
  // Always select from the latest feed: a withdrawn photo disappears on refresh.
  const current = count > 0 ? safe[index % count] : null;
  return (
    <section className="fm-live-photos" aria-labelledby="live-photos">
      <h2 id="live-photos" className="fm-eyebrow">{t("Beyond Now Photo")}</h2>
      {safe.length === 0
        ? <p className="fm-empty">{t("Photos appear here when both people agree.")}</p>
        : (
          <ul className="fm-chips" style={{ gap: 12 }}>
            <li>
              {/* eslint-disable-next-line @next/next/no-img-element -- a server-made, fixed-shape URL */}
              <img data-testid="live-photo" src={current ?? undefined} alt="" width={200} height={200} style={{ borderRadius: 16, display: "block" }} />
            </li>
          </ul>
        )}
    </section>
  );
}
