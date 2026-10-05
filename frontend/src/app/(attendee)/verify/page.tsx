import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";
import type { Metadata } from "next";

import { VerifyForm } from "@/components/registration/VerifyForm";

export const metadata: Metadata = {
  title: "Verify your email",
};

export default function VerifyPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-profile fm-auth">
      <PageHeader title="Check your email" description="Your summit, your connections." kind="profile" />
      <VerifyForm />
      <BrandCredit />
    </main>
  );
}
