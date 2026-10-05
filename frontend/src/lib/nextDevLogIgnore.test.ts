import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";

// Next.js's development server prints every incoming request and can only
// OMIT a request from that log (it cannot mask one). These are the patterns the
// app configured for that; they are tested against the raw request URL, exactly
// as Next does (`pattern.test(request.url)`).
const ignore = nextConfig.logging && nextConfig.logging.incomingRequests
  ? (nextConfig.logging.incomingRequests as { ignore: RegExp[] }).ignore
  : [];
const ignored = (url: string) => ignore.some((pattern) => pattern.test(url));

describe("Next.js development request log", () => {
  const T = "etj4pHPLxJKn2qL-eoygsZT94gIEyYHdkxdC4LG_8qA";

  it.each([
    `/api/admin/exports/download?ticket=${T}`,
    `/api/admin/exports/download?x=1&ticket=${T}`,
    `/api/admin/exports/download?ticket=${T}&x=1`,
    `/api/admin/exports/download?a=1&b=2&ticket=${T}&c=3`,
    `/api/admin/exports/download?%74icket=${T}`,
    `/api/admin/exports/download?t%69cket=${T}&x=1`,
    `/api/admin/exports/download?%54%49%43%4B%45%54=${T}`,
    `/api/admin/exports/download?%74%69%63%6b%65%74=${T}`,
    `/api/admin/exports/download?TICKET=${T}`,
    `/api/admin/exports/download?ticket%3D${T}`,
    `/api/admin/exports/download?ticket=${T}&ticket=${T}`,
    `/api/admin/exports/download?x=1;ticket=${T}`,
    // the path itself spelled with encoded characters
    `/api/admin/exp%6frts/download?ticket=${T}`,
    `/api%2Fadmin%2Fexports/download?ticket=${T}`,
    `/%61pi/admin/exports/download?ticket=${T}`,
    `/API/ADMIN/EXPORTS/download?ticket=${T}`,
    // a ticket on any other path
    `/somewhere/else?ticket=${T}`,
    `/login?next=%2Fapi%2Fadmin%2Fexports%2Fdownload%3Fticket%3D${T}`,
    // the other export endpoints are quiet too
    "/api/admin/exports/attendees",
    "/api/admin/exports/status/abcDEF123456",
  ])("keeps %s out of the log", (url) => {
    expect(ignored(url)).toBe(true);
  });

  it.each([
    "/c/abcdef",
    "/api/cards/abcdef",
  ])("still keeps card links out of the log: %s", (url) => {
    expect(ignored(url)).toBe(true);
  });

  it.each([
    "/admin",
    "/api/admin/attendees?q=ticket&limit=50",
    "/api/admin/attendees?q=ticket%20desk",
    "/api/admin/points-log?ticketing=1",
    "/api/admin/points-log?my_ticket=1",
    "/api/health",
    "/_next/static/chunks/main.js",
    "/booths",
  ])("does not silence ordinary requests: %s", (url) => {
    expect(ignored(url)).toBe(false);
  });
});
