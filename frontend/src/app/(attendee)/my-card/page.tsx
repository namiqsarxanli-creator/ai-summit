import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";
import type { Metadata } from "next";

import { MyCard } from "@/components/card/MyCard";

export const metadata: Metadata = {
  title: "My card",
};

export default function MyCardPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-qr">
      <PageHeader showBack={false} title="My QR" description="Show your attendee card" kind="qr" />
      <MyCard />
      <BrandCredit />
    </main>
  );
}
