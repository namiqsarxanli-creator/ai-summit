import type { Metadata } from "next";

import { PlaygroundProgram } from "@/components/playground/PlaygroundProgram";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "AI Playground",
};

// Reached from Home, not from the five-tab bar (which it still shows, with no tab
// highlighted). A team invitation arrives in the URL fragment (#invite=...).
export default function PlaygroundPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-playground">
      <PageHeader title="AI Playground" description="15 minutes of hands-on discovery" kind="home" />
      <PlaygroundProgram />
      <BrandCredit />
    </main>
  );
}
