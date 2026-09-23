import Link from "next/link";

// IMPLEMENTATION_PLAN.md M08: "Mobile navigation: Home · Booths · My
// QR · Leaderboard · Profile." `active` is passed explicitly by each
// page rather than derived from the URL, because each page already
// knows which surface it is once its own data has loaded — the same
// moment it is safe to show this bar at all (see the "ok" states in
// HomeDashboard / Recommendations / MyCard / ProfileView).
const TABS = [
  { key: "home", href: "/", label: "Home" },
  { key: "booths", href: "/booths", label: "Booths" },
  { key: "qr", href: "/my-card", label: "My QR" },
  { key: "leaderboard", href: "/leaderboard", label: "Leaderboard" },
  { key: "profile", href: "/profile", label: "Profile" },
] as const;

export type AttendeeTabKey = (typeof TABS)[number]["key"];

// Fixed to the bottom, so every host page reserves space for it (see
// the `pb-24` on each page's <main>) rather than letting it cover
// content. Every link is at least 44px tall — the touch-target floor
// M08's manual/device test requires.
export function AttendeeTabBar({ active }: { active: AttendeeTabKey }) {
  return (
    <nav
      aria-label="Attendee navigation"
      className="fixed inset-x-0 bottom-0 z-10 border-t border-slate-800 bg-slate-950/95 backdrop-blur"
    >
      <ul className="mx-auto flex max-w-2xl">
        {TABS.map((tab) => {
          const isActive = tab.key === active;
          return (
            <li key={tab.key} className="flex-1">
              <Link
                href={tab.href}
                aria-current={isActive ? "page" : undefined}
                className={`flex min-h-[44px] flex-col items-center justify-center gap-0.5 px-1 py-2 text-center text-xs font-medium transition-colors ${
                  isActive
                    ? "text-indigo-300"
                    : "text-slate-500 hover:text-slate-300"
                }`}
              >
                {tab.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
