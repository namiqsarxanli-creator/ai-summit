import type { Metadata } from "next";

import { BrandCredit } from "@/components/ui/BrandCredit";

export const metadata: Metadata = { title: "Admin", robots: { index: false, follow: false } };

// The administration console keeps its dark surface; the maker credit sits on a
// light plate so it stays legible on it.
export default function AdminLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <>
      {children}
      <div className="fm-credit-plate">
        <BrandCredit />
      </div>
    </>
  );
}
