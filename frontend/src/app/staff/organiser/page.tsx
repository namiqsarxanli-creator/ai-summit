import type { Metadata } from "next";

import { OrganiserPanel } from "@/components/organiser/OrganiserPanel";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "FACEMARK — Organiser Panel",
};

// The FACEMARK organiser workspace (administrators and Game Masters). Reached by
// its own link and from the admin console - never from attendee pages. Kept out
// of search results by the staff layout.
export default function OrganiserPage() {
  return (
    <main id="main-content" className="fm-page fm-organiser">
      <PageHeader title="FACEMARK — Organiser Panel" description="AI Summit · Beyond Now" kind="profile" backHref="/staff" />
      <OrganiserPanel />
      <BrandCredit />
    </main>
  );
}
