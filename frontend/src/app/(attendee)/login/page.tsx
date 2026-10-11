import { AttendeeLoginGate } from "@/components/auth/AttendeeLoginGate";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";
import type { Metadata } from "next";

import { LoginForm } from "@/components/auth/LoginForm";

export const metadata: Metadata = {
  title: "Sign in",
};

export default function LoginPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-profile fm-auth">
      <PageHeader title="Sign in" description="No password needed — we'll email you a one-time code." kind="profile" />
      <AttendeeLoginGate><LoginForm /></AttendeeLoginGate>
      <BrandCredit />
    </main>
  );
}
