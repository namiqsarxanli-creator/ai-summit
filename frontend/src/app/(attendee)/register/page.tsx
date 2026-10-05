import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";
import type { Metadata } from "next";

import { RegisterForm } from "@/components/registration/RegisterForm";

export const metadata: Metadata = {
  title: "Register",
};

export default function RegisterPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-profile fm-auth">
      <PageHeader title="Join the summit network" description="Your summit, your connections." kind="profile" />
      <RegisterForm />
      <BrandCredit />
    </main>
  );
}
