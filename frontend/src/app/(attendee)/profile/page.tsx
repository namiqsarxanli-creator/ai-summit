import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";
import type { Metadata } from "next";

import { ProfileView } from "@/components/profile/ProfileView";

export const metadata: Metadata = {
  title: "Profile",
};

export default function ProfilePage() {
  return (
    <main id="main-content" className="fm-page fm-screen-profile">
      <PageHeader showBack={false} title="Your profile" description="Your summit, your connections." kind="profile" />
      <ProfileView />
      <BrandCredit />
    </main>
  );
}
