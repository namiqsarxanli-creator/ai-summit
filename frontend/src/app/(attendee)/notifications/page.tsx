import type { Metadata } from "next";

import { NotificationList } from "@/components/notifications/Notifications";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "Notifications",
};

// Reached from the bell on Home: reminders, requests waiting for an answer, points.
export default function NotificationsPage() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="Notifications" description="Reminders, requests and points in one place" kind="home" />
      <NotificationList />
      <BrandCredit />
    </main>
  );
}
