import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";
import type { AttendeeProfile } from "@/lib/profile";

import { ProfileView } from "./ProfileView";

const KEY = "0123456789abcdef0123456789abcdef";
const PHOTO = {
  small: `/api/media/avatars/${KEY}-128.jpg`,
  large: `/api/media/avatars/${KEY}-384.jpg`,
};

const PROFILE: AttendeeProfile = {
  first_name: "Ada",
  last_name: "Lovelace",
  email: "ada@example.com",
  phone: "+1-555-0142",
  profession: "Systems Engineer",
  linkedin_url: "https://www.linkedin.com/in/ada-demo",
  sponsor_consent: false,
  sponsor_consent_at: null,
  sponsor_consent_version: null,
  interests: [{ slug: "generative-ai", name: "Generative AI" }],
  version: 1,
  photo: null,
};

const CATALOGUE = [
  { slug: "generative-ai", name: "Generative AI" },
  { slug: "data-analytics", name: "Data Analytics" },
  { slug: "robotics", name: "Robotics" },
];

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function failure(status: number, code: string, details: Record<string, unknown> = {}): Response {
  return json(status, { error: { code, message: "", details, correlation_id: "c" } });
}

type Call = { method: string; url: string; body: unknown; rawBody: unknown };

// A tiny in-memory server: each test says how PUT /profile (and friends) answer;
// everything else behaves.
function server(overrides: {
  profile?: AttendeeProfile;
  put?: (call: Call, n: number) => Response | Promise<Response>;
  consent?: (call: Call, n: number) => Response | Promise<Response>;
  photoPut?: (call: Call, n: number) => Response | Promise<Response>;
  photoDelete?: (call: Call, n: number) => Response | Promise<Response>;
  interests?: () => Response;
} = {}) {
  const calls: Call[] = [];
  const counts: Record<string, number> = {};
  const profile = overrides.profile ?? PROFILE;
  const mock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const raw = init?.body;
    const call: Call = {
      method,
      url,
      rawBody: raw,
      body: typeof raw === "string" ? JSON.parse(raw) : undefined,
    };
    calls.push(call);
    const key = `${method} ${url}`;
    counts[key] = (counts[key] ?? 0) + 1;
    const n = counts[key];
    if (key === "GET /api/attendees/me/profile") return json(200, profile);
    if (key === "GET /api/interests") return overrides.interests?.() ?? json(200, CATALOGUE);
    if (key === "PUT /api/attendees/me/profile") {
      return overrides.put ? overrides.put(call, n) : json(200, { ...profile, version: profile.version + 1 });
    }
    if (key === "PUT /api/attendees/me/sponsor-consent") {
      return overrides.consent
        ? overrides.consent(call, n)
        : json(200, {
            sponsor_consent: (call.body as { granted: boolean }).granted,
            sponsor_consent_at: "2026-10-02T09:00:00Z",
            consent_version: "registration-v1",
            changed: true,
          });
    }
    if (key === "PUT /api/attendees/me/photo") {
      return overrides.photoPut ? overrides.photoPut(call, n) : json(200, { photo: PHOTO });
    }
    if (key === "DELETE /api/attendees/me/photo") {
      return overrides.photoDelete ? overrides.photoDelete(call, n) : json(200, { photo: null });
    }
    return failure(404, "NOT_FOUND");
  });
  vi.stubGlobal("fetch", mock);
  return {
    calls,
    puts: () => calls.filter((c) => c.method === "PUT" && c.url === "/api/attendees/me/profile"),
    of: (method: string, url: string) => calls.filter((c) => c.method === method && c.url === url),
  };
}

async function openEditor() {
  render(<ProfileView />);
  fireEvent.click(await screen.findByRole("button", { name: "Edit profile" }));
  return screen.findByRole("form", { name: "Edit profile" });
}

function type(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

beforeEach(() => {
  vi.stubGlobal("URL", Object.assign(URL, {
    createObjectURL: vi.fn(() => "blob:http://localhost/preview-1"),
    revokeObjectURL: vi.fn(),
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("editing the profile", () => {
  it("shows an Edit button, the details read-only, and no email field to change", async () => {
    server();
    const form = await openEditor();
    const email = within(form).getByLabelText("Email");
    expect(email).toHaveAttribute("readonly");
    expect(within(form).getByText("Your email can't be changed here.")).toBeInTheDocument();
    for (const label of ["First name", "Last name", "Phone", "Profession / role", "LinkedIn (optional)"]) {
      expect(within(form).getByLabelText(label)).not.toHaveAttribute("readonly");
    }
  });

  it("starts clean: nothing to save, the indicator says so", async () => {
    server();
    const form = await openEditor();
    expect(within(form).getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(within(form).getByText("No changes yet")).toBeInTheDocument();
  });

  it("marks unsaved changes as soon as something is typed and clears the mark when it is undone", async () => {
    server();
    const form = await openEditor();
    type("First name", "Augusta");
    const status = within(form).getByText("Unsaved changes").closest("p") as HTMLElement;
    expect(status).toHaveAttribute("data-dirty", "true");
    expect(within(form).getByRole("button", { name: "Save changes" })).toBeEnabled();

    type("First name", "Ada");
    expect(within(form).getByText("No changes yet")).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("saves the whole editable set with the loaded version, then shows the saved profile", async () => {
    const api = server({
      put: (call) =>
        json(200, { ...PROFILE, ...(call.body as object), linkedin_url: null, version: 2, interests: [
          { slug: "generative-ai", name: "Generative AI" },
          { slug: "robotics", name: "Robotics" },
        ] }),
    });
    const form = await openEditor();
    type("First name", "  Augusta ");
    type("LinkedIn (optional)", "");
    fireEvent.click(await within(form).findByLabelText("Robotics"));
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText("Profile saved.")).toBeInTheDocument();
    expect(api.puts()).toHaveLength(1);
    expect(api.puts()[0]?.body).toEqual({
      first_name: "Augusta",
      last_name: "Lovelace",
      phone: "+1-555-0142",
      profession: "Systems Engineer",
      linkedin_url: null,
      interest_slugs: ["generative-ai", "robotics"],
      expected_version: 1,
    });
    expect(api.puts()[0]?.body).not.toHaveProperty("email");
    expect(screen.getByRole("heading", { name: "Augusta Lovelace" })).toBeInTheDocument();
    expect(screen.getByText("Robotics")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Edit profile" })).not.toBeInTheDocument();
  });

  it("checks the fields first and sends nothing while one is wrong, keeping what was typed", async () => {
    const api = server();
    const form = await openEditor();
    type("First name", "   ");
    type("LinkedIn (optional)", "https://example.com/not-linkedin");
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    expect(await within(form).findByText("Enter your first name.")).toBeInTheDocument();
    expect(within(form).getByText(/Enter a full LinkedIn URL/)).toBeInTheDocument();
    expect(api.puts()).toHaveLength(0);
    expect(within(form).getByLabelText("LinkedIn (optional)")).toHaveValue("https://example.com/not-linkedin");
  });

  it("shows the server's field errors and keeps every typed value", async () => {
    server({
      put: () =>
        failure(422, "VALIDATION_ERROR", { errors: [{ loc: ["body", "phone"], msg: "bad" }] }),
    });
    const form = await openEditor();
    type("Phone", "12345");
    type("Profession / role", "Analyst");
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    expect(await within(form).findByText("Enter your phone number.")).toBeInTheDocument();
    expect(within(form).getByLabelText("Phone")).toHaveValue("12345");
    expect(within(form).getByLabelText("Profession / role")).toHaveValue("Analyst");
  });

  it("keeps the draft on a network failure and saves it when tried again", async () => {
    let attempts = 0;
    const api = server({
      put: () => {
        attempts += 1;
        if (attempts === 1) throw new TypeError("offline");
        return json(200, { ...PROFILE, profession: "Analyst", version: 2 });
      },
    });
    const form = await openEditor();
    type("Profession / role", "Analyst");
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    expect(await within(form).findByText(messageFor("NETWORK_ERROR"))).toBeInTheDocument();
    expect(within(form).getByLabelText("Profession / role")).toHaveValue("Analyst");
    expect(within(form).getByText("Unsaved changes")).toBeInTheDocument();

    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Profile saved.")).toBeInTheDocument();
    expect(api.puts()).toHaveLength(2);
    expect(api.puts()[1]?.body).toMatchObject({ profession: "Analyst", expected_version: 1 });
  });

  it("disables the buttons and marks the form busy while saving, and sends one request", async () => {
    let release: (r: Response) => void = () => {};
    const api = server({ put: () => new Promise<Response>((resolve) => { release = resolve; }) });
    const form = await openEditor();
    type("First name", "Augusta");
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    const saving = await within(form).findByRole("button", { name: "Saving…" });
    expect(saving).toBeDisabled();
    expect(form).toHaveAttribute("aria-busy", "true");
    fireEvent.submit(form); // a double submit while the first is in flight
    expect(api.puts()).toHaveLength(1);
    await act(async () => release(json(200, { ...PROFILE, first_name: "Augusta", version: 2 })));
    expect(await screen.findByText("Profile saved.")).toBeInTheDocument();
  });

  it("names an interest that is no longer available, keeps the selection, and lets it be removed", async () => {
    let attempts = 0;
    const api = server({
      put: (call) => {
        attempts += 1;
        return attempts === 1
          ? failure(400, "UNKNOWN_INTEREST", { slugs: ["robotics"] })
          : json(200, { ...PROFILE, ...(call.body as object), linkedin_url: PROFILE.linkedin_url, version: 2 });
      },
    });
    const form = await openEditor();
    fireEvent.click(await within(form).findByLabelText("Robotics"));
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    expect(
      await within(form).findByText("Some of your interests are no longer available. Remove them and save again."),
    ).toBeInTheDocument();
    const flagged = within(form).getByLabelText(/Robotics/).closest("label") as HTMLElement;
    expect(flagged).toHaveAttribute("data-unavailable", "true");
    expect(within(form).getByLabelText(/Robotics/)).toBeChecked(); // nothing was dropped silently

    fireEvent.click(within(form).getByLabelText(/Robotics/)); // remove it
    expect(within(form).getByLabelText(/Robotics/).closest("label")).not.toHaveAttribute("data-unavailable");
    type("Profession / role", "Analyst"); // (back to the saved interests, so something else to save)
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Profile saved.")).toBeInTheDocument();
    expect(api.puts()[1]?.body).toMatchObject({ interest_slugs: ["generative-ai"], profession: "Analyst" });
  });

  it("returns to the sign-in prompt if the session ended while editing", async () => {
    server({ put: () => failure(401, "UNAUTHENTICATED") });
    const form = await openEditor();
    type("First name", "Augusta");
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
  });

  it("still lists the interests the person holds when the catalogue cannot be loaded, and offers a retry", async () => {
    server({ interests: () => failure(503, "MAINTENANCE") });
    const form = await openEditor();
    expect(await within(form).findByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(within(form).getByLabelText("Generative AI")).toBeChecked();
  });
});

describe("cancelling", () => {
  it("leaves at once when nothing was changed", async () => {
    server();
    const form = await openEditor();
    fireEvent.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("form", { name: "Edit profile" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Ada Lovelace" })).toBeInTheDocument();
  });

  it("asks before throwing typed changes away, and keeps them if told to keep editing", async () => {
    const api = server();
    const form = await openEditor();
    type("First name", "Augusta");
    fireEvent.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("alertdialog", { name: "Discard your changes?" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(within(form).getByLabelText("First name")).toHaveValue("Augusta");

    fireEvent.click(within(form).getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(screen.queryByRole("form", { name: "Edit profile" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Ada Lovelace" })).toBeInTheDocument(); // the saved name
    expect(api.puts()).toHaveLength(0);
  });

  it("warns the browser before unloading while there are unsaved changes, and not otherwise", async () => {
    server();
    const form = await openEditor();
    const clean = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);
    type("First name", "Augusta");
    const dirty = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirty);
    expect(dirty.defaultPrevented).toBe(true);
    fireEvent.click(within(form).getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    const after = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(after);
    expect(after.defaultPrevented).toBe(false);
  });
});

describe("a profile changed on another device", () => {
  const NEWER: AttendeeProfile = { ...PROFILE, profession: "Chief Engineer", version: 2 };

  it("keeps what was typed, shows both versions, and refuses to overwrite silently", async () => {
    const api = server({ put: () => failure(409, "PROFILE_CONFLICT", { profile: NEWER }) });
    const form = await openEditor();
    type("First name", "Augusta");
    type("Profession / role", "Mathematician");
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    const alert = await screen.findByText("Your profile was changed on another device");
    const panel = alert.closest("section") as HTMLElement;
    expect(within(panel).getByText("Mathematician")).toBeInTheDocument(); // yours
    expect(within(panel).getByText("Chief Engineer")).toBeInTheDocument(); // saved elsewhere
    expect(within(panel).getByText("Profession / role")).toBeInTheDocument();
    // typed text is intact
    expect(within(form).getByLabelText("First name")).toHaveValue("Augusta");
    expect(within(form).getByLabelText("Profession / role")).toHaveValue("Mathematician");
    expect(api.puts()).toHaveLength(1);
  });

  it("saves the typed changes on top of the newer version when asked", async () => {
    let attempts = 0;
    const api = server({
      put: (call) => {
        attempts += 1;
        return attempts === 1
          ? failure(409, "PROFILE_CONFLICT", { profile: NEWER })
          : json(200, { ...NEWER, ...(call.body as object), linkedin_url: NEWER.linkedin_url, version: 3 });
      },
    });
    const form = await openEditor();
    type("Profession / role", "Mathematician");
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    fireEvent.click(await screen.findByRole("button", { name: "Save my changes anyway" }));

    expect(await screen.findByText("Profile saved.")).toBeInTheDocument();
    expect(api.puts()).toHaveLength(2);
    expect(api.puts()[1]?.body).toMatchObject({ profession: "Mathematician", expected_version: 2 });
    expect(screen.getByText("Mathematician")).toBeInTheDocument();
  });

  it("drops the typed changes for the saved version only when told to", async () => {
    server({ put: () => failure(409, "PROFILE_CONFLICT", { profile: NEWER }) });
    const form = await openEditor();
    type("Profession / role", "Mathematician");
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    fireEvent.click(await screen.findByRole("button", { name: "Use the saved version" }));
    expect(within(form).getByLabelText("Profession / role")).toHaveValue("Chief Engineer");
    expect(screen.queryByText("Your profile was changed on another device")).not.toBeInTheDocument();
    expect(within(form).getByText("No changes yet")).toBeInTheDocument();
  });

  it("just finishes when the other device saved exactly what was typed", async () => {
    server({
      put: () =>
        failure(409, "PROFILE_CONFLICT", { profile: { ...PROFILE, profession: "Mathematician", version: 2 } }),
    });
    const form = await openEditor();
    type("Profession / role", "Mathematician");
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Profile saved.")).toBeInTheDocument();
    expect(screen.queryByText("Your profile was changed on another device")).not.toBeInTheDocument();
  });

  it("falls back to a plain message when the conflict carries no usable profile", async () => {
    server({ put: () => failure(409, "PROFILE_CONFLICT", { profile: { nonsense: true } }) });
    const form = await openEditor();
    type("First name", "Augusta");
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    expect(await within(form).findByText(messageFor("PROFILE_CONFLICT"))).toBeInTheDocument();
    expect(within(form).getByLabelText("First name")).toHaveValue("Augusta");
  });
});

describe("the profile photo", () => {
  function pick(file: File) {
    fireEvent.change(screen.getByLabelText("Choose a photo"), { target: { files: [file] } });
  }
  const png = () => new File([new Uint8Array([137, 80, 78, 71])], "me.png", { type: "image/png" });

  it("tells the person where the photo will be seen", async () => {
    server();
    render(<ProfileView />);
    expect(
      await screen.findByText(
        "Your photo appears on the leaderboard and on your contact card, where other attendees can see it.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the robot until there is a photo, then the photo with a way to change and remove it", async () => {
    server({ profile: { ...PROFILE, photo: PHOTO } });
    const { container } = render(<ProfileView />);
    await screen.findByRole("button", { name: "Change photo" });
    expect(screen.getByRole("button", { name: "Remove photo" })).toBeInTheDocument();
    expect(container.querySelector('.fm-avatar-profile img')?.getAttribute("src")).toContain(KEY);
  });

  it("offers only 'Add a photo' while there is none", async () => {
    server();
    render(<ProfileView />);
    expect(await screen.findByRole("button", { name: "Add a photo" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove photo" })).not.toBeInTheDocument();
  });

  it("previews the chosen picture locally and uploads nothing until asked", async () => {
    const api = server();
    const { container } = render(<ProfileView />);
    await screen.findByRole("button", { name: "Add a photo" });
    pick(png());
    expect(await screen.findByText("Preview — not uploaded yet")).toBeInTheDocument();
    expect(container.querySelector('.fm-avatar-profile img')?.getAttribute("src")).toBe(
      "blob:http://localhost/preview-1",
    );
    expect(api.of("PUT", "/api/attendees/me/photo")).toHaveLength(0);
  });

  it("uploads the raw file, then shows the new photo and says so", async () => {
    const api = server();
    const { container } = render(<ProfileView />);
    await screen.findByRole("button", { name: "Add a photo" });
    const file = png();
    pick(file);
    fireEvent.click(await screen.findByRole("button", { name: "Upload photo" }));
    expect(await screen.findByText("Photo updated.")).toBeInTheDocument();
    const uploads = api.of("PUT", "/api/attendees/me/photo");
    expect(uploads).toHaveLength(1);
    expect(uploads[0]?.rawBody).toBe(file);
    await waitFor(() =>
      expect(container.querySelector('.fm-avatar-profile img')?.getAttribute("src")).toContain(KEY),
    );
    expect(screen.getByRole("button", { name: "Change photo" })).toBeInTheDocument();
  });

  it.each([
    ["an SVG", new File(["<svg/>"], "x.svg", { type: "image/svg+xml" }), "Choose a JPEG, PNG or WebP picture."],
    ["a GIF", new File([new Uint8Array([1])], "x.gif", { type: "image/gif" }), "Choose a JPEG, PNG or WebP picture."],
    ["an empty file", new File([], "x.png", { type: "image/png" }), "That file is empty. Choose another picture."],
  ])("turns away %s before uploading anything", async (_label, file, message) => {
    const api = server();
    render(<ProfileView />);
    await screen.findByRole("button", { name: "Add a photo" });
    pick(file);
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(api.of("PUT", "/api/attendees/me/photo")).toHaveLength(0);
    expect(screen.queryByText("Preview — not uploaded yet")).not.toBeInTheDocument();
  });

  it("turns away an over-size picture before uploading", async () => {
    const api = server();
    render(<ProfileView />);
    await screen.findByRole("button", { name: "Add a photo" });
    const big = new File([new Uint8Array(1)], "big.png", { type: "image/png" });
    Object.defineProperty(big, "size", { value: 9 * 1024 * 1024 });
    pick(big);
    expect(await screen.findByText("That photo is too large. Choose a smaller picture.")).toBeInTheDocument();
    expect(api.of("PUT", "/api/attendees/me/photo")).toHaveLength(0);
  });

  it.each([
    ["PHOTO_INVALID", 400],
    ["PHOTO_TOO_LARGE", 413],
    ["PHOTO_BUSY", 503],
    ["PHOTO_STORAGE_UNAVAILABLE", 503],
    ["RATE_LIMITED", 429],
  ])("explains a refused upload (%s) and keeps the chosen picture for another try", async (code, status) => {
    const api = server({ photoPut: () => failure(status, code) });
    render(<ProfileView />);
    await screen.findByRole("button", { name: "Add a photo" });
    pick(png());
    fireEvent.click(await screen.findByRole("button", { name: "Upload photo" }));
    expect(await screen.findByText(messageFor(code))).toBeInTheDocument();
    expect(screen.getByText("Preview — not uploaded yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload photo" })).toBeEnabled();
    expect(api.of("PUT", "/api/attendees/me/photo")).toHaveLength(1);
  });

  it("keeps the chosen picture through a network failure and uploads it on retry", async () => {
    let attempts = 0;
    const api = server({
      photoPut: () => {
        attempts += 1;
        if (attempts === 1) throw new TypeError("offline");
        return json(200, { photo: PHOTO });
      },
    });
    render(<ProfileView />);
    await screen.findByRole("button", { name: "Add a photo" });
    pick(png());
    fireEvent.click(await screen.findByRole("button", { name: "Upload photo" }));
    expect(await screen.findByText(messageFor("NETWORK_ERROR"))).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Upload photo" }));
    expect(await screen.findByText("Photo updated.")).toBeInTheDocument();
    expect(api.of("PUT", "/api/attendees/me/photo")).toHaveLength(2);
  });

  it("cancels a chosen picture without a request", async () => {
    const api = server();
    render(<ProfileView />);
    await screen.findByRole("button", { name: "Add a photo" });
    pick(png());
    const preview = await screen.findByText("Preview — not uploaded yet");
    const group = preview.parentElement as HTMLElement;
    fireEvent.click(within(group).getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Add a photo" })).toBeInTheDocument();
    expect(api.of("PUT", "/api/attendees/me/photo")).toHaveLength(0);
  });

  it("removes the photo and goes back to the robot", async () => {
    const api = server({ profile: { ...PROFILE, photo: PHOTO } });
    const { container } = render(<ProfileView />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove photo" }));
    expect(await screen.findByText("Photo removed.")).toBeInTheDocument();
    expect(api.of("DELETE", "/api/attendees/me/photo")).toHaveLength(1);
    expect(container.querySelector(".fm-avatar-profile img")).toBeNull();
    expect(screen.getByRole("button", { name: "Add a photo" })).toBeInTheDocument();
  });

  it("falls back to the robot when the saved photo fails to load", async () => {
    server({ profile: { ...PROFILE, photo: PHOTO } });
    const { container } = render(<ProfileView />);
    await screen.findByRole("button", { name: "Change photo" });
    fireEvent.error(container.querySelector(".fm-avatar-profile img") as HTMLImageElement);
    expect(container.querySelector(".fm-avatar-profile img")).toBeNull();
    expect(container.querySelector(".fm-avatar-profile")).toHaveAttribute("data-photo", "false");
  });

  it("does not lose the typed profile text when the photo is changed meanwhile", async () => {
    server();
    const form = await openEditor();
    type("First name", "Augusta");
    pick(png());
    fireEvent.click(await screen.findByRole("button", { name: "Upload photo" }));
    expect(await screen.findByText("Photo updated.")).toBeInTheDocument();
    expect(within(form).getByLabelText("First name")).toHaveValue("Augusta");
  });
});

describe("sponsor consent, on its own", () => {
  it("is saved by its own button, not by the profile form", async () => {
    const api = server();
    render(<ProfileView />);
    fireEvent.click(await screen.findByRole("button", { name: "Share my details with sponsors" }));
    expect(await screen.findByText("You agreed to share your details with event sponsors.")).toBeInTheDocument();
    expect(api.of("PUT", "/api/attendees/me/sponsor-consent")[0]?.body).toEqual({ granted: true });
    expect(api.puts()).toHaveLength(0);
    expect(screen.getByText(/Agreed on/)).toBeInTheDocument();
    expect(screen.getByText("Your choice was saved.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop sharing my details" })).toBeInTheDocument();
  });

  it("withdraws the same way", async () => {
    const api = server({
      profile: { ...PROFILE, sponsor_consent: true, sponsor_consent_at: "2026-10-01T10:00:00Z", sponsor_consent_version: "registration-v1" },
      consent: () =>
        json(200, { sponsor_consent: false, sponsor_consent_at: null, consent_version: null, changed: true }),
    });
    render(<ProfileView />);
    fireEvent.click(await screen.findByRole("button", { name: "Stop sharing my details" }));
    expect(
      await screen.findByText("You have not agreed to share your details with event sponsors."),
    ).toBeInTheDocument();
    expect(api.of("PUT", "/api/attendees/me/sponsor-consent")[0]?.body).toEqual({ granted: false });
    expect(screen.queryByText(/Agreed on/)).not.toBeInTheDocument();
  });

  it("keeps showing the value in force when the change could not be saved", async () => {
    server({ consent: () => failure(503, "MAINTENANCE") });
    render(<ProfileView />);
    fireEvent.click(await screen.findByRole("button", { name: "Share my details with sponsors" }));
    expect(await screen.findByText(messageFor("MAINTENANCE"))).toBeInTheDocument();
    expect(
      screen.getByText("You have not agreed to share your details with event sponsors."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Share my details with sponsors" })).toBeEnabled();
  });

  it("does not disturb a profile edit in progress", async () => {
    server();
    const form = await openEditor();
    type("First name", "Augusta");
    fireEvent.click(screen.getByRole("button", { name: "Share my details with sponsors" }));
    expect(await screen.findByText("Your choice was saved.")).toBeInTheDocument();
    expect(within(form).getByLabelText("First name")).toHaveValue("Augusta");
    expect(within(form).getByText("Unsaved changes")).toBeInTheDocument();
  });
});
