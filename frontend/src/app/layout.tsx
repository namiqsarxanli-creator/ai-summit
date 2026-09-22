import type { Metadata } from "next";
import "./globals.css";

// M01 scope: a minimal, honest shell. No business UI (registration,
// leaderboard, QR, staff/admin views) belongs here — those are later
// milestones per IMPLEMENTATION_PLAN.md.
export const metadata: Metadata = {
  title: "FACEMARK",
  description: "FACEMARK — event networking and gamification platform (foundation build)",
  manifest: "/manifest.webmanifest",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-950 text-slate-50 antialiased">{children}</body>
    </html>
  );
}
