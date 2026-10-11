"use client";
import { useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { fetchSession } from "@/lib/session";
import { useLanguage } from "@/components/ui/LanguageProvider";
export function AttendeeLoginGate({children}:{children:ReactNode}) {
  const router = useRouter(); const {t} = useLanguage(); const [ready,setReady] = useState(false);
  useEffect(()=>{let active=true;void fetchSession().then(state=>{if(!active)return;if(state.kind==="signed-in")router.replace("/");else setReady(true);});return()=>{active=false;};},[router]);
  return ready ? children : <p role="status" className="fm-note">{t("Checking your session…")}</p>;
}
