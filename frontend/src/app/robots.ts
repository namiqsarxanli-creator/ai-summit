import type { MetadataRoute } from "next";

// §9.5: "Card URLs are excluded from crawling via robots.txt" — a card
// URL's path is its token. The API is never meant for crawlers either, nor
// are the staff, admin and FACEMARK entrances, whose links are handed out directly.
export default function robots(): MetadataRoute.Robots {
  return {
    rules: { userAgent: "*", disallow: ["/c/", "/api/", "/staff", "/admin", "/facemark"] },
  };
}
