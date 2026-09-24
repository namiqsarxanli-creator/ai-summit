import type { Metadata } from "next";
import { PwaRegistration } from "@/components/ui/PwaRegistration";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "FACEMARK · AI Summit", template: "%s" },
  description: "Connect with people and discover the booths that match you at AI Summit.",
  manifest: "/manifest.webmanifest",
  applicationName: "FACEMARK",
  appleWebApp: { capable: true, statusBarStyle: "black-translucent", title: "FACEMARK" },
  icons: { icon: [{ url: "/icon-192.png", sizes: "192x192", type: "image/png" }, { url: "/icon-512.png", sizes: "512x512", type: "image/png" }], apple: "/icon-192.png" },
};

export const viewport = { width: "device-width", initialScale: 1, viewportFit: "cover" as const, themeColor: "#080f1d" };

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="min-h-screen text-slate-50 antialiased"><a href="#main-content" className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded focus:bg-cyan-300 focus:p-3 focus:text-slate-950">Skip to content</a>{children}<PwaRegistration /></body>
    </html>
  );
}
