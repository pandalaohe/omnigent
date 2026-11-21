import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AccessTab } from "./AccessTab";
import { expectSettingsPages } from "@/test/settingsPaging";

const notices = vi.hoisted(() => ({ toast: vi.fn() }));
vi.mock("sonner", () => ({ toast: notices.toast }));
afterEach(() => {
  cleanup();
  Object.assign(window, { omnigentDesktop: undefined });
  vi.clearAllMocks();
});
it("validates chips, keeps a cancelled widening draft and supports discard and save", async () => {
  const access = { level: "agent", blocked: [], ask: [], allowed: [] };
  const set = vi
    .fn()
    .mockResolvedValueOnce({ ok: false, error: "Browser access change cancelled" })
    .mockResolvedValue({ ok: true });
  Object.assign(window, {
    omnigentDesktop: {
      kind: "electron",
      getBrowserAccess: vi.fn().mockResolvedValue({ ok: true, access }),
      setBrowserAccess: set,
    },
  });
  render(<AccessTab />);
  const input = screen.getByLabelText("Add to Open without asking");
  await waitFor(() => expect(input).not.toBeDisabled());
  fireEvent.change(input, { target: { value: "https://example.com/path" } });
  fireEvent.submit(input.closest("form")!);
  expect(screen.getByRole("alert")).toHaveTextContent("Use host");
  fireEvent.change(input, { target: { value: "*.example.com" } });
  fireEvent.submit(input.closest("form")!);
  fireEvent.change(input, { target: { value: "127.0.0.0/8" } });
  fireEvent.submit(input.closest("form")!);
  expect(screen.getByText("2 unsaved changes")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Browser access change cancelled");
  expect(set).toHaveBeenCalledWith({ ...access, allowed: ["*.example.com", "127.0.0.0/8"] });
  expect(screen.getByText("2 unsaved changes")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  expect(screen.queryByText("*.example.com")).not.toBeInTheDocument();
  fireEvent.change(input, { target: { value: "host.example" } });
  fireEvent.submit(input.closest("form")!);
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Browser access saved.");
  expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
});
it("bounds the number of remembered addresses even for a single server", async () => {
  const rowText = (number: number) => `local-${number}.example.test`;
  const entries = Array.from({ length: 45 }, (_, index) => rowText(index + 1));
  Object.assign(window, {
    omnigentDesktop: {
      kind: "electron",
      browserRememberedList: vi.fn().mockResolvedValue({
        ok: true,
        servers: [{ origin: "https://server.example.test", current: true, entries }],
      }),
      browserRememberedForget: vi.fn(),
    },
  });
  render(<AccessTab />);
  const returnToFirstPage = await expectSettingsPages("Remembered address pages", rowText);
  returnToFirstPage();
  expect(screen.getByText("This server")).toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "Search remembered addresses" }), {
    target: { value: rowText(44) },
  });
  expect(screen.getByText(rowText(44))).toBeInTheDocument();
  expect(screen.queryByText(rowText(45))).not.toBeInTheDocument();
});

it("forgets a remembered grant without offering a renderer re-grant", async () => {
  const forget = vi.fn().mockResolvedValue({ ok: true });
  Object.assign(window, {
    omnigentDesktop: {
      kind: "electron",
      browserRememberedList: vi.fn().mockResolvedValue({
        ok: true,
        servers: [{ origin: "https://example.test", current: true, entries: ["localhost:8000"] }],
      }),
      browserRememberedForget: forget,
    },
  });
  render(<AccessTab />);
  await screen.findByText("localhost:8000");
  fireEvent.click(screen.getByRole("button", { name: "Forget" }));
  await waitFor(() =>
    expect(forget).toHaveBeenCalledWith({
      origin: "https://example.test",
      entry: "localhost:8000",
    }),
  );
  expect(notices.toast).toHaveBeenCalledWith("Forgot localhost:8000. The agent will ask again.");
});
