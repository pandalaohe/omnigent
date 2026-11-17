import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ExtensionsTab } from "./ExtensionsTab";

// Radix menus use pointer events; Enter also opens them with accessible focus.
afterEach(() => {
  cleanup();
  Object.assign(window, { omnigentDesktop: undefined });
});
it("gates extension execution on sign-ins, shortens IDs and confirms removal", async () => {
  let rows = [
    { id: "package", runtimeId: "a".repeat(32), name: "Bitwarden", version: "1", enabled: true },
  ];
  const remove = vi.fn(async () => {
    rows = [];
    return { ok: true };
  });
  const set = vi.fn().mockResolvedValue({ ok: true });
  Object.assign(window, {
    omnigentDesktop: {
      kind: "electron",
      getBrowserLoginSetting: vi.fn().mockResolvedValue({ ok: true, enabled: false }),
      setBrowserLoginSetting: set,
      clearBrowserLogin: vi.fn(),
      browserExtensionsList: async () => ({ ok: true, extensions: rows }),
      browserExtensionsAdd: vi.fn().mockResolvedValue({ ok: true }),
      browserExtensionsEnable: vi.fn().mockResolvedValue({ ok: true }),
      browserExtensionsRemove: remove,
    },
  });
  render(<ExtensionsTab />);
  const toggle = await screen.findByRole("switch", { name: "Enable Bitwarden" });
  expect(toggle).toBeDisabled();
  expect(screen.getByText("ID …aaaaaa")).toHaveAttribute("title", "a".repeat(32));
  fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
  await waitFor(() => expect(toggle).not.toBeDisabled());
  expect(set).toHaveBeenCalledWith(true);
  fireEvent.keyDown(screen.getByRole("button", { name: "Actions for Bitwarden" }), {
    key: "Enter",
  });
  fireEvent.click(await screen.findByRole("menuitem", { name: "Remove…" }));
  expect(remove).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog")).toHaveTextContent("local vault cache");
  fireEvent.click(screen.getByRole("button", { name: "Remove extension" }));
  await waitFor(() => expect(remove).toHaveBeenCalledWith("package"));
  await screen.findByText("Extension removed.");
});

it("keeps add, enable and Bitwarden permission review available with visible outcomes", async () => {
  let enabled = true;
  const add = vi.fn().mockResolvedValue({ ok: false, error: "Installation cancelled." }),
    enable = vi.fn(async (_id: string, next: boolean) => {
      enabled = next;
      return { ok: true };
    }),
    review = vi.fn().mockResolvedValue({ ok: true });
  Object.assign(window, {
    omnigentDesktop: {
      kind: "electron",
      getBrowserLoginSetting: vi.fn().mockResolvedValue({ ok: true, enabled: true }),
      setBrowserLoginSetting: vi.fn(),
      clearBrowserLogin: vi.fn(),
      browserExtensionsList: async () => ({
        ok: true,
        extensions: [{ id: "package", name: "Bitwarden", version: "1", enabled }],
      }),
      browserExtensionsAdd: add,
      browserExtensionsEnable: enable,
      browserExtensionsRemove: vi.fn(),
      browserExtensionsReview: review,
    },
  });
  render(<ExtensionsTab />);
  const toggle = await screen.findByRole("switch", { name: "Enable Bitwarden" });
  await waitFor(() => expect(toggle).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", { name: "Add extension…" }));
  await screen.findByText("Installation cancelled.");
  fireEvent.click(toggle);
  await screen.findByText("Extension disabled.");
  expect(enable).toHaveBeenCalledWith("package", false);
  fireEvent.keyDown(screen.getByRole("button", { name: "Actions for Bitwarden" }), {
    key: "Enter",
  });
  fireEvent.click(await screen.findByRole("menuitem", { name: "Review permissions" }));
  await screen.findByText("Extension permissions reviewed.");
  expect(review).toHaveBeenCalledWith("package");
});

it("filters extensions while retaining their actions", async () => {
  Object.assign(window, {
    omnigentDesktop: {
      kind: "electron",
      getBrowserLoginSetting: vi.fn().mockResolvedValue({ ok: true, enabled: true }),
      setBrowserLoginSetting: vi.fn(),
      clearBrowserLogin: vi.fn(),
      browserExtensionsList: async () => ({
        ok: true,
        extensions: [
          { id: "one", name: "First", version: "1", enabled: true },
          { id: "two", name: "Second", version: "1", enabled: false },
        ],
      }),
      browserExtensionsAdd: vi.fn(),
      browserExtensionsEnable: vi.fn(),
      browserExtensionsRemove: vi.fn(),
    },
  });
  render(<ExtensionsTab />);
  const search = await screen.findByRole("textbox", { name: "Search extensions" });
  expect(screen.getByText("Showing 2 of 2")).toBeVisible();
  fireEvent.change(search, { target: { value: "second" } });
  expect(screen.getByText("Showing 1 of 2")).toBeVisible();
  expect(screen.queryByRole("switch", { name: "Enable First" })).toBeNull();
  expect(screen.getByRole("switch", { name: "Enable Second" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Actions for Second" })).toBeVisible();
  expect(screen.getByLabelText("Disabled")).toBeVisible();
});
