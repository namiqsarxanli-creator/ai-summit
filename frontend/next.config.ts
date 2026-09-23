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
      {
        source: "/c/:token*",
        headers: [
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Robots-Tag", value: "noindex" },
        ],
      },
    ];
  },

  // §21.11: a card token in a request log is a capability in plain
  // text. Next.js logs incoming requests in development only, and can
  // only omit a request from that log, not mask it — so card requests
  // are omitted (the backend masks its own access log as /c/***).
  logging: {
    incomingRequests: { ignore: [/\/c\//, /\/api\/cards\//] },
  },
};

export default nextConfig;
