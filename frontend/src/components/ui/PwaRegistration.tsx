"use client";

import { useEffect } from "react";

export function PwaRegistration() {
  useEffect(() => {
    if (!window.isSecureContext || !("serviceWorker" in navigator)) return;
    const register = () => { void navigator.serviceWorker.register("/sw.js").catch(() => {}); };
    if (document.readyState === "complete") register();
    else window.addEventListener("load", register);
    return () => window.removeEventListener("load", register);
  }, []);
  return null;
}
