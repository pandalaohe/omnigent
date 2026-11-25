import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { authenticatedFetch } from "@/lib/identity";
import { interfaceText } from "@/lib/interfaceText";
import { UsageTelemetrySetting } from "./UsageTelemetrySetting";

vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const off = { enabled: false, locked: false, source: "default" };

function renderSetting() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <UsageTelemetrySetting />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
});

it("saves an admin's change and reports the confirmed state", async () => {
  vi.mocked(useIsAdmin).mockReturnValue(true);
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(Response.json({ telemetry: off }))
    .mockResolvedValueOnce(Response.json({ enabled: true, locked: false, source: "setting" }));
  renderSetting();

  const toggle = await screen.findByRole("switch", { name: interfaceText("usageTelemetry") });
  expect(toggle).not.toBeChecked();
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle).toBeChecked());
  expect(authenticatedFetch).toHaveBeenLastCalledWith(
    "/v1/system/telemetry",
    expect.objectContaining({ method: "PUT", body: JSON.stringify({ telemetry_enabled: true }) }),
  );
  expect(screen.getByRole("status")).toHaveTextContent(interfaceText("usageTelemetrySaved"));
});

it("shows an override's source beside a disabled admin toggle", async () => {
  const source = "DO_NOT_TRACK";
  vi.mocked(useIsAdmin).mockReturnValue(true);
  vi.mocked(authenticatedFetch).mockResolvedValue(
    Response.json({ telemetry: { ...off, locked: true, source } }),
  );
  renderSetting();

  const toggle = await screen.findByRole("switch", { name: interfaceText("usageTelemetry") });
  expect(toggle).toBeDisabled();
  expect(screen.getByText(interfaceText("usageTelemetryLocked", { source }))).toBeVisible();
});

it("shows a member the effective state without an editable control", async () => {
  vi.mocked(useIsAdmin).mockReturnValue(false);
  vi.mocked(authenticatedFetch).mockResolvedValue(Response.json({ telemetry: off }));
  renderSetting();

  expect(
    await screen.findByText(interfaceText("usageTelemetryOff"), { exact: true }),
  ).toBeVisible();
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
});
