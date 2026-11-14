import type { NextConfig } from "next";

// ARCHITECTURE.md §3.2 — verbatim rewrite. The browser only ever talks
// to this Next.js origin; requests to /api/* are proxied server-side
// to the backend, so there is no CORS preflight to misconfigure and
// session cookies can use SameSite=Lax instead of SameSite=None.
//
// BACKEND_INTERNAL_URL is the backend's own base URL as reached from
// inside the frontend container (e.g. http://backend:8000/api — see
// docker-compose.yml and .env.example). It deliberately includes the
// /api segment so FastAPI's own routes (GET /api/health, and every
// endpoint documented in ARCHITECTURE.md) match what the browser sees,
// with no prefix stripped or added on either side of the proxy.
const nextConfig: NextConfig = {
  devIndicators: false,

  // The production image (Dockerfile.prod) builds a self-contained server
  // with only the files the app actually needs. Opt-in, so `next dev` and
  // the development container are unaffected.
  output: process.env.NEXT_OUTPUT === "standalone" ? "standalone" : undefined,
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.BACKEND_INTERNAL_URL}/:path*`,
      },
    ];
  },

  // §21.11: card pages send `Referrer-Policy: no-referrer`, so the token
  // in the path does not leak to LinkedIn on tap; §9.5: `noindex`.
  async headers() {
    return [
      // The page backgrounds (8 Nov 2026: 1.4 MB PNGs became small WebP files). A
      // new picture gets a new file name, so a week in the browser is safe.
      ...["/summit-hero.webp", "/summit-concept.webp"].map((source) => ({
        source,
        headers: [{ key: "Cache-Control", value: "public, max-age=604800, stale-while-revalidate=86400" }],
      })),
      {
        source: "/phone-ca.mobileconfig",
        headers: [
          { key: "Content-Type", value: "application/x-apple-aspen-config" },
          { key: "Cache-Control", value: "no-store" },
        ],
      },
      {
        source: "/c/:token*",
        headers: [
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Robots-Tag", value: "noindex" },
          { key: "Cache-Control", value: "no-store" },
        ],
      },
    ];
  },

  // §21.11: a card token in a request log is a capability in plain
  // text, and so is a CSV export's one-time download ticket
  // (`/api/admin/exports/download?ticket=...`). Next.js logs incoming
  // requests in development only, and can only omit a request from that
  // log, not mask it — so these requests are omitted (the backend and the
  // proxy mask their own logs). The patterns are tested against the raw
  // request URL, so they also match when letters, `/`, `?` or `=` arrive
  // percent-encoded.
  logging: {
    incomingRequests: {
      ignore: [
        /\/c\//,
        /\/api\/cards\//,
        // every export endpoint, spelled any way the URL can spell it
        anyEncoding("api/admin/exports"),
        // ... and, whatever the path, a `ticket` query parameter
        new RegExp(`(?:[?&;]|%3f|%26)${anyEncoding("ticket").source}(?:=|%3d)`, "i"),
      ],
    },
  },
};

// A pattern for `text` that matches however any character of it may be
// percent-encoded (`%74icket`, `%54ICKET`, `api%2Fadmin`), case-insensitively.
function anyEncoding(text: string): RegExp {
  const piece = (char: string): string => {
    const forms = new Set<string>([char.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")]);
    for (const variant of new Set([char.toLowerCase(), char.toUpperCase()])) {
      forms.add(`%${variant.charCodeAt(0).toString(16).padStart(2, "0")}`);
    }
    return `(?:${[...forms].join("|")})`;
  };
  return new RegExp([...text].map(piece).join(""), "i");
}

export default nextConfig;
