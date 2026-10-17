"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";
import Link from "next/link";
import { useEffect, useState } from "react";

import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import type { ApiResult } from "@/lib/api";
import type { PhotoUrls } from "@/lib/avatar";
import { messageFor } from "@/lib/errors";
import { CARD_GROUPS, labelOf, selectedIn } from "@/lib/interestCard";
import { fetchProfile, type AttendeeProfile } from "@/lib/profile";
import { logout } from "@/lib/session";

import { ConsentControl } from "./ConsentControl";
import { PhotoControl } from "./PhotoControl";
import { ProfileEditor } from "./ProfileEditor";

type State =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "ok"; profile: AttendeeProfile }
  | { kind: "error"; code: string };

function stateFrom(result: ApiResult<AttendeeProfile>): State {
  if (result.ok) return { kind: "ok", profile: result.data };
  const { code } = result.error;
  return code === "UNAUTHENTICATED" || code === "REGISTRATION_NOT_VERIFIED"
    ? { kind: "signed-out" }
    : { kind: "error", code };
}

// M08's Profile surface: "registration data, selected interests,
// consent state (private view only)" - and, since attendees run their own
// profile, the ways to change it: edit details and interests (with a version
// check against a second device), a profile photo, and sponsor consent as its own
// choice. Every value comes from the session-scoped `/api/attendees/me/profile`
// response - nothing here is computed, and nothing here is shown to anyone but the
// signed-in attendee themselves.
export function ProfileView() {
  const { t } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchProfile().then((result) => {
      if (!cancelled) setState(stateFrom(result));
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function onSignOut() {
    setSigningOut(true);
    setLogoutError(null);
    const result = await logout();
    if (result.ok) {
      setEditing(false);
      setState(stateFrom(await fetchProfile()));
    } else {
      setLogoutError(t(messageFor(result.error.code)));
    }
    setSigningOut(false);
  }

  function patch(update: (profile: AttendeeProfile) => AttendeeProfile) {
    setState((current) => (current.kind === "ok" ? { kind: "ok", profile: update(current.profile) } : current));
  }

  function signedOut() {
    setEditing(false);
    setState({ kind: "signed-out" });
  }

  if (state.kind === "loading") {
    return <p role="status" className="fm-note">{t("Loading your profile…")}</p>;
  }

  if (state.kind === "signed-out") {
    return (
      <div className="fm-stack">
        <p className="fm-note">{t("Sign in to see your profile.")}</p>
        <Link href="/login" className="fm-btn fm-btn-primary fm-btn-block">
          {t("Sign in")}
        </Link>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <p role="alert" className="fm-alert fm-alert-warn">
        {t(messageFor(state.code))}
      </p>
    );
  }

  const { profile } = state;
  return (
    <div className="fm-stack-lg">
      <section className="fm-profile-card">
        <PhotoControl
          photo={profile.photo}
          onChanged={(photo: PhotoUrls | null) => patch((p) => ({ ...p, photo }))}
          onSignedOut={signedOut}
        />
        {editing ? (
          <div className="fm-stack" style={{ marginTop: 20, textAlign: "left" }}>
            <ProfileEditor
              profile={profile}
              onBaseline={(latest) => patch((p) => ({ ...latest, photo: p.photo }))}
              onSaved={(next) => {
                patch((p) => ({ ...next, photo: p.photo }));
                setEditing(false);
                setSaved(true);
              }}
              onCancel={() => setEditing(false)}
              onSignedOut={signedOut}
            />
          </div>
        ) : (
          <>
            <h2 className="fm-profile-name" style={{ marginTop: 16 }}>
              {profile.first_name} {profile.last_name}
            </h2>
            <p className="fm-small">{profile.profession}</p>
            <dl className="fm-dl">
              {profile.company && <ProfileRow label={t("Company")} value={profile.company} />}
              <ProfileRow label={t("Email")} value={profile.email} />
              <ProfileRow label={t("Phone")} value={profile.phone} />
              {profile.linkedin_url && <ProfileRow label={t("LinkedIn")} value={profile.linkedin_url} />}
            </dl>
            {saved && (
              <p role="status" className="fm-status fm-status-ok" style={{ marginTop: 12 }}>
                {t("Profile saved.")}
              </p>
            )}
            <button
              type="button"
              onClick={() => {
                setSaved(false);
                setEditing(true);
              }}
              className="fm-btn fm-btn-secondary fm-btn-block" style={{ marginTop: 16 }}
            >
              {t("Edit profile")}
            </button>
          </>
        )}
      </section>

      {!editing && (
        <section className="fm-section" aria-label={t("Interest card")}>
          <h2 className="fm-eyebrow">{t("Interest card")}</h2>
          <p className="fm-hint">{t("Optional. Kept on your private profile only: it is not shown on your contact card and does not change your booth recommendations.")}</p>
          <dl className="fm-dl">
            {CARD_GROUPS.map((group) => {
              const chosen = selectedIn(profile.interest_card, group.key);
              return (
                <div key={group.key}>
                  <dt>{t(group.title)}</dt>
                  <dd>
                    {chosen.length === 0
                      ? t("Not answered")
                      : chosen
                          .map((id) =>
                            group.options.some((o) => o.id === id)
                              ? t(labelOf(group.key, id))
                              : `${id} (${t("no longer offered")})`,
                          )
                          .join(", ")}
                  </dd>
                </div>
              );
            })}
          </dl>
        </section>
      )}

      {!editing && (
        <section className="fm-section">
          <h2 className="fm-eyebrow">{t("Your interests")}</h2>
          <p className="fm-hint">{t("These shape your booth recommendations and appear on your contact card. They are separate from the interest card.")}</p>
          {profile.interests.length === 0 ? (
            <p className="fm-empty">{t("You haven't selected any interests yet.")}</p>
          ) : (
            <ul aria-label={t("Your interests")} className="fm-chips">
              {profile.interests.map((interest) => (
                <li key={interest.slug} className="fm-chip">
                  {t(interest.name)}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <ConsentControl
        consent={profile}
        onChanged={(consent) => patch((p) => ({ ...p, ...consent }))}
        onSignedOut={signedOut}
      />

      {logoutError && (
        <p role="alert" className="fm-alert fm-alert-error">
          {logoutError}
        </p>
      )}
      <button
        type="button"
        onClick={onSignOut}
        disabled={signingOut}
        className="fm-btn fm-btn-secondary fm-btn-block"
      >
        {signingOut ? t("Signing out…") : t("Sign out")}
      </button>

      <AttendeeTabBar active="profile" />
    </div>
  );
}

function ProfileRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
