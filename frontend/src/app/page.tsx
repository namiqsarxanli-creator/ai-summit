import Link from "next/link";
import { T } from "@/components/ui/LanguageProvider";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { SessionPanel } from "@/components/auth/SessionPanel";
import { HomeDashboard } from "@/components/dashboard/HomeDashboard";

export default function Home() {
  return (
    <main id="main-content" className="fm-home">
      <header className="fm-hero">
        <div className="fm-hero-art" aria-hidden="true" />
        <div className="fm-hero-content">
          <div className="fm-hero-brand">
            <h1>AI Summit</h1>
            <p className="fm-hero-tagline"><T id="People. Ideas. Real impact." /></p>
          </div>
          <div className="fm-hero-note"><p><T id="Artificial intelligence opens bigger possibilities." /></p><span /><small><T id="Let’s build the future together." /></small></div>
        </div>
      </header>
      <div className="fm-home-content">
        <HomeDashboard />
        <SessionPanel />
        <Link href="/staff/login" className="fm-staff-entry"><T id="Staff sign in" /> <span aria-hidden="true">→</span></Link>
        <BrandCredit />
      </div>
    </main>
  );
}
