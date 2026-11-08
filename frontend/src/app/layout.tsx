import type { Metadata } from "next";
import { ConfirmHost } from "@/components/ui/ConfirmHost";
import { PwaRegistration } from "@/components/ui/PwaRegistration";
import { LanguageProvider, LanguageSwitcher, T } from "@/components/ui/LanguageProvider";
import { BRAND, SITE_DESCRIPTION, siteOrigin } from "@/lib/site";
import "./globals.css";

// Page titles are "<page> · AI Summit" (each page sets only its own part; the
// browser tab then shows it translated - see DocumentTitle in LanguageProvider).
// The Open Graph / Twitter tags give a shared link a title, text and picture
// (WhatsApp, Telegram, LinkedIn...). They need absolute URLs, hence metadataBase.
export const metadata: Metadata = {
  metadataBase: new URL(siteOrigin()),
  title: { default: BRAND, template: `%s · ${BRAND}` },
  description: SITE_DESCRIPTION,
  manifest: "/manifest.webmanifest",
  applicationName: BRAND,
  appleWebApp: { capable: true, statusBarStyle: "black-translucent", title: BRAND },
  openGraph: {
    type: "website",
    siteName: BRAND,
    title: BRAND,
    description: SITE_DESCRIPTION,
    url: "/",
    images: [{ url: "/og-image.jpg", width: 1200, height: 630, alt: BRAND }],
  },
  twitter: { card: "summary_large_image", title: BRAND, description: SITE_DESCRIPTION, images: ["/og-image.jpg"] },
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "any" },
      { url: "/app-icon-192.png", sizes: "192x192", type: "image/png" },
      { url: "/app-icon-512.png", sizes: "512x512", type: "image/png" },
    ],
    apple: { url: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" },
  },
};

export const viewport = { width: "device-width", initialScale: 1, viewportFit: "cover" as const, themeColor: "#ffffff" };

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased"><LanguageProvider><a href="#main-content" className="fm-skip"><T id="Skip to content" /></a><LanguageSwitcher />{children}<ConfirmHost /><PwaRegistration /></LanguageProvider></body>
    </html>
  );
}
