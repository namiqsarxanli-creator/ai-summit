import type { Metadata } from "next";

import { StaffLoginForm } from "@/components/staff/StaffLoginForm";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "FACEMARK sign in",
};

// The FACEMARK organisers' own sign-in. The accounts are the staff accounts an
// administrator created; after signing in the organiser lands in /facemark,
// which admits administrators and Game Masters only (the server decides).
export default function FacemarkLoginPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-profile fm-auth">
      <PageHeader title="FACEMARK sign in" description="For FACEMARK organisers: administrators and Game Masters." kind="profile" showBack={false} />
      <StaffLoginForm home="/facemark" />
      <BrandCredit />
    </main>
  );
}
