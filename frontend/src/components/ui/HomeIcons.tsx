import type { SVGProps } from "react";

type IconName =
  | "home" | "booths" | "qr" | "leaderboard" | "profile" | "arrow" | "medal"
  | "calendar" | "masterclass" | "playground" | "people" | "star" | "report";

export function HomeIcon({ name, ...props }: SVGProps<SVGSVGElement> & { name: IconName }) {
  const common = { fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  const paths: Record<IconName, React.ReactNode> = {
    home: <path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z" />,
    booths: <><path d="M3 21V7l9-4 9 4v14M3 9h18M9 21v-7h6v7M7 12h.01M17 12h.01" /></>,
    qr: <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><path d="M14 14h3v3h-3zM21 14v3M14 21h3M21 21h-3v-3" /></>,
    leaderboard: <><path d="M4 20h16M6 17V9h4v8M10 17V5h4v12M14 17v-6h4v6" /><path d="m12 2 .4 1.1 1.1.4-1.1.4L12 5l-.4-1.1-1.1-.4 1.1-.4z" /></>,
    profile: <><circle cx="12" cy="8" r="4" /><path d="M4 21v-2a8 8 0 0 1 16 0v2" /></>,
    arrow: <path d="m8 5 7 7-7 7" />,
    medal: <><circle cx="12" cy="9" r="6" /><path d="m8 14-2 8 6-3 6 3-2-8M12 6l.9 1.9 2.1.3-1.5 1.5.4 2.1L12 10.8l-1.9 1 .4-2.1L9 8.2l2.1-.3z" /></>,
    calendar: <><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M8 3v4M16 3v4M3 10h18M8 14h3M8 17h6" /></>,
    masterclass: <><path d="m2 9 10-5 10 5-10 5z" /><path d="M6 11v5c0 1.7 2.7 3 6 3s6-1.3 6-3v-5M22 9v6" /></>,
    playground: <><path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V3" /><path d="M7.5 15h9" /></>,
    people: <><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20v-1a6.5 6.5 0 0 1 13 0v1M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6.5 6.5 0 0 1 3.5 5.8V20" /></>,
    star: <path d="m12 3 2.7 5.6 6.1.8-4.4 4.3 1 6.1L12 17l-5.4 2.8 1-6.1-4.4-4.3 6.1-.8z" />,
    report: <><path d="M7 3h7l5 5v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" /><path d="M14 3v5h5M9 13h6M9 17h4" /></>,
  };
  return <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true" {...common} {...props}>{paths[name]}</svg>;
}