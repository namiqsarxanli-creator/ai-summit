"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { HomeIcon } from "@/components/ui/HomeIcons";
import Link from "next/link";

// IMPLEMENTATION_PLAN.md M08: "Mobile navigation: Home · Booths · My
// QR · Leaderboard · Profile." `active` is passed explicitly by each
// page rather than derived from the URL, because each page already
// knows which surface it is once its own data has loaded — the same
// moment it is safe to show this bar at all (see the "ok" states in
// HomeDashboard / Recommendations / MyCard / ProfileView).
const TABS = [
  { key: "home", href: "/", label: "Home", icon: "home" },
  { key: "booths", href: "/booths", label: "Booths", icon: "booths" },
  { key: "qr", href: "/my-card", label: "My QR", icon: "qr" },
  { key: "leaderboard", href: "/leaderboard", label: "Leaderboard", icon: "leaderboard" },
  { key: "profile", href: "/profile", label: "Profile", icon: "profile" },
] as const;

export type AttendeeTabKey = (typeof TABS)[number]["key"];

// Fixed to the bottom; the page shell (`.fm-page:has(.fm-tabbar)`) reserves the
// space for it plus the device's bottom safe area, so it never covers content.
// Every link is at least 56px tall — above the 44px touch-target floor.
// `active` is left out on a page that is not one of the five tabs (it is reached
// from Home, e.g. the masterclass programme): then no tab is highlighted.
export function AttendeeTabBar({ active }: { active?: AttendeeTabKey }) {
  const { t } = useLanguage();
  return (
    <nav aria-label={t("Attendee navigation")} className="fm-tabbar">
      <ul className="fm-tabbar-list">
        {TABS.map((tab) => {
          const isActive = tab.key === active;
          return (
            <li key={tab.key}>
              <Link href={tab.href} aria-current={isActive ? "page" : undefined} className="fm-tab-link">
                <span className="fm-tab-icon" aria-hidden="true"><HomeIcon name={tab.icon} /></span>
                {t(tab.label)}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
