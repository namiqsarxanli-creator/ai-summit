import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";
import type { Metadata } from "next";

import { StaffPanel } from "@/components/staff/StaffPanel";

export const metadata: Metadata = {
  title: "Staff panel",
};

export default function StaffPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-booths">
      <PageHeader title="Your booth" description="Scan attendee" kind="booths" />
      <StaffPanel />
      <BrandCredit />
    </main>
  );
}
