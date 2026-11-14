import { describe, expect, it } from "vitest";

import {
  ACTION_OPTIONS, AUDIT_TEXTS, FIELD_LABELS, TARGET_OPTIONS, actionLabel, anyAuditFilter, auditProblem, auditQuery,
  diffRows, sameAuditFilters, targetLabel, type AuditEvent,
} from "./auditView";
import { hasTranslation, translate } from "./i18n";

const base: AuditEvent = {
  id: 1, created_at: "2026-09-01T10:00:00Z", actor_id: 1, actor_name: "A", action: "booth.update", target_type: "booth",
  target_id: "1", operation_id: null, before: null, after: null, changed_fields: [], result: null, hidden_field_count: 0, details: "known",
};

describe("audit filters", () => {
  it("builds the query from the API's parameter names and leaves blanks out", () => {
    expect(auditQuery({}, null)).toBe("");
    const q = new URLSearchParams(auditQuery({ actor_id: " 4 ", action: "task.update", target_type: "task", target_id: "9", created_from: "", created_to: " " }, "cur"));
    expect(Object.fromEntries(q)).toEqual({ actor_id: "4", action: "task.update", target_type: "task", target_id: "9", cursor: "cur" });
  });

  it("dates are the start of the first local day and the start of the day after the last", () => {
    const q = new URLSearchParams(auditQuery({ created_from: "2026-03-28", created_to: "2026-03-28" }, null));
    expect(q.get("created_from")).toBe(new Date("2026-03-28T00:00:00").toISOString());
    expect(q.get("created_to")).toBe(new Date("2026-03-29T00:00:00").toISOString());
    expect(new Date(q.get("created_from")!).getTime()).toBeLessThan(new Date(q.get("created_to")!).getTime());
  });

  it("refuses what the server would refuse", () => {
    expect(auditProblem({})).toBe("");
    expect(auditProblem({ actor_id: "0" })).not.toBe("");
    expect(auditProblem({ actor_id: "1.5" })).not.toBe("");
    expect(auditProblem({ actor_id: "12345678901" })).not.toBe("");
    expect(auditProblem({ target_id: "5" })).toContain("target type");
    expect(auditProblem({ target_type: "booth", target_id: "5; DROP" })).not.toBe("");
    expect(auditProblem({ target_type: "export", target_id: "0123456789abcdef" })).toBe("");
    expect(auditProblem({ target_type: "booth_interest", target_id: "3:7" })).toBe("");
    expect(auditProblem({ created_from: "2026-09-05", created_to: "2026-09-01" })).not.toBe("");
    expect(auditProblem({ created_from: "2026-09-01", created_to: "2026-09-01" })).toBe("");
    expect(auditProblem({ created_from: "not-a-date" })).not.toBe("");
  });

  it("compares filters ignoring blanks and spaces", () => {
    expect(sameAuditFilters({ action: " a " }, { action: "a", actor_id: "" })).toBe(true);
    expect(sameAuditFilters({ action: "a" }, { action: "b" })).toBe(false);
    expect(anyAuditFilter({ action: " " })).toBe(false);
    expect(anyAuditFilter({ target_type: "booth" })).toBe(true);
  });
});

describe("audit differences", () => {
  it("one row per field either side recorded, id first, with the server's verdict on what changed", () => {
    const rows = diffRows({ ...base, before: { name: "a", weight: "1" }, after: { name: "b", id: 3, weight: "1" }, changed_fields: ["name"] });
    expect(rows.map((r) => r.field)).toEqual(["id", "name", "weight"]);
    expect(rows.find((r) => r.field === "id")).toMatchObject({ before: undefined, after: 3, changed: false });
    expect(rows.find((r) => r.field === "name")).toMatchObject({ before: "a", after: "b", changed: true });
    expect(rows.find((r) => r.field === "weight")?.changed).toBe(false);
  });

  it("no before and no after is no rows", () => {
    expect(diffRows(base)).toEqual([]);
  });
});

describe("audit texts", () => {
  it("every text of the section has an Azerbaijani and a Russian translation", () => {
    const missing = [...new Set(AUDIT_TEXTS)].filter((text) => !hasTranslation(text));
    expect(missing).toEqual([]);
    for (const text of new Set(AUDIT_TEXTS)) {
      expect(translate(text, "az")).not.toBe("");
      expect(translate(text, "ru")).not.toBe("");
    }
    // the words that differ between languages really do (a translation that is the English text is a gap)
    for (const text of ["Audit log", "Actor", "Target type", "Details", "Refused before it started", "Booth updated"]) {
      expect(translate(text, "az")).not.toBe(text);
      expect(translate(text, "ru")).not.toBe(text);
    }
  });

  it("every action, target type and field the server can send has a label", () => {
    expect(ACTION_OPTIONS.map(([code]) => code)).toEqual([
      "staff.create", "staff.deactivate", "booth.create", "booth.update", "booth.weight", "booth.weight.remove",
      "interest.create", "interest.update", "task.create", "task.update", "preset.create", "preset.update",
      "event_config.update", "points.reverse", "staff.password_change", "staff.password_reset_requested",
      "staff.password_reset", "staff.password_admin_set", "export.started", "export.finished", "export.rejected",
    ]);
    for (const [code] of ACTION_OPTIONS) expect(actionLabel(code)).not.toBeNull();
    for (const [code] of TARGET_OPTIONS) expect(targetLabel(code)).not.toBeNull();
    expect(actionLabel("weird.thing")).toBeNull();
    expect(targetLabel("gadget")).toBeNull();
    const serverFields = ["id", "slug", "name", "title", "label", "description", "location_label", "role", "booth_id", "interest_id", "is_active",
      "allow_repeat_task_points", "allow_repeat_points", "allow_repeat_task_points_default", "sort_order", "points", "weight", "removed",
      "feedback_points", "leaderboard_top_n", "points_log_id", "reversal_id", "user_id", "task_id", "source", "reversed",
      "reason", "sessions_revoked", "initiated_by"];
    for (const field of serverFields) expect(FIELD_LABELS[field], field).toBeDefined();
  });
});
