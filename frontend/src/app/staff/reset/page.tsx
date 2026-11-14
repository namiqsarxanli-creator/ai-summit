import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";
import type { Metadata } from "next";

import { StaffResetForm } from "@/components/staff/StaffResetForm";

export const metadata: Metadata = {
  title: "Reset password",
};

// "I forgot my password": a one-time code by email, then a new password.
export default function StaffResetPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-profile fm-auth">
      <PageHeader title="Reset your password" description="We email a one-time code to your staff address. It is valid for 10 minutes." backHref="/staff/login" kind="profile" />
      <StaffResetForm />
      <BrandCredit />
    </main>
  );
}
