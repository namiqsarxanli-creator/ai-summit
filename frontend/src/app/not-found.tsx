import Link from "next/link";
import type { Metadata } from "next";

import { BrandCredit } from "@/components/ui/BrandCredit";
import { T } from "@/components/ui/LanguageProvider";
import { PageHeader } from "@/components/ui/PageHeader";

// Any URL that matches no page (a mistyped link, an old bookmark). Rendered inside
// the root layout, so it gets the language switcher and the light event design; the
// response status stays 404.
export const metadata: Metadata = {
  title: "Page not found",
};

export default function NotFound() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="Page not found" description="This page does not exist or has moved." kind="home" />
      <Link href="/" className="fm-btn fm-btn-primary fm-btn-block">
        <T id="Back to home" />
      </Link>
      <BrandCredit />
    </main>
  );
}
