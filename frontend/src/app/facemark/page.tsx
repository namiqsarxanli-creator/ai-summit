import type { Metadata } from "next";

import { OrganiserPanel } from "@/components/organiser/OrganiserPanel";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "FACEMARK — Organiser Panel",
};

// The FACEMARK organiser workspace (administrators and Game Masters), a section
// of its own beside /admin and /staff.
export default function FacemarkPage() {
  return (
    <main id="main-content" className="fm-page fm-organiser">
      <PageHeader title="FACEMARK — Organiser Panel" description="AI Summit · Beyond Now" kind="profile" showBack={false} />
      <OrganiserPanel />
      <BrandCredit />
    </main>
  );
}
