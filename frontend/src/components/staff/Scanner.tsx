"use client";

// M10 scan/confirm + M11 award — ARCHITECTURE.md §13.1.
//
// State machine, one state kind at a time:
//   camera        - live preview, decoding; the mandatory default path
//   resolving     - POST /api/staff/scan/resolve in flight
//   resolve-error - INVALID_QR / rate-limited / etc — offers rescan
//   confirm       - MANDATORY "Attendee found..." [Cancel] [Confirm]
//                   (§13.1 step 5) — the only way out of "camera" that
//                   doesn't return straight to it
//   awarding      - the booth's own activities from GET /api/staff/tasks,
//                   each showing the points the SERVER will award
//   review        - who / booth / activity / exact points, and one
//                   explicit "Award +N" (the client sends no points, only
//                   the rule revision it is confirming). If the server
//                   answers TASK_RULE_CHANGED nothing was paid: this screen
//                   comes back with the NEW rule, a notice, and a new key
//   award-pending - POST /api/staff/points/award in flight; the screen
//                   has no button left, so a second tap cannot fire
//   award-success - the server's resulting_total, never computed here;
//                   another activity for the same scan is one tap away
//   award-error   - a lost response/transient failure offers "Try again"
//                   with the SAME idempotency key (never a double
//                   payment); a refused activity offers "Choose another"
//   manual-entry  - reachable ONLY from a camera failure (§13.5, §24.21)
//
// The camera is only ever mounted while state.kind === "camera": moving
// to any other state stops every track (via useCameraScanner's own
// cleanup) and makes a second decode of the same frame impossible.
import { useEffect, useRef, useState } from "react";
import { useLanguage } from "@/components/ui/LanguageProvider";

import { awardPoints, type AwardResponse } from "@/lib/award";
import { messageFor } from "@/lib/errors";
import { resolveScan, tokenFromScannedText, type ScanResolveResult } from "@/lib/scanner";
import { fetchStaffMe, fetchStaffTasks, type StaffMe, type StaffTask } from "@/lib/staff";

import { useCameraScanner } from "./useCameraScanner";

type TasksState =
  | { kind: "loading" }
  | { kind: "none" } // admins: nothing to award
  | { kind: "ok"; tasks: StaffTask[] }
  | { kind: "error"; code: string };

type Held = { identity: ScanResolveResult; cardToken: string };

type ScanState =
  | { kind: "camera" }
  | { kind: "manual-entry" }
  | { kind: "resolving" }
  | { kind: "resolve-error"; code: string }
  | ({ kind: "confirm" } & Held)
  | ({ kind: "awarding"; tasks: TasksState } & Held)
  | ({
      kind: "review";
      task: StaffTask;
      tasks: StaffTask[];
      idempotencyKey: string;
      // Set when the server refused the previous confirmation because the rule
      // changed: what the staff member had confirmed before.
      changedFrom?: { points: number; repeatable: boolean };
    } & Held)
  | ({ kind: "award-pending"; task: StaffTask; tasks: StaffTask[]; idempotencyKey: string } & Held)
  | ({ kind: "award-success"; result: AwardResponse; tasks: StaffTask[] } & Held)
  | ({
      kind: "award-error";
      code: string;
      task: StaffTask;
      tasks: StaffTask[];
      idempotencyKey: string;
    } & Held);

type MeState =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "error"; code: string }
  | { kind: "ok"; me: StaffMe };

function isSignedOut(code: string): boolean {
  return code === "UNAUTHENTICATED" || code === "STAFF_NOT_AUTHORIZED";
}

function newIdempotencyKey(): string {
  // §13.4: one key per logical award, generated when the review screen first
  // opens for an activity RULE during a scan — never per click. Retrying the
  // same confirmation after a lost response re-sends THIS key, so the server
  // replays the original result instead of paying twice; choosing a different
  // activity, or confirming a changed rule, is a different award and gets its
  // own key.
  return crypto.randomUUID();
}

// The rule the server says is in force now, from a TASK_RULE_CHANGED answer.
// Anything malformed yields null (the caller falls back to reloading the list).
function ruleFromDetails(details: Record<string, unknown>, task: StaffTask): StaffTask | null {
  const { task_id, task_title, points, repeatable, rule_revision } = details;
  if (
    task_id !== task.id ||
    typeof points !== "number" ||
    typeof repeatable !== "boolean" ||
    typeof rule_revision !== "string" ||
    rule_revision === ""
  ) {
    return null;
  }
  return {
    ...task,
    title: typeof task_title === "string" ? task_title : task.title,
    points,
    repeatable,
    rule_revision,
  };
}

// Failures where trying the very same award again is the right move: the
// request may or may not have been processed, and the unchanged idempotency
// key makes a repeat safe.
const RETRYABLE = new Set(["NETWORK_ERROR", "INTERNAL_ERROR", "AWARD_IN_PROGRESS", "MAINTENANCE"]);
// Failures that mean "this activity cannot be awarded to this attendee" while
// the scan itself is still good: offer the list again.
const CHOOSE_ANOTHER = new Set([
  "DUPLICATE_TASK_REWARD",
  "TASK_NOT_FOUND",
  "TASK_INACTIVE",
  "TASK_NOT_CONFIGURED",
  "TASK_RULE_CHANGED",
  "IDEMPOTENCY_KEY_REUSE",
]);
// This page is older than the server's award contract. Nothing was paid and no
// retry can succeed: the only remedy is loading the current page.
const RELOAD_REQUIRED = new Set(["CSRF_REJECTED"]);

export function Scanner() {
  const { t } = useLanguage();
  const [me, setMe] = useState<MeState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchStaffMe().then((result) => {
      if (cancelled) return;
      if (!result.ok) {
        setMe(
          isSignedOut(result.error.code)
            ? { kind: "signed-out" }
            : { kind: "error", code: result.error.code },
        );
        return;
      }
      setMe({ kind: "ok", me: result.data });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (me.kind === "loading") {
    return <p role="status" className="fm-note">{t("Checking your session…")}</p>;
  }
  if (me.kind === "signed-out") {
    return <p className="fm-alert fm-alert-warn">{t("Please sign in as staff to scan attendees.")}</p>;
  }
  if (me.kind === "error") {
    return (
      <p role="alert" className="fm-alert fm-alert-warn">
        {t(messageFor(me.code))}
      </p>
    );
  }

  return <ScanFlow role={me.me.role} boothName={me.me.booth_name} />;
}

function ScanFlow({ role, boothName }: { role: StaffMe["role"]; boothName: string | null }) {
  const { t } = useLanguage();
  const [state, setState] = useState<ScanState>({ kind: "camera" });

  // A ref, kept in sync by `transition` below (every state change in
  // this component goes through it) rather than by `state` itself: two
  // events (a decode, or two taps) can arrive synchronously, before a
  // `setState` from the first has flushed, and checking `state` directly
  // would let both pass. Writing the ref in the same statement as each
  // `setState` means the second call always sees the first transition.
  const stateRef = useRef<ScanState>({ kind: "camera" });
  // One idempotency key per activity RULE (id + confirmed revision) for the
  // current scan: a network retry finds its key again, a changed rule gets a
  // new one.
  const keysRef = useRef<Map<string, string>>(new Map());
  // Identifies the latest "load the activities" request, so a slow answer
  // for an earlier screen cannot overwrite a newer one.
  const loadRef = useRef(0);

  function transition(next: ScanState) {
    stateRef.current = next;
    setState(next);
  }

  function handleDetected(text: string) {
    // Both "camera" (a live decode) and "manual-entry" (the fallback
    // form's submit) are valid sources for this — anything else means a
    // scan is already resolving or already resolved, so a stray or
    // duplicate call is ignored rather than starting a second lookup.
    if (stateRef.current.kind !== "camera" && stateRef.current.kind !== "manual-entry") return;
    const token = tokenFromScannedText(text);
    if (!token) {
      transition({ kind: "resolve-error", code: "INVALID_QR" });
      return;
    }
    transition({ kind: "resolving" });
    resolveScan(token).then((result) => {
      if (!result.ok) {
        transition({ kind: "resolve-error", code: result.error.code });
        return;
      }
      keysRef.current = new Map(); // a new scan never inherits another's keys
      transition({ kind: "confirm", identity: result.data, cardToken: token });
    });
  }

  function rescan() {
    transition({ kind: "camera" });
  }

  function enterAwarding(held: Held) {
    const load = ++loadRef.current;
    transition({ kind: "awarding", ...held, tasks: { kind: role === "BOOTH_STAFF" ? "loading" : "none" } });
    if (role !== "BOOTH_STAFF") return;
    fetchStaffTasks().then((result) => {
      if (load !== loadRef.current) return;
      setState((current) => {
        if (current.kind !== "awarding") return current;
        const next: ScanState = {
          ...current,
          tasks: result.ok
            ? { kind: "ok", tasks: result.data }
            : { kind: "error", code: result.error.code },
        };
        stateRef.current = next;
        return next;
      });
    });
  }

  function confirmIdentity() {
    if (stateRef.current.kind !== "confirm") return;
    enterAwarding({ identity: stateRef.current.identity, cardToken: stateRef.current.cardToken });
  }

  function keyFor(task: StaffTask): string {
    const id = `${task.id}:${task.rule_revision}`;
    let key = keysRef.current.get(id);
    if (key === undefined) {
      key = newIdempotencyKey();
      keysRef.current.set(id, key);
    }
    return key;
  }

  function chooseTask(task: StaffTask) {
    const current = stateRef.current;
    if (current.kind !== "awarding" || current.tasks.kind !== "ok") return;
    transition({
      kind: "review",
      identity: current.identity,
      cardToken: current.cardToken,
      task,
      tasks: current.tasks.tasks,
      idempotencyKey: keyFor(task),
    });
  }

  function backToList() {
    const current = stateRef.current;
    if (
      current.kind !== "review" &&
      current.kind !== "award-error" &&
      current.kind !== "award-success"
    ) {
      return;
    }
    // Keep the list we already have: no spinner, no new request needed.
    transition({
      kind: "awarding",
      identity: current.identity,
      cardToken: current.cardToken,
      tasks: { kind: "ok", tasks: current.tasks },
    });
  }

  function reloadList() {
    const current = stateRef.current;
    if (current.kind !== "award-error") return;
    enterAwarding({ identity: current.identity, cardToken: current.cardToken });
  }

  function submitAward() {
    // The only way to send an award. It leaves "review"/"award-error" at
    // once, so a second tap (or a double-tap) finds a different state and
    // does nothing: two taps can never fire two requests.
    const current = stateRef.current;
    if (current.kind !== "review" && current.kind !== "award-error") return;
    const { identity, cardToken, task, tasks, idempotencyKey } = current;
    transition({ kind: "award-pending", identity, cardToken, task, tasks, idempotencyKey });
    awardPoints({
      card_token: cardToken,
      task_id: task.id,
      rule_revision: task.rule_revision,
      idempotency_key: idempotencyKey,
      scan_ref: identity.scan_ref,
    }).then((result) => {
      if (!result.ok) {
        if (result.error.code === "TASK_RULE_CHANGED") {
          // Nothing was paid. Show the rule as it stands now and ask again -
          // with a NEW key, because this is a new confirmation.
          const updated = ruleFromDetails(result.error.details, task);
          if (updated !== null) {
            transition({
              kind: "review",
              identity,
              cardToken,
              task: updated,
              tasks: tasks.map((entry) => (entry.id === updated.id ? updated : entry)),
              idempotencyKey: keyFor(updated),
              changedFrom: { points: task.points, repeatable: task.repeatable },
            });
            return;
          }
        }
        transition({
          kind: "award-error",
          identity,
          cardToken,
          task,
          tasks,
          idempotencyKey,
          code: result.error.code,
        });
        return;
      }
      transition({ kind: "award-success", identity, cardToken, tasks, result: result.data });
    });
  }

  const camera = useCameraScanner(state.kind === "camera", handleDetected);

  return (
    <div className="fm-stack-lg">
      {state.kind === "camera" && (
        <CameraView camera={camera} onManualEntry={() => transition({ kind: "manual-entry" })} />
      )}
      {state.kind === "manual-entry" && (
        <ManualEntry onSubmit={handleDetected} onBackToCamera={rescan} />
      )}

      {state.kind === "resolving" && <p role="status" className="fm-empty">{t("Looking up attendee…")}</p>}

      {state.kind === "resolve-error" && (
        <ErrorPanel code={state.code} onRetry={rescan} retryLabel="Scan again" />
      )}

      {state.kind === "confirm" && (
        <ConfirmScreen identity={state.identity} onCancel={rescan} onConfirm={confirmIdentity} />
      )}

      {state.kind === "awarding" && (
        <AwardingShell
          identity={state.identity}
          role={role}
          tasks={state.tasks}
          onChooseTask={chooseTask}
          onScanNext={rescan}
        />
      )}

      {state.kind === "review" && (
        <ReviewScreen
          identity={state.identity}
          boothName={boothName}
          task={state.task}
          changedFrom={state.changedFrom}
          onBack={backToList}
          onAward={submitAward}
        />
      )}

      {state.kind === "award-pending" && (
        <p role="status" className="fm-empty">
          {t("Awarding")} +{state.task.points} · {state.task.title}…
        </p>
      )}

      {state.kind === "award-success" && (
        <AwardSuccess
          identity={state.identity}
          result={state.result}
          onAnother={backToList}
          onScanNext={rescan}
        />
      )}

      {state.kind === "award-error" && (
        <AwardError
          code={state.code}
          task={state.task}
          onRetry={submitAward}
          onChooseAnother={
            state.code === "TASK_INACTIVE" ||
            state.code === "TASK_NOT_FOUND" ||
            state.code === "TASK_NOT_CONFIGURED" ||
            state.code === "TASK_RULE_CHANGED"
              ? reloadList
              : backToList
          }
          onScanNext={rescan}
        />
      )}
    </div>
  );
}

function ErrorPanel({
  code,
  onRetry,
  retryLabel,
}: {
  code: string;
  onRetry: () => void;
  retryLabel: string;
}) {
  const { t } = useLanguage();
  return (
    <div className="fm-stack">
      <p role="alert" className="fm-alert fm-alert-warn">
        {t(messageFor(code))}
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="fm-btn fm-btn-primary fm-btn-block"
      >
        {t(retryLabel)}
      </button>
    </div>
  );
}

function CameraView({
  camera,
  onManualEntry,
}: {
  camera: ReturnType<typeof useCameraScanner>;
  onManualEntry: () => void;
}) {
  const { t } = useLanguage();
  const { videoRef, canvasRef, status, restart } = camera;

  return (
    <div className="fm-stack">
      <div className="fm-camera">
        <video
          ref={videoRef}
          autoPlay
          playsInline
          muted
          className={`fm-camera-video ${status.kind === "streaming" ? "" : "invisible"}`}
        />
        <canvas ref={canvasRef} className="hidden" aria-hidden />
        {status.kind === "starting" && (
          <p className="fm-camera-msg fm-camera-center">
            {t("Starting camera…")}
          </p>
        )}
      </div>

      {status.kind === "streaming" && (
        <p role="status" className="fm-note fm-center">{t("Point the camera at an attendee's QR code.")}</p>
      )}

      {status.kind === "denied" && (
        <CameraFailure
          title="Camera access was denied"
          detail="Allow camera access in your browser's site settings, then try again."
          onRetry={restart}
          onManualEntry={onManualEntry}
        />
      )}
      {status.kind === "unavailable" && (
        <CameraFailure
          title="No camera was found"
          detail="This device doesn't have a usable camera for scanning."
          onRetry={restart}
          onManualEntry={onManualEntry}
        />
      )}
      {status.kind === "error" && (
        <CameraFailure
          title="The camera couldn't be started"
          detail="Something went wrong starting the camera."
          onRetry={restart}
          onManualEntry={onManualEntry}
        />
      )}
    </div>
  );
}

function CameraFailure({
  title,
  detail,
  onRetry,
  onManualEntry,
}: {
  title: string;
  detail: string;
  onRetry: () => void;
  onManualEntry: () => void;
}) {
  const { t } = useLanguage();
  return (
    <div role="alert" className="fm-alert fm-alert-warn fm-stack">
      <p className="fm-strong">{t(title)}</p>
      <p>{t(detail)}</p>
      <div className="fm-stack" style={{ gap: 8 }}>
        <button
          type="button"
          onClick={onRetry}
          className="fm-btn fm-btn-primary fm-btn-block"
        >
          {t("Try camera again")}
        </button>
        <button
          type="button"
          onClick={onManualEntry}
          className="fm-btn fm-btn-secondary fm-btn-block"
        >
          {t("Enter code manually")}
        </button>
      </div>
    </div>
  );
}

function ManualEntry({
  onSubmit,
  onBackToCamera,
}: {
  onSubmit: (text: string) => void;
  onBackToCamera: () => void;
}) {
  const { t } = useLanguage();
  const [value, setValue] = useState("");
  return (
    <form
      className="fm-stack"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(value);
      }}
    >
      <p className="fm-eyebrow">
        {t("Manual entry — emergency fallback only")}
      </p>
      <label htmlFor="manual-card-token" className="fm-label">
        {t("Card code")}
      </label>
      <input
        id="manual-card-token"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="fm-input"
        placeholder={t("Paste the attendee's card code or URL")}
      />
      <button
        type="submit"
        className="fm-btn fm-btn-primary fm-btn-block"
      >
        {t("Look up attendee")}
      </button>
      <button type="button" onClick={onBackToCamera} className="fm-btn fm-btn-quiet fm-btn-block">
        {t("Back to camera")}
      </button>
    </form>
  );
}

function ConfirmScreen({
  identity,
  onCancel,
  onConfirm,
}: {
  identity: ScanResolveResult;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { t } = useLanguage();
  return (
    <div className="fm-card fm-stack">
      <p className="fm-eyebrow">{t("Attendee found")}</p>
      <p className="fm-h2">
        {identity.first_name} {identity.last_name}
      </p>
      <p className="fm-small">{identity.profession}</p>
      <p className="fm-text-soft">{t("Is this the correct attendee?")}</p>
      <div className="fm-row" style={{ flexWrap: "nowrap" }}>
        <button
          type="button"
          onClick={onCancel}
          className="fm-btn fm-btn-secondary fm-grow"
        >
          {t("Cancel")}
        </button>
        <button
          type="button"
          onClick={onConfirm}
          className="fm-btn fm-btn-primary fm-grow"
        >
          {t("Confirm")}
        </button>
      </div>
    </div>
  );
}

function AwardingShell({
  identity,
  role,
  tasks,
  onChooseTask,
  onScanNext,
}: {
  identity: ScanResolveResult;
  role: StaffMe["role"];
  tasks: TasksState;
  onChooseTask: (task: StaffTask) => void;
  onScanNext: () => void;
}) {
  const { t } = useLanguage();
  return (
    <div className="fm-card fm-stack">
      <p className="fm-eyebrow">{t("Confirmed")}</p>
      <p className="fm-h2">
        {identity.first_name} {identity.last_name}
      </p>

      {role !== "BOOTH_STAFF" && (
        <p className="fm-small">{t("Admins can't award points — booth staff can complete this scan.")}</p>
      )}

      {role === "BOOTH_STAFF" && tasks.kind === "loading" && (
        <p role="status" className="fm-note">{t("Loading activities…")}</p>
      )}
      {role === "BOOTH_STAFF" && tasks.kind === "error" && (
        <p role="alert" className="fm-alert fm-alert-warn">
          {t(messageFor(tasks.code))}
        </p>
      )}
      {role === "BOOTH_STAFF" && tasks.kind === "ok" && tasks.tasks.length === 0 && (
        <p className="fm-small">{t("No activities with a point value are set up for this booth yet.")}</p>
      )}
      {role === "BOOTH_STAFF" && tasks.kind === "ok" && tasks.tasks.length > 0 && (
        <>
          <p className="fm-text-soft">{t("Choose the activity to award:")}</p>
          <ul aria-label={t("Activities")} className="fm-stack" style={{ gap: 8 }}>
            {tasks.tasks.map((task) => (
              <li key={task.id}>
                <button
                  type="button"
                  onClick={() => onChooseTask(task)}
                  className="fm-btn fm-btn-secondary fm-btn-block fm-activity-choice"
                >
                  <span>{t(task.title)}</span>
                  <span className="text-lg font-bold">+{task.points}</span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      <button
        type="button"
        onClick={onScanNext}
        className="fm-btn fm-btn-secondary fm-btn-block"
      >
        {t("Scan another attendee")}
      </button>
    </div>
  );
}

// The final check before points are paid: who, at which booth, for what, and
// exactly how many points. Nothing here is editable - the value is the
// activity's server-side rule.
function ReviewScreen({
  identity,
  boothName,
  task,
  changedFrom,
  onBack,
  onAward,
}: {
  identity: ScanResolveResult;
  boothName: string | null;
  task: StaffTask;
  changedFrom?: { points: number; repeatable: boolean };
  onBack: () => void;
  onAward: () => void;
}) {
  const { t } = useLanguage();
  const rows: [string, string][] = [
    [t("Attendee"), `${identity.first_name} ${identity.last_name}`],
    [t("Booth"), boothName ? t(boothName) : "—"],
    [t("Activity"), t(task.title)],
  ];
  return (
    <div className="fm-card fm-stack">
      <p className="fm-eyebrow">{t("Review and award")}</p>
      {changedFrom && (
        <div role="alert" className="fm-alert fm-alert-warn">
          <p className="fm-strong">{t("This activity's rule changed. Nothing was awarded yet.")}</p>
          <p>
            {t("Before")}: +{changedFrom.points} · {t("Now")}: +{task.points}
            {changedFrom.repeatable !== task.repeatable
              ? ` · ${task.repeatable ? t("This activity can be earned more than once.") : t("This activity can be earned once per attendee.")}`
              : ""}
          </p>
          <p>{t("Check the new rule and confirm again.")}</p>
        </div>
      )}
      <dl className="fm-stack" style={{ gap: 8 }}>
        {rows.map(([label, value]) => (
          <div key={label} className="fm-row-between" style={{ alignItems: "flex-start" }}>
            <dt className="fm-small">{label}</dt>
            <dd className="fm-strong" style={{ textAlign: "right" }}>{value}</dd>
          </div>
        ))}
        <div className="fm-row-between" style={{ borderTop: "1px solid var(--fm-line)", paddingTop: 12 }}>
          <dt className="fm-small">{t("Points to award")}</dt>
          <dd className="fm-h2">+{task.points}</dd>
        </div>
      </dl>
      <p className="fm-micro">
        {task.repeatable ? t("This activity can be earned more than once.") : t("This activity can be earned once per attendee.")}
      </p>
      <div className="fm-row" style={{ flexWrap: "nowrap" }}>
        <button
          type="button"
          onClick={onBack}
          className="fm-btn fm-btn-secondary fm-grow"
        >
          {t("Back")}
        </button>
        <button
          type="button"
          onClick={onAward}
          className="fm-btn fm-btn-primary fm-grow"
        >
          {t("Award")} +{task.points}
        </button>
      </div>
    </div>
  );
}

function AwardError({
  code,
  task,
  onRetry,
  onChooseAnother,
  onScanNext,
}: {
  code: string;
  task: StaffTask;
  onRetry: () => void;
  onChooseAnother: () => void;
  onScanNext: () => void;
}) {
  const { t } = useLanguage();
  const mustReload = RELOAD_REQUIRED.has(code);
  const canRetry = RETRYABLE.has(code);
  const canChoose = !mustReload && (CHOOSE_ANOTHER.has(code) || canRetry);
  return (
    <div className="fm-stack">
      <p role="alert" className="fm-alert fm-alert-warn">
        {t(messageFor(code))}
      </p>
      {mustReload && (
        <>
          <p className="fm-micro">{t("No points were awarded.")}</p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="fm-btn fm-btn-primary fm-btn-block"
          >
            {t("Reload the page")}
          </button>
        </>
      )}
      {canRetry && (
        <>
          <p className="fm-micro">
            {t("Retrying is safe: the same award is never paid twice.")}
          </p>
          <button
            type="button"
            onClick={onRetry}
            className="fm-btn fm-btn-primary fm-btn-block"
          >
            {t("Try again")} · +{task.points} · {t(task.title)}
          </button>
        </>
      )}
      {canChoose && (
        <button
          type="button"
          onClick={onChooseAnother}
          className="fm-btn fm-btn-secondary fm-btn-block"
        >
          {t("Choose another activity")}
        </button>
      )}
      <button
        type="button"
        onClick={onScanNext}
        className={`fm-btn fm-btn-block ${canChoose || mustReload ? "fm-btn-secondary" : "fm-btn-primary"}`}
      >
        {t("Scan another attendee")}
      </button>
    </div>
  );
}

function AwardSuccess({
  identity,
  result,
  onAnother,
  onScanNext,
}: {
  identity: ScanResolveResult;
  result: AwardResponse;
  onAnother: () => void;
  onScanNext: () => void;
}) {
  const { t } = useLanguage();
  return (
    <div className="fm-card fm-stack">
      <p className="fm-eyebrow">
        {result.replayed ? t("Already awarded") : t("Awarded")}
      </p>
      <p className="fm-h2">+{result.points_awarded}</p>
      <p className="fm-text-soft">
        {identity.first_name} {identity.last_name} · {t(result.booth_name)}
        {result.task_title ? ` · ${t(result.task_title)}` : ""}
      </p>
      {/* The server computes this total — never re-derived here. */}
      <p className="fm-small">{t("New total:")} {result.resulting_total}</p>
      <button
        type="button"
        onClick={onAnother}
        className="fm-btn fm-btn-secondary fm-btn-block"
      >
        {t("Award another activity")}
      </button>
      <button
        type="button"
        onClick={onScanNext}
        className="fm-btn fm-btn-primary fm-btn-block"
      >
        {t("Scan another attendee")}
      </button>
    </div>
  );
}
