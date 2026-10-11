import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";

import { RegisterForm } from "./RegisterForm";

const push = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

const INTERESTS = [
  { slug: "generative-ai", name: "Generative AI" },
  { slug: "robotics", name: "Robotics" },
];

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string, details: Record<string, unknown> = {}) {
  return jsonResponse(status, { error: { code, message: "server text", details, correlation_id: "c" } });
}

let registerResponse: () => Response | Promise<Response>;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  registerResponse = () =>
    jsonResponse(200, { message: "If the submitted information is valid, a verification code has been sent." });
  fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/interests") return jsonResponse(200, INTERESTS);
    if (url === "/api/attendees/register") return registerResponse();
    throw new Error(`unexpected fetch ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  window.sessionStorage.clear();
  push.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function fillValidForm() {
  fireEvent.change(screen.getByLabelText("First name"), { target: { value: "Grace" } });
  fireEvent.change(screen.getByLabelText("Last name"), { target: { value: "Hopper" } });
  fireEvent.change(screen.getByLabelText("Email"), {
    target: { value: "grace@summit.example.org" },
  });
  fireEvent.change(screen.getByLabelText("Phone"), { target: { value: "+1-555-0142" } });
  fireEvent.change(screen.getByLabelText("Profession / role"), {
    target: { value: "Systems Engineer" },
  });
}

function registerCalls() {
  return fetchMock.mock.calls.filter(([url]) => url === "/api/attendees/register");
}

describe("RegisterForm", () => {
  it("offers the interest catalogue fetched from the API", async () => {
    render(<RegisterForm />);
    expect(await screen.findByLabelText("Generative AI")).toBeInTheDocument();
    expect(screen.getByLabelText("Robotics")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/interests", expect.anything());
  });

  it("has no pre-selected sponsor consent and requires an explicit choice", async () => {
    render(<RegisterForm />);
    await screen.findByLabelText("Robotics");
    expect(screen.getByLabelText("Yes, share my details with sponsors")).not.toBeChecked();
    expect(screen.getByLabelText("No, don't share my details")).not.toBeChecked();

    fillValidForm();
    fireEvent.click(screen.getByRole("button", { name: "Register and get my code" }));

    expect(
      await screen.findByText("Choose whether to share your details with sponsors."),
    ).toBeInTheDocument();
    expect(registerCalls()).toHaveLength(0);
  });

  it("validates required fields and LinkedIn shape before calling the server", async () => {
    render(<RegisterForm />);
    await screen.findByLabelText("Robotics");
    fireEvent.change(screen.getByLabelText("LinkedIn (optional)"), {
      target: { value: "linkedin.com/in/no-scheme" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Register and get my code" }));

    expect(await screen.findByText("Enter your first name.")).toBeInTheDocument();
    expect(screen.getByText(/Enter a full LinkedIn URL/)).toBeInTheDocument();
    expect(registerCalls()).toHaveLength(0);
  });

  it("does not resend registration on rapid duplicate submission", async () => {
    let resolveRegister!: (response: Response) => void;
    registerResponse = () => new Promise<Response>((resolve) => { resolveRegister = resolve; });
    render(<RegisterForm />);
    await screen.findByLabelText("Robotics");
    fillValidForm();
    fireEvent.click(screen.getByLabelText("No, don't share my details"));
    const form = screen.getByRole("button", { name: "Register and get my code" }).closest("form")!;
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(registerCalls()).toHaveLength(1);
    resolveRegister(jsonResponse(200, { message: "Code sent" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/verify"));
  });
  it("submits the exact registration contract and moves to verification", async () => {
    render(<RegisterForm />);
    fireEvent.click(await screen.findByLabelText("Robotics"));
    fillValidForm();
    fireEvent.click(screen.getByLabelText("No, don't share my details"));
    fireEvent.click(screen.getByRole("button", { name: "Register and get my code" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/verify"));
    const [, init] = registerCalls()[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      first_name: "Grace",
      last_name: "Hopper",
      email: "grace@summit.example.org",
      phone: "+1-555-0142",
      profession: "Systems Engineer",
      interest_slugs: ["robotics"],
      sponsor_consent: false,
      language: "en", // the OTP email is written in the language in use
    });
    // The address travels to /verify via sessionStorage — never the URL.
    expect(window.sessionStorage.getItem("facemark.pendingEmail")).toBe(
      "grace@summit.example.org",
    );
  });

  it("sends sponsor_consent=true only when the attendee opts in", async () => {
    render(<RegisterForm />);
    await screen.findByLabelText("Robotics");
    fillValidForm();
    fireEvent.click(screen.getByLabelText("Yes, share my details with sponsors"));
    fireEvent.click(screen.getByRole("button", { name: "Register and get my code" }));

    await waitFor(() => expect(registerCalls()).toHaveLength(1));
    const [, init] = registerCalls()[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string).sponsor_consent).toBe(true);
  });

  it.each([
    ["UNKNOWN_INTEREST", 400],
    ["MAINTENANCE", 503],
    ["INTERNAL_ERROR", 500],
  ])("shows attendee copy, never server internals, for %s", async (code, status) => {
    registerResponse = () => errorResponse(status, code);
    render(<RegisterForm />);
    await screen.findByLabelText("Robotics");
    fillValidForm();
    fireEvent.click(screen.getByLabelText("No, don't share my details"));
    fireEvent.click(screen.getByRole("button", { name: "Register and get my code" }));

    expect(await screen.findByText(messageFor(code))).toBeInTheDocument();
    expect(screen.queryByText("server text")).not.toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });

  it("maps a server VALIDATION_ERROR onto the offending field", async () => {
    registerResponse = () =>
      errorResponse(422, "VALIDATION_ERROR", {
        errors: [{ loc: ["body", "phone"], msg: "raw", type: "string_too_long" }],
      });
    render(<RegisterForm />);
    await screen.findByLabelText("Robotics");
    fillValidForm();
    fireEvent.click(screen.getByLabelText("No, don't share my details"));
    fireEvent.click(screen.getByRole("button", { name: "Register and get my code" }));

    expect(await screen.findByText("Enter your phone number.")).toBeInTheDocument();
    expect(screen.getByLabelText("Phone")).toHaveAttribute("aria-invalid", "true");
  });

  it("reports a network failure without crashing", async () => {
    registerResponse = () => {
      throw new TypeError("Failed to fetch");
    };
    render(<RegisterForm />);
    await screen.findByLabelText("Robotics");
    fillValidForm();
    fireEvent.click(screen.getByLabelText("No, don't share my details"));
    fireEvent.click(screen.getByRole("button", { name: "Register and get my code" }));

    expect(await screen.findByText(messageFor("NETWORK_ERROR"))).toBeInTheDocument();
  });

  it("offers a retry when the interest catalogue fails to load", async () => {
    fetchMock.mockImplementationOnce(async () => {
      throw new TypeError("Failed to fetch");
    });
    render(<RegisterForm />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByLabelText("Generative AI")).toBeInTheDocument();
  });
});
