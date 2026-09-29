// The admin "System status" settings section: loads the four server
// thresholds, saves an edit via PUT, and surfaces a server validation error
// inline.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => true }));

import { SystemStatusSettingsSection } from "./SettingsPage";

const fetchMock = vi.fn();

function mockResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

const SETTINGS = { cpu_pct: 85, mem_pct: 90, disk_pct: 90, server_5xx_pct: 5 };

function renderSection() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SystemStatusSettingsSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("SystemStatusSettingsSection", () => {
  it("loads the server thresholds into the form", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(SETTINGS));
    renderSection();

    expect(await screen.findByLabelText(/CPU threshold/)).toHaveValue(85);
    expect(screen.getByLabelText(/Memory threshold/)).toHaveValue(90);
    expect(screen.getByLabelText(/Disk threshold/)).toHaveValue(90);
    expect(screen.getByLabelText(/Server 5xx failure rate/)).toHaveValue(5);
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/system/settings");
  });

  it("saves an edited threshold with PUT", async () => {
    fetchMock.mockResolvedValue(mockResponse(SETTINGS));
    renderSection();

    fireEvent.change(await screen.findByLabelText(/CPU threshold/), {
      target: { value: "80" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([, init]) => (init as RequestInit | undefined)?.method === "PUT",
        ),
      ).toBe(true);
    });
    const putCall = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === "PUT",
    );
    const putInit = putCall === undefined ? undefined : (putCall[1] as RequestInit);
    expect(JSON.parse(String(putInit?.body))).toEqual({
      ...SETTINGS,
      cpu_pct: 80,
    });
  });

  it("shows a server validation error inline", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(SETTINGS));
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { message: "system-status setting 'cpu_pct' must be in (0, 100]" } },
        400,
      ),
    );
    renderSection();

    fireEvent.change(await screen.findByLabelText(/CPU threshold/), {
      target: { value: "0" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "system-status setting 'cpu_pct' must be in (0, 100]",
    );
  });
});
