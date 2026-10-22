"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { competitionRankingOn } from "@/lib/final";

// The booth-activity scanner predates the Beyond Now competition: while the
// competition ranking is on, the points it gives do not count there.
export function LegacyScanNotice() {
  const { t } = useLanguage();
  const [on, setOn] = useState(false);
  useEffect(() => {
    let active = true;
    void competitionRankingOn().then((value) => { if (active) setOn(value === true); });
    return () => { active = false; };
  }, []);
  if (!on) return null;
  return (
    <div role="alert" className="fm-alert fm-alert-warn fm-stack" style={{ gap: 8 }}>
      <p>{t("Points given here do not count in the Beyond Now competition. Use the attendance desk for competition points.")}</p>
      <Link href="/staff/attendance" className="fm-btn fm-btn-primary fm-btn-block">{t("Attendance scanning")}</Link>
    </div>
  );
}
