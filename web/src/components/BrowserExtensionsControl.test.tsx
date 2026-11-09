import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BrowserExtensionsControl } from "./BrowserExtensionsControl";

vi.mock("@/lib/analytics", () => ({ useOmnigentAnalytics: () => ({ track: vi.fn() }) }));
afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});
describe("browser extension settings", () => {
  it("hides outside desktop and shows no dead controls", () => {
    render(<BrowserExtensionsControl />);
    expect(screen.queryByRole("button", { name: "Add extension" })).not.toBeInTheDocument();
  });
  it("lists, toggles, reviews and removes with visible results and no renderer path", async () => {
    let rows = [
      { id: "package", runtimeId: "a".repeat(32), name: "Bitwarden", version: "1", enabled: true },
    ];
    const add = vi.fn().mockResolvedValue({ ok: false, error: "Installation cancelled." });
    const enable = vi.fn(async (_id, enabled) => {
      rows = rows.map((row) => ({ ...row, enabled }));
      return { ok: true };
    });
    const remove = vi.fn(async () => {
      rows = [];
      return { ok: true };
    });
    const review = vi.fn().mockResolvedValue({ ok: true });
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      getBrowserLoginSetting: vi.fn(),
      setBrowserLoginSetting: vi.fn(),
      clearBrowserLogin: vi.fn(),
      browserExtensionsList: async () => ({ ok: true, extensions: rows }),
      browserExtensionsAdd: add,
      browserExtensionsEnable: enable,
      browserExtensionsRemove: remove,
      browserExtensionsReview: review,
    };
    render(<BrowserExtensionsControl />);
    expect(await screen.findByText("a".repeat(32))).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add extension" }));
    expect(await screen.findByText("Installation cancelled.")).toBeInTheDocument();
    expect(add).toHaveBeenCalledWith();
    fireEvent.click(screen.getByRole("switch", { name: "Enable Bitwarden" }));
    expect(await screen.findByText("Extension disabled.")).toBeInTheDocument();
    expect(enable).toHaveBeenCalledWith("package", false);
    fireEvent.click(screen.getByRole("button", { name: "Review permissions" }));
    expect(await screen.findByText("Extension permissions reviewed.")).toBeInTheDocument();
    expect(review).toHaveBeenCalledWith("package");
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(remove).toHaveBeenCalledOnce());
    expect(await screen.findByText("Extension removed.")).toBeInTheDocument();
    expect(screen.getByText("No extensions installed.")).toBeInTheDocument();
  });
});
