import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetchMock }));

import type { AutoRotationStatus } from "@/lib/autoRotationApi";
import {
  SESSION_COLLAB_DEFAULTS,
  SESSION_COLLAB_STORAGE_KEY,
  USER_PREFERENCES_DIRTY_STORAGE_KEY,
  writeSessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";
import {
  resetUserPreferencesSyncForTests,
  USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT,
} from "@/lib/userPreferencesSync";
import { AutoRotationChip } from "./AutoRotationChip";

const initialStatus: AutoRotationStatus = {
  eligible: true,
  ineligible_reason: null,
  harness: "claude-native",
  enabled: false,
  compactions_setting: 4,
  remaining_pct_setting: 25,
  sources: {
    enabled: "default",
    compactions: "default",
    remaining_pct: "default",
  },
  compactions: 1,
  remaining_pct: 64,
  approximate: [],
  due: false,
  rotation_pending: false,
};

const clients: QueryClient[] = [];

function mountChip(sessionId = "parent-1", readOnly = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const view = render(
    <QueryClientProvider client={client}>
      <AutoRotationChip sessionId={sessionId} readOnly={readOnly} />
    </QueryClientProvider>,
  );
  return {
    client,
    rerenderChip: (nextSessionId: string, nextReadOnly = readOnly) =>
      view.rerender(
        <QueryClientProvider client={client}>
          <AutoRotationChip sessionId={nextSessionId} readOnly={nextReadOnly} />
        </QueryClientProvider>,
      ),
  };
}

beforeEach(() => {
  localStorage.clear();
  fetchMock.mockResolvedValue({ ok: true, json: async () => initialStatus });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  resetUserPreferencesSyncForTests();
  clients.splice(0).forEach((client) => client.clear());
  vi.resetAllMocks();
});

describe("AutoRotationChip", () => {
  it("renders nothing when the session is ineligible", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        ...initialStatus,
        eligible: false,
        ineligible_reason: "not_top_level",
      }),
    });
    mountChip("child-1");
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/sessions/child-1/auto-rotation",
        expect.any(Object),
      ),
    );
    expect(screen.queryByTestId("auto-rotation-chip-trigger")).toBeNull();
  });

  it("displays status summary, approximate note, pending notice, and round-trips session overrides", async () => {
    writeSessionCollabPreferences({
      ...SESSION_COLLAB_DEFAULTS,
      autoRotationEnabled: true,
      autoRotationCompactions: 6,
      autoRotationRemainingPct: 30,
    });
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        ...initialStatus,
        harness: "pi",
        enabled: true,
        compactions_setting: 6,
        remaining_pct_setting: 30,
        sources: { enabled: "user", compactions: "user", remaining_pct: "user" },
        compactions: 2,
        remaining_pct: null,
        approximate: ["usage", "compaction_point"],
        due: true,
        rotation_pending: true,
      }),
    });

    mountChip();

    const trigger = await screen.findByRole("button", { name: "Auto-rotation: 2/6" });
    fireEvent.click(trigger);

    expect(screen.getByText("Compactions: 2 of 6")).toBeInTheDocument();
    expect(screen.getByText("Context usage not reported yet")).toBeInTheDocument();
    expect(screen.getByText("Approximate for this session type")).toBeInTheDocument();
    expect(screen.getByText("Continues in a new session when this turn ends.")).toBeInTheDocument();

    const select = screen.getByRole("combobox", { name: "Auto-rotation" });
    expect(select).toHaveValue("default");
    expect(screen.getByRole("option", { name: "Use default (On)" })).toBeInTheDocument();

    const compactionsInput = screen.getByRole("spinbutton", { name: "Rotate after compactions" });
    const remainingInput = screen.getByRole("spinbutton", { name: "Context left at rotation" });
    expect(compactionsInput).toHaveAttribute("placeholder", "6");
    expect(remainingInput).toHaveAttribute("placeholder", "30");

    fireEvent.change(select, { target: { value: "off" } });
    fireEvent.change(compactionsInput, { target: { value: "8" } });
    fireEvent.change(remainingInput, { target: { value: "40" } });

    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        ...initialStatus,
        enabled: false,
        compactions_setting: 8,
        remaining_pct_setting: 40,
        sources: { enabled: "session", compactions: "session", remaining_pct: "session" },
        compactions: 2,
        remaining_pct: 55,
      }),
    });

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenLastCalledWith(
        "/v1/sessions/parent-1/auto-rotation",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ enabled: "off", compactions: 8, remaining_pct: 40 }),
        }),
      ),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("Saved");
    expect(screen.getByRole("button", { name: "Auto-rotation: Off" })).toBeInTheDocument();
    expect(screen.getByText("Context left before the next compaction: 55%")).toBeInTheDocument();

    // Clearing inputs sends "default"
    fireEvent.change(select, { target: { value: "default" } });
    fireEvent.change(compactionsInput, { target: { value: "" } });
    fireEvent.change(remainingInput, { target: { value: "" } });

    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => initialStatus,
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenLastCalledWith(
        "/v1/sessions/parent-1/auto-rotation",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({
            enabled: "default",
            compactions: "default",
            remaining_pct: "default",
          }),
        }),
      ),
    );
  });

  it("validates out-of-range values locally and surfaces PATCH errors", async () => {
    mountChip();
    fireEvent.click(await screen.findByRole("button", { name: "Auto-rotation: Off" }));

    const compactionsInput = screen.getByRole("spinbutton", { name: "Rotate after compactions" });
    const remainingInput = screen.getByRole("spinbutton", { name: "Context left at rotation" });

    fireEvent.change(compactionsInput, { target: { value: "25" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Enter a whole number of compactions from 1 to 20.",
    );

    fireEvent.change(compactionsInput, { target: { value: "5" } });
    fireEvent.change(remainingInput, { target: { value: "95" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a whole percentage from 1 to 90.");

    fireEvent.change(remainingInput, { target: { value: "20" } });
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({
        detail: { code: "not_eligible", message: "Session is not eligible for auto-rotation." },
      }),
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Session is not eligible for auto-rotation.",
    );
  });

  it("does not let an in-flight GET overwrite a completed PATCH", async () => {
    const { client } = mountChip();
    fireEvent.click(await screen.findByRole("button", { name: "Auto-rotation: Off" }));

    let finishRead!: (value: unknown) => void;
    let finishWrite!: (value: unknown) => void;
    const staleRead = new Promise((done) => {
      finishRead = done;
    });
    const mutation = new Promise((done) => {
      finishWrite = done;
    });
    fetchMock.mockImplementation((_url: string, init?: RequestInit) =>
      init?.method === "PATCH" ? mutation : staleRead,
    );

    void client.refetchQueries({ queryKey: ["auto-rotation", "parent-1"] });
    fireEvent.change(screen.getByRole("combobox", { name: "Auto-rotation" }), {
      target: { value: "on" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByRole("button", { name: "Saving…" });

    finishWrite({
      ok: true,
      json: async () => ({
        ...initialStatus,
        enabled: true,
        sources: { ...initialStatus.sources, enabled: "session" },
      }),
    });
    await screen.findByRole("button", { name: "Auto-rotation: 1/4" });

    await act(async () => {
      finishRead({ ok: true, json: async () => initialStatus });
      await staleRead;
    });
    expect(screen.getByRole("button", { name: "Auto-rotation: 1/4" })).toBeInTheDocument();
  });

  it("resets unsaved overrides when sessionId changes", async () => {
    const { client, rerenderChip } = mountChip("parent-1");
    client.setQueryData(["auto-rotation", "parent-2"], {
      ...initialStatus,
      enabled: true,
      compactions_setting: 5,
      sources: { ...initialStatus.sources, enabled: "session", compactions: "session" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Auto-rotation: Off" }));

    const compactionsInput = screen.getByRole("spinbutton", { name: "Rotate after compactions" });
    fireEvent.change(compactionsInput, { target: { value: "9" } });
    expect(compactionsInput).toHaveValue(9);

    rerenderChip("parent-2");

    await waitFor(() =>
      expect(screen.getByRole("spinbutton", { name: "Rotate after compactions" })).toHaveValue(5),
    );
    expect(screen.getByRole("combobox", { name: "Auto-rotation" })).toHaveValue("on");
  });

  it("renders read-only notice and omits mutation controls when readOnly is true", async () => {
    mountChip("parent-1", true);
    fireEvent.click(await screen.findByRole("button", { name: "Auto-rotation: Off" }));
    expect(screen.getByText("Read-only here.")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Auto-rotation" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("refreshes only after the server acknowledges a local session_collab preference patch", async () => {
    mountChip("parent-1");
    await screen.findByRole("button", { name: "Auto-rotation: Off" });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Local write fires SESSION_COLLAB_CHANGED_EVENT before the PATCH is sent.
    // The chip must not re-GET yet, otherwise the server would return the old value.
    act(() => {
      writeSessionCollabPreferences({
        ...SESSION_COLLAB_DEFAULTS,
        autoRotationEnabled: true,
        autoRotationCompactions: 6,
      });
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Unrelated namespace acknowledgement must not trigger a re-GET.
    act(() => {
      window.dispatchEvent(
        new CustomEvent(USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT, {
          detail: { namespace: "theme" },
        }),
      );
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        ...initialStatus,
        enabled: true,
        compactions_setting: 6,
        sources: { enabled: "user", compactions: "user", remaining_pct: "default" },
      }),
    });

    act(() => {
      window.dispatchEvent(
        new CustomEvent(USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT, {
          detail: { namespace: "session_collab" },
        }),
      );
    });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("button", { name: "Auto-rotation: 1/6" })).toBeInTheDocument();
  });

  it("waits for cross-tab sync acknowledgement before refreshing on storage events", async () => {
    mountChip("parent-1");
    await screen.findByRole("button", { name: "Auto-rotation: Off" });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Another tab queues a PATCH (marking session_collab dirty) and writes localStorage.
    localStorage.setItem(USER_PREFERENCES_DIRTY_STORAGE_KEY, JSON.stringify(["session_collab"]));
    const updatedRaw = JSON.stringify({
      ...SESSION_COLLAB_DEFAULTS,
      autoRotationEnabled: true,
      autoRotationCompactions: 7,
    });
    localStorage.setItem(SESSION_COLLAB_STORAGE_KEY, updatedRaw);

    act(() => {
      fireEvent(
        window,
        new StorageEvent("storage", {
          key: SESSION_COLLAB_STORAGE_KEY,
          newValue: updatedRaw,
        }),
      );
    });
    // Still dirty in the other tab — must not re-GET yet.
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        ...initialStatus,
        enabled: true,
        compactions_setting: 7,
        sources: { enabled: "user", compactions: "user", remaining_pct: "default" },
      }),
    });

    // Other tab's PATCH is acknowledged and clears the dirty flag.
    localStorage.removeItem(USER_PREFERENCES_DIRTY_STORAGE_KEY);
    act(() => {
      fireEvent(
        window,
        new StorageEvent("storage", {
          key: USER_PREFERENCES_DIRTY_STORAGE_KEY,
          oldValue: JSON.stringify(["session_collab"]),
          newValue: null,
        }),
      );
    });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("button", { name: "Auto-rotation: 1/7" })).toBeInTheDocument();
  });
});
