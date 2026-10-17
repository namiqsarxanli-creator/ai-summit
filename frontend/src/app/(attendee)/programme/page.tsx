import type { Metadata } from "next";

import { MyProgramme } from "@/components/programme/MyProgramme";
import { ReminderChoice } from "@/components/programme/ReminderChoice";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "My programme",
};

// Reached from Home: the attendee's own day, in event time.
export default function ProgrammePage() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="My programme" description="Your bookings and meetings in time order" kind="profile" />
      <ReminderChoice />
      <MyProgramme />
      <BrandCredit />
    </main>
  );
}
