import type { Metadata } from "next";

// The staff area is reached by the link handed to staff, not from the attendee
// home, and is kept out of search results.
export const metadata: Metadata = { robots: { index: false, follow: false } };

export default function StaffLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}
