import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";
import type { Metadata } from "next";

import { StaffPasswordForm } from "@/components/staff/StaffPasswordForm";

export const metadata: Metadata = {
  title: "Change password",
};

// A signed-in staff member or administrator changes their own password.
export default function StaffPasswordPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-profile fm-auth">
      <PageHeader title="Change your password" description="Changing it ends every session of your account, on every device." backHref="/staff" kind="profile" />
      <StaffPasswordForm />
      <BrandCredit />
    </main>
  );
}
