import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { LANGUAGE_STORAGE_KEY } from "@/lib/i18n";
import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import { LanguageProvider, LanguageSwitcher } from "./LanguageProvider";

afterEach(() => {
  window.localStorage.removeItem(LANGUAGE_STORAGE_KEY);
  document.documentElement.lang = "en";
});

describe("language selection", () => {
  it("switches navigation immediately and persists the chosen language", async () => {
    render(<LanguageProvider><LanguageSwitcher /><AttendeeTabBar active="home" /></LanguageProvider>);
    expect(screen.getByRole("group", { name: "Language" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Home" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Azərbaycan" }));
    expect(screen.getByRole("link", { name: "Əsas" })).toBeInTheDocument();
    await waitFor(() => expect(window.localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe("az"));
    expect(document.documentElement.lang).toBe("az");

    fireEvent.click(screen.getByRole("button", { name: "Русский" }));
    expect(screen.getByRole("link", { name: "Главная" })).toBeInTheDocument();
    await waitFor(() => expect(window.localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe("ru"));
  });

  it("restores a saved language on a new mount", async () => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "az");
    render(<LanguageProvider><LanguageSwitcher /><AttendeeTabBar active="home" /></LanguageProvider>);
    expect(await screen.findByRole("link", { name: "Əsas" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Azərbaycan" })).toHaveAttribute("aria-pressed", "true");
  });
});

describe("the browser tab title", () => {
  afterEach(() => {
    document.title = "";
  });

  it("is shown in the chosen language, follows navigation, and keeps the brand", async () => {
    document.title = "Register · AI Summit"; // what the server rendered
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "az");
    render(<LanguageProvider><LanguageSwitcher /></LanguageProvider>);
    await waitFor(() => expect(document.title).toBe("Qeydiyyat · AI Summit"));

    document.title = "Leaderboard · AI Summit"; // Next.js on a client-side navigation
    await waitFor(() => expect(document.title).toBe("Reytinq · AI Summit"));

    fireEvent.click(screen.getByRole("button", { name: "Русский" }));
    await waitFor(() => expect(document.title).toBe("Рейтинг · AI Summit"));
    fireEvent.click(screen.getByRole("button", { name: "English" }));
    await waitFor(() => expect(document.title).toBe("Leaderboard · AI Summit"));
  });

  it("leaves the plain brand title alone", async () => {
    document.title = "AI Summit";
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "ru");
    render(<LanguageProvider><LanguageSwitcher /></LanguageProvider>);
    await waitFor(() => expect(document.documentElement.lang).toBe("ru"));
    expect(document.title).toBe("AI Summit");
  });
});
