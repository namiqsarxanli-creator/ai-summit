"use client";

import { useEffect, useState } from "react";

import { SessionPanel } from "@/components/auth/SessionPanel";

// Matches the documented backend contract exactly (ARCHITECTURE.md /
// IMPLEMENTATION_PLAN.md M01): GET /api/health -> {"status": "ok"}.
type HealthResponse = {
  status: string;
};

type HealthState =
  | { kind: "loading" }
  | { kind: "ok"; body: HealthResponse }
  | { kind: "error"; message: string };

function useBackendHealth(): HealthState {
  const [state, setState] = useState<HealthState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;

    // A plain relative fetch. This is the actual end-to-end proof of
    // ARCHITECTURE.md §3.2: the browser makes a same-origin request to
    // this Next.js origin, which the rewrite in next.config.ts forwards
    // server-side to FastAPI. No backend origin is known to this code.
    fetch("/api/health")
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`backend responded with HTTP ${response.status}`);
        }
        return (await response.json()) as HealthResponse;
      })
      .then((body) => {
        if (!cancelled) setState({ kind: "ok", body });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          const message = error instanceof Error ? error.message : "unknown error";
          setState({ kind: "error", message });
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return state;
}

export default function Home() {
  const health = useBackendHealth();

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-6 px-6 text-center">
      <h1 className="text-3xl font-semibold tracking-tight text-slate-50">FACEMARK</h1>
      <p className="text-sm text-slate-400">AI Summit networking — attendee registration is open.</p>

      <SessionPanel />

      <div className="w-full rounded-lg border border-slate-800 bg-slate-900 p-4 text-left text-sm">
        <p className="mb-2 font-medium text-slate-300">Backend connectivity (/api/health)</p>
        {health.kind === "loading" && <p className="text-slate-500">Checking…</p>}
        {health.kind === "ok" && (
          <pre className="rounded bg-slate-950 p-2 text-emerald-400">
            {JSON.stringify(health.body, null, 2)}
          </pre>
        )}
        {health.kind === "error" && (
          <p className="text-red-400">Unreachable: {health.message}</p>
        )}
      </div>
    </main>
  );
}
