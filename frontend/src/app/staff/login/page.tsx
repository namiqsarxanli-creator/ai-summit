import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";
import type { Metadata } from "next";

import { StaffLoginForm } from "@/components/staff/StaffLoginForm";

export const metadata: Metadata = {
  title: "Staff sign in",
};

// M09: staff sign-in, kept entirely separate from the attendee
// experience (different route tree, different session cookie
// contents, different endpoints) — a summit visitor never lands here
// by accident, and a staff member never sees attendee-only UI.
export default function StaffLoginPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-profile fm-auth">
      <PageHeader title="Staff sign in" description="Accounts are created by an admin — there is no staff self-registration." kind="profile" />
      <StaffLoginForm />
      <BrandCredit />
    </main>
  );
}
