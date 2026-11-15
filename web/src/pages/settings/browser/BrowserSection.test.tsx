import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { BrowserSection } from "./BrowserSection";

function Route() {
  const location = useLocation(),
    navigate = useNavigate();
  return (
    <>
      <output data-testid="route">
        {location.pathname}
        {location.search}
        {location.hash}
      </output>
      <button type="button" onClick={() => navigate(-1)}>
        Back
      </button>
      <button type="button" onClick={() => navigate(1)}>
        Forward
      </button>
      <BrowserSection />
    </>
  );
}
function install(features: string[] = []) {
  const call = vi.fn(async () => ({
    ok: true,
    history: [],
    bookmarks: [],
    days: 90,
    entries: 5000,
  }));
  Object.assign(window, {
    omnigentDesktop: {
      kind: "electron",
      browserOpenOrNavigate: vi.fn(),
      browserPlaces: call,
      onBrowserPlaceChosen: () => () => {},
      browserPlacesFeatures: features,
      getBrowserLoginSetting: async () => ({ ok: true, enabled: true }),
      setBrowserLoginSetting: vi.fn(),
      clearBrowserLogin: vi.fn(),
      browserExtensionsList: async () => ({ ok: true, extensions: [] }),
      browserExtensionsAdd: vi.fn(),
      browserExtensionsEnable: vi.fn(),
      browserExtensionsRemove: vi.fn(),
      getBrowserAccess: async () => ({
        ok: true,
        access: { level: "agent", blocked: [], ask: [], allowed: [] },
      }),
      setBrowserAccess: vi.fn(),
    },
  });
  return call;
}
afterEach(() => {
  cleanup();
  Object.assign(window, { omnigentDesktop: undefined });
});
it("shows a plain web notice and no browser controls outside the desktop shell", () => {
  render(
    <MemoryRouter>
      <BrowserSection />
    </MemoryRouter>,
  );
  expect(screen.getByText("Browser settings are in the desktop app")).toBeInTheDocument();
  expect(screen.queryByRole("tab")).not.toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});
it("defaults unknown tabs to History and preserves other search params and hash across Back/Forward", async () => {
  install();
  render(
    <MemoryRouter initialEntries={["/settings/browser?keep=1&tab=unknown#anchor"]}>
      <Route />
    </MemoryRouter>,
  );
  expect(screen.getByRole("tab", { name: "History" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("tab", { name: "Bookmarks" }));
  expect(screen.getByTestId("route")).toHaveTextContent(
    "/settings/browser?keep=1&tab=bookmarks#anchor",
  );
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  await waitFor(() =>
    expect(screen.getByRole("tab", { name: "History" })).toHaveAttribute("aria-selected", "true"),
  );
  fireEvent.click(screen.getByRole("button", { name: "Forward" }));
  await waitFor(() =>
    expect(screen.getByRole("tab", { name: "Bookmarks" })).toHaveAttribute("aria-selected", "true"),
  );
});
it("preserves old-shell controls across five tabs and never calls a missing optional action", async () => {
  const call = install();
  render(
    <MemoryRouter>
      <BrowserSection />
    </MemoryRouter>,
  );
  expect(screen.getByText("Some options need a newer desktop app")).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Keep pages for" })).toBeDisabled();
  fireEvent.click(screen.getByRole("tab", { name: "Bookmarks" }));
  expect(screen.getByRole("button", { name: "Add bookmark" })).toBeEnabled();
  fireEvent.click(screen.getByRole("tab", { name: "Sign-in & site data" }));
  await waitFor(() =>
    expect(screen.getByRole("switch", { name: "Keep in-app browser sign-ins" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("tab", { name: "Extensions" }));
  expect(screen.getByRole("button", { name: "Add extension…" })).toBeEnabled();
  fireEvent.click(screen.getByRole("tab", { name: "Access" }));
  await waitFor(() => expect(screen.getByLabelText("Add to Never open")).toBeEnabled());
  expect(
    call.mock.calls.every((args) => (args as unknown as [{ action: string }])[0].action === "list"),
  ).toBe(true);
});
