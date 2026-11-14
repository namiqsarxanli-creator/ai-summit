"use client";

import { useEffect, useState } from "react";

import { fetchProfile } from "@/lib/profile";

// Whether the signed-in attendee has picked topics on their interest card:
// true / false, or null while unknown (not loaded, or the profile could not be
// read). A booth listing without any match means "pick topics" only when the
// answer is false; with topics picked it means the booths have no interest
// profile yet.
export function useCardTopics(): boolean | null {
  const [has, setHas] = useState<boolean | null>(null);
  useEffect(() => {
    let active = true;
    void fetchProfile().then((r) => {
      if (active && r.ok) setHas((r.data.interest_card?.topics ?? []).length > 0);
    });
    return () => { active = false; };
  }, []);
  return has;
}
