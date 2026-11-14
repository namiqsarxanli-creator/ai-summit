import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";
import type { Metadata } from "next";

import { LegacyScanNotice } from "@/components/staff/LegacyScanNotice";
import { Scanner } from "@/components/staff/Scanner";

export const metadata: Metadata = {
  title: "Scan attendee",
};

export default function StaffScanPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-qr">
      <PageHeader title="Scan attendee" description="Scan a QR code to open the attendee card." kind="qr" backHref="/staff" />
      <LegacyScanNotice />
      <Scanner />
      <BrandCredit />
    </main>
  );
}
