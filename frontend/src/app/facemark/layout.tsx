import type { Metadata } from "next";

// The FACEMARK organiser section: its own entrance, like /admin and /staff. Its
// link is handed to the organisers; it is never linked from attendee pages and
// is kept out of search results.
export const metadata: Metadata = { robots: { index: false, follow: false } };

export default function FacemarkLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}
