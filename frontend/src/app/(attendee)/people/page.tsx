import type { Metadata } from "next";

import { MentorGroups } from "@/components/mentors/MentorGroups";
import { PairPhotos } from "@/components/people/PairPhotos";
import { PeopleHub } from "@/components/people/PeopleHub";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "People to meet",
};

// Reached from Home. Compatible people, Match Corner meetings, connections and
// the Meet the Mentor groups of the Connection Hub.
export default function PeoplePage() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="People to meet" description="Your best matches, meetings and connections" kind="profile" />
      <PeopleHub />
      <MentorGroups />
      <PairPhotos />
      <BrandCredit />
    </main>
  );
}
