import Link from "next/link";
import { T } from "./LanguageProvider";
import { HomeIcon } from "./HomeIcons";

export function PageHeader({ title, description, backHref = "/", kind = "booths" }: {
  title: string; description?: string; backHref?: string;
  kind?: "booths" | "qr" | "leaderboard" | "profile" | "home";
}) {
  return (
    <header className={"fm-page-header fm-page-header-" + kind}>
      <div className="fm-page-topline">
        <Link href={backHref} className="fm-back"><span aria-hidden="true">‹</span><T id="Back" /></Link>
        <Link href="/" className="fm-wordmark">AI Summit</Link>
      </div>
      <div className="fm-page-heading">
        <span className="fm-page-symbol"><HomeIcon name={kind} /></span>
        <h1><T id={title} /></h1>
        {description && <p><T id={description} /></p>}
      </div>
    </header>
  );
}
