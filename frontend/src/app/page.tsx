import { SessionPanel } from "@/components/auth/SessionPanel";
import { HomeDashboard } from "@/components/dashboard/HomeDashboard";

export default function Home() {
  return (
    <main id="main-content" className="mx-auto flex min-h-screen max-w-lg flex-col gap-7 px-5 pb-28 pt-10 sm:pt-16">
      <header className="space-y-3">
        <p className="text-xs font-bold uppercase tracking-[0.3em] text-cyan-300">AI Summit · Connect</p>
        <h1 className="text-4xl font-bold tracking-tight text-white">FACEMARK</h1>
        <p className="max-w-sm text-base leading-relaxed text-slate-300">Your people, your discoveries, your summit. Start here.</p>
      </header>
      <SessionPanel />
      <HomeDashboard />
    </main>
  );
}
