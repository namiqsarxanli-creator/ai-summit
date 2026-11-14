import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { confirmAction } from "@/lib/confirm";

import { ConfirmHost } from "./ConfirmHost";

describe("ConfirmHost", () => {
  it("asks in the site's own dialog instead of the browser's, and answers yes or no", async () => {
    const native = vi.spyOn(window, "confirm");
    render(<ConfirmHost />);
    let yes: Promise<boolean> | undefined;
    act(() => { yes = confirmAction("Delete this?"); });
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Delete this?");
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await expect(yes).resolves.toBe(true);
    expect(screen.queryByRole("alertdialog")).toBeNull();

    let no: Promise<boolean> | undefined;
    act(() => { no = confirmAction("Block this person?"); });
    await screen.findByRole("alertdialog");
    fireEvent.keyDown(window, { key: "Escape" });
    await expect(no).resolves.toBe(false);
    expect(native).not.toHaveBeenCalled();
    native.mockRestore();
  });

  it("falls back to the browser's confirm when no dialog is mounted", async () => {
    const native = vi.spyOn(window, "confirm").mockReturnValue(false);
    await expect(confirmAction("Sure?")).resolves.toBe(false);
    expect(native).toHaveBeenCalledWith("Sure?");
    native.mockRestore();
  });
});
