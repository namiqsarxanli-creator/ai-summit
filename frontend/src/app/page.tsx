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
            {/* The event's name: the same in every language, so not translated. */}
            <p className="fm-hero-tagline">Beyond Now</p>
          </div>
        </div>
      </header>
      <div className="fm-home-content">
        <HomeDashboard />
        <SessionPanel />
        <BrandCredit />
      </div>
    </main>
  );
}
