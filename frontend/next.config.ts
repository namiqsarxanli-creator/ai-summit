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
};

export default nextConfig;
