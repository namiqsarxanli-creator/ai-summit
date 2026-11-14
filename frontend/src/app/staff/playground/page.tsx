import type { Metadata } from "next";

import { PlaygroundStaff } from "@/components/playground/PlaygroundStaff";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "Playground arrivals and queue",
};

// Booth staff confirm arrivals for their own booth's sessions (the server checks
// the booth); administrators see every booth.
export default function StaffPlaygroundPage() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="Playground arrivals and queue" description="Manage arrivals at your booth" kind="home" backHref="/staff" />
      <PlaygroundStaff />
      <BrandCredit />
    </main>
  );
}
