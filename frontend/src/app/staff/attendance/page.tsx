import type { Metadata } from "next";

import { AttendanceDesk } from "@/components/competition/AttendanceDesk";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "Attendance scanning",
};

export default function AttendancePage() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="Attendance scanning" description="Only the jobs assigned to your account" kind="qr" backHref="/staff" />
      <AttendanceDesk />
      <BrandCredit />
    </main>
  );
}
