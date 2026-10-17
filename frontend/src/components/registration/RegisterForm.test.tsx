import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
      company: null, // left empty: no company is invented
      interest_card: { sector: null, topics: [], seeking: [], offering: [] }, // every card group is optional
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

  it("sends the company and the interest card with the same contract profile editing uses", async () => {
    render(<RegisterForm />);
    await screen.findByLabelText("Robotics");
    fillValidForm();
    fireEvent.change(screen.getByLabelText("Company (optional)"), { target: { value: "  Kapital Bank " } });
    const card = within(screen.getByTestId("interest-card"));
    fireEvent.click(within(card.getByRole("group", { name: "My field" })).getByLabelText("Banking and finance"));
    const topics = within(card.getByRole("group", { name: "Topics I'm interested in" }));
    for (const label of ["Data", "AI strategy", "Fintech", "Design"]) fireEvent.click(topics.getByLabelText(label));
    fireEvent.click(within(card.getByRole("group", { name: "What I'm looking for" })).getByLabelText("A partner"));
    fireEvent.click(within(card.getByRole("group", { name: "What I offer" })).getByLabelText("Mentoring"));
    fireEvent.click(screen.getByLabelText("No, don't share my details"));
    fireEvent.click(screen.getByRole("button", { name: "Register and get my code" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/verify"));
    const body = JSON.parse((registerCalls()[0] as [string, RequestInit])[1].body as string);
    expect(body.company).toBe("Kapital Bank");
    expect(body.profession).toBe("Systems Engineer"); // the role stays its own field
    expect(body.interest_card).toEqual({
      sector: "bank_finance",
      topics: ["data", "ai_strategy", "fintech"], // the fourth tap was refused at the limit
      seeking: ["partner"],
      offering: ["mentoring"],
    });
    expect(body.sponsor_consent).toBe(false); // the card has nothing to do with consent
  });

  it("names a card problem the server reports on the card itself", async () => {
    registerResponse = () =>
      errorResponse(422, "VALIDATION_ERROR", {
        errors: [{ loc: ["body", "interest_card", "topics"], msg: "x", type: "value_error" }],
      });
    render(<RegisterForm />);
    await screen.findByLabelText("Robotics");
    fillValidForm();
    fireEvent.click(screen.getByLabelText("No, don't share my details"));
    fireEvent.click(screen.getByRole("button", { name: "Register and get my code" }));
    expect(await screen.findByText("Check your interest card choices.")).toBeInTheDocument();
  });
});
