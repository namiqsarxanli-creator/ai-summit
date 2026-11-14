import { fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "@/components/ui/LanguageProvider";
import { LANGUAGE_STORAGE_KEY } from "@/lib/i18n";

import { LoginForm } from "./LoginForm";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

const fetchMock = vi.fn(async () =>
  new Response(JSON.stringify({ message: "ok" }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }),
);

beforeEach(() => {
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("OTP request language", () => {
  it.each(["az", "ru"])("asks for the code email in the selected language (%s)", async (code) => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, code);
    const { container } = render(
      <LanguageProvider>
        <LoginForm />
      </LanguageProvider>,
    );
    await waitFor(() => expect(document.documentElement.lang).toBe(code));

    const email = container.querySelector("#email") as HTMLInputElement;
    fireEvent.change(email, { target: { value: "grace@summit.example.org" } });
    fireEvent.submit(email.closest("form") as HTMLFormElement);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    const call = calls[0];
    if (!call) throw new Error("no request was made");
    const [url, init] = call;
    expect(url).toBe("/api/auth/otp/request");
    expect(JSON.parse(init.body as string)).toEqual({
      email: "grace@summit.example.org",
      language: code,
    });
  });
});
