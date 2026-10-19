// The FACEMARK organiser workspace (backend competition/organiser.py). The server
// says which sections this account may use; every action inside a section goes
// to that tool's own endpoint, which checks its own role or scope again.

import { apiGet } from "@/lib/api";

export type OrganiserSection = "overview" | "scanning" | "qmeter" | "corrections" | "final" | "screens";
export const SECTIONS: OrganiserSection[] = ["overview", "scanning", "qmeter", "corrections", "final", "screens"];

export type Workspace = {
  title: string;
  full_name: string;
  role: "ADMIN" | "VOLUNTEER" | "BOOTH_STAFF";
  game_master: boolean;
  sections: Record<OrganiserSection, boolean>;
  scanning_jobs: string[];
  panel_codes: boolean;
};

export const fetchWorkspace = () => apiGet<Workspace>("/api/staff/organiser");

export const SECTION_TITLE: Record<OrganiserSection, string> = {
  overview: "Overview",
  scanning: "Scanning",
  qmeter: "Qmeter",
  corrections: "Points corrections",
  final: "Final results",
  screens: "Venue screens",
};

// Why a section is not offered to this account (shown on the overview).
export const SECTION_NEEDS: Partial<Record<OrganiserSection, string>> = {
  qmeter: "Needs the Game Master scope.",
  corrections: "Administrators only.",
  final: "Needs the Game Master scope or an administrator.",
};
