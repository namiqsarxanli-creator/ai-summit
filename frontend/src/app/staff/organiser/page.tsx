import { redirect } from "next/navigation";

// The organiser panel moved to its own section, /facemark; links to the old
// address keep working.
export default function OldOrganiserAddress() {
  redirect("/facemark");
}
