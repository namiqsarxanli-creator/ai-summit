import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";
import type { Metadata } from "next";

import { MasterclassProgram } from "@/components/masterclasses/MasterclassProgram";

export const metadata: Metadata = {
  title: "Masterclasses",
};

// Reached from Home, not from the five-tab bar (which it still shows, with no
// tab highlighted). The component loads the programme and the attendee's own
// choices itself.
export default function MasterclassesPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-masterclasses">
      <PageHeader title="Masterclasses" description="Choose one masterclass in each block" kind="home" />
      <MasterclassProgram />
      <BrandCredit />
    </main>
  );
}
