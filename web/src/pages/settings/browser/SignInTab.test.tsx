import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { SignInTab } from "./SignInTab";

vi.mock("sonner", () => ({ toast: vi.fn() }));
afterEach(() => {
  cleanup();
  Object.assign(window, { omnigentDesktop: undefined });
});
function setup(extra: Record<string, unknown> = {}) {
  const bridge = {
    kind: "electron",
    getBrowserLoginSetting: vi.fn().mockResolvedValue({ ok: true, enabled: false }),
    setBrowserLoginSetting: vi.fn().mockResolvedValue({ ok: true }),
    clearBrowserLogin: vi.fn().mockResolvedValue({ ok: true }),
    browserFillApprovals: vi.fn().mockResolvedValue({ ok: true, sites: [] }),
    browserRevokeFillApproval: vi.fn().mockResolvedValue({ ok: true }),
    ...extra,
  };
  Object.assign(window, { omnigentDesktop: bridge });
  render(
    <MemoryRouter>
      <SignInTab />
    </MemoryRouter>,
  );
  return bridge;
}
it("saves sign-ins and confirms consequences before clearing", async () => {
  const bridge = setup();
  const toggle = screen.getByRole("switch", { name: "Keep in-app browser sign-ins" });
  await waitFor(() => expect(toggle).not.toBeDisabled());
  fireEvent.click(toggle);
  await waitFor(() => expect(bridge.setBrowserLoginSetting).toHaveBeenCalledWith(true));
  expect(toggle).toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: "Sign out…" }));
  expect(bridge.clearBrowserLogin).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog")).toHaveTextContent("Bitwarden");
  fireEvent.click(screen.getByRole("button", { name: "Sign out and clear" }));
  await waitFor(() => expect(bridge.clearBrowserLogin).toHaveBeenCalledOnce());
  expect(await screen.findByText(/Signed out. Open Browser/)).toBeInTheDocument();
});
it("retains switch state on a failed write and revokes saved site approval", async () => {
  const site = { origin: "https://example.test", id: "demo", label: "Demo account" },
    revoke = vi.fn().mockResolvedValue({ ok: true });
  setup({
    setBrowserLoginSetting: vi.fn().mockResolvedValue({ ok: false }),
    browserFillApprovals: vi.fn().mockResolvedValue({ ok: true, sites: [site] }),
    browserRevokeFillApproval: revoke,
  });
  const toggle = screen.getByRole("switch");
  await waitFor(() => expect(toggle).not.toBeDisabled());
  fireEvent.click(toggle);
  await screen.findByText("Could not save browser sign-in setting.");
  expect(toggle).not.toBeChecked();
  fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
  await waitFor(() => expect(revoke).toHaveBeenCalledWith(site));
  expect(await screen.findByText(/No sites approved/)).toBeInTheDocument();
});
it("lists local choices, waits for native Allow outcome and commits Forget without undo", async () => {
  const set = vi.fn().mockResolvedValue({ ok: false, cancelled: true });
  setup({
    browserLocalNetworkList: vi
      .fn()
      .mockResolvedValue({ ok: true, sites: [{ origin: "https://example.test", allowed: false }] }),
    browserLocalNetworkSet: set,
  });
  fireEvent.click(await screen.findByRole("radio", { name: "Allow" }));
  await screen.findByText("Change cancelled.");
  expect(screen.getByRole("radio", { name: "Block" })).toHaveAttribute("aria-checked", "true");
  set.mockResolvedValue({ ok: true, sites: [] });
  fireEvent.click(screen.getByRole("button", { name: "Forget" }));
  await waitFor(() =>
    expect(set).toHaveBeenLastCalledWith({ origin: "https://example.test", allowed: null }),
  );
});
