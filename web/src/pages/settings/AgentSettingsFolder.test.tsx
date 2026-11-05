import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { agentFolderQueryKey, type AgentFolderSettings } from "@/lib/agentFolderSettings";
import type { Host } from "@/hooks/useHosts";
import { AgentSettingsFolder } from "./AgentSettingsFolder";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/shell/RevealInFileManager", () => ({
  useDesktopHostId: () => null,
  revealLabel: () => "Open",
}));
vi.mock("@/shell/WorkspacePickerDialog", () => ({
  WorkspacePickerDialog: ({ onConfirm }: { onConfirm: (path: string) => void }) => (
    <button type="button" onClick={() => onConfirm("/opt/work/second")}>
      Choose second folder
    </button>
  ),
}));

const host: Host = { host_id: "desktop", name: "Desktop", owner: "local", status: "online" };
const initial: AgentFolderSettings = {
  family: "codex",
  root: "/opt/work/first",
  choosable: true,
  exists: true,
  revision: "r1",
  read_only_reason: null,
  account: { kind: "chatgpt", label: "Personal subscription" },
  fields: [
    { key: "model", present: true, value: "model-one", writable: true, undo_available: false },
    {
      key: "model_reasoning_effort",
      present: false,
      value: null,
      writable: true,
      undo_available: false,
    },
  ],
};
let view: AgentFolderSettings;
let requests: { path: string; method: string; body: Record<string, unknown> }[];
let refusal: { status: number; error: string } | null;
let readError: number | null;
let ignoreWrite: boolean;

function mount(target = host, harness = "codex-native") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const tree = (next: Host, nextHarness = harness) => (
    <QueryClientProvider client={client}>
      <AgentSettingsFolder host={next} harness={nextHarness} />
    </QueryClientProvider>
  );
  return { ...render(tree(target)), client, tree };
}

beforeEach(() => {
  view = structuredClone(initial);
  requests = [];
  refusal = null;
  readError = null;
  ignoreWrite = false;
  vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    requests.push({ path, method, body });
    if (path.includes("model-options"))
      return Response.json({
        models: [{ id: view.root.endsWith("second") ? "second-model" : "first-model" }],
      });
    if (readError) return Response.json({ detail: "Unavailable" }, { status: readError });
    if (method !== "GET" && refusal)
      return Response.json({ error: refusal.error }, { status: refusal.status });
    if (method === "PUT" && path.endsWith("/root"))
      view = {
        ...view,
        root: String(body.root),
        account: { kind: "none", label: "Not signed in under this folder" },
      };
    else if (method === "PUT" && !ignoreWrite)
      view = {
        ...view,
        revision: "r2",
        fields: view.fields.map((field) =>
          field.key === body.key
            ? { ...field, present: true, value: String(body.value), undo_available: true }
            : field,
        ),
      };
    else if (method === "POST") view = structuredClone(initial);
    return Response.json(view);
  });
});
afterEach(() => {
  cleanup();
  focusManager.setFocused(undefined);
  vi.clearAllMocks();
});

describe("Settings folder", () => {
  it.each([
    ["claude-native", "claude", "claude-model"],
    ["codex", "codex", "model-two"],
    ["codex-sdk", "codex", "model-two"],
  ])(
    "preserves a %s read when a pending Codex save completes",
    async (nextHarness, family, expectedModel) => {
      const fetch = vi.mocked(authenticatedFetch).getMockImplementation()!;
      let finishSave: ((response: Response) => void) | undefined;
      let finishRead: ((response: Response) => void) | undefined;
      let pendingRead: Promise<Response> | undefined;
      let confirmed = false;
      const saved = {
        ...initial,
        revision: "r2",
        fields: [{ ...initial.fields[0], value: "model-two", undo_available: true }],
      };
      vi.mocked(authenticatedFetch).mockImplementation((input, init) => {
        if (String(input).endsWith("/agent-settings/codex-native") && init?.method === "PUT")
          return new Promise((resolve) => {
            finishSave = (response) => {
              confirmed = true;
              resolve(response);
            };
          });
        if (String(input).endsWith(`/agent-settings/${nextHarness}`)) {
          if (confirmed)
            return Promise.resolve(
              Response.json(
                family === "codex"
                  ? saved
                  : {
                      ...initial,
                      family,
                      fields: [{ ...initial.fields[0], value: "claude-model" }],
                    },
              ),
            );
          pendingRead = new Promise((resolve) => {
            finishRead = resolve;
          });
          return pendingRead;
        }
        return fetch(input, init);
      });
      const mounted = mount();
      if (nextHarness === "codex")
        mounted.client.setQueryData(agentFolderQueryKey(host.host_id, nextHarness), initial);
      await screen.findByText("model-one");
      fireEvent.click(screen.getByRole("button", { name: "Change Model" }));
      fireEvent.change(screen.getByLabelText("New Model"), { target: { value: "model-two" } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => expect(finishSave).toBeDefined());
      mounted.rerender(mounted.tree(host, nextHarness));
      await waitFor(() => expect(finishRead).toBeDefined());
      act(() => finishSave!(Response.json(saved)));
      await waitFor(() =>
        expect(
          mounted.client.getQueryData(agentFolderQueryKey(host.host_id, "codex-native")),
        ).toEqual(saved),
      );
      const read = Response.json({
        ...initial,
        family,
        fields: [
          { ...initial.fields[0], value: family === "codex" ? "model-one" : "claude-model" },
        ],
      });
      const parsedRead = vi.spyOn(read, "json");
      await act(async () => {
        finishRead!(read);
        await pendingRead;
      });
      await waitFor(() => expect(parsedRead).toHaveBeenCalled());
      await screen.findByText(expectedModel);
      expect(screen.queryByText("Loading settings…")).toBeNull();
      if (family === "codex")
        expect(screen.getByRole("button", { name: "Undo Model" })).toBeTruthy();
    },
  );

  it("keeps the saved value and Undo when a focus read returns an older view after Save", async () => {
    const fetch = vi.mocked(authenticatedFetch).getMockImplementation()!;
    let finishSave: ((response: Response) => void) | undefined;
    let finishRead: ((response: Response) => void) | undefined;
    let lateRead: Promise<Response> | undefined;
    vi.mocked(authenticatedFetch).mockImplementation((input, init) => {
      if (String(input).includes("agent-settings")) {
        if (init?.method === "PUT")
          return new Promise((resolve) => {
            finishSave = resolve;
          });
        if (finishSave) {
          lateRead = new Promise((resolve) => {
            finishRead = resolve;
          });
          return lateRead;
        }
      }
      return fetch(input, init);
    });
    mount();
    await screen.findByText("model-one");
    fireEvent.click(screen.getByRole("button", { name: "Change Model" }));
    fireEvent.change(screen.getByLabelText("New Model"), { target: { value: "model-two" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(finishSave).toBeDefined());
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await waitFor(() => expect(finishRead).toBeDefined());
    const saved = {
      ...initial,
      revision: "r2",
      fields: [{ ...initial.fields[0], value: "model-two", undo_available: true }],
    };
    act(() => finishSave!(Response.json(saved)));
    await screen.findByText("Model saved.");
    const staleResponse = Response.json(initial);
    const parsedRead = vi.spyOn(staleResponse, "json");
    await act(async () => {
      finishRead!(staleResponse);
      await lateRead;
    });
    await waitFor(() => expect(parsedRead).toHaveBeenCalled());
    expect(screen.getByText("model-two")).toBeTruthy();
    expect(screen.queryByText("model-one")).toBeNull();
    expect(screen.getByRole("button", { name: "Undo Model" })).toBeTruthy();
    expect(screen.getByText("Model saved.")).toBeTruthy();
  });

  it.each(["opencode-native", "acp:research"])(
    "shows no Retry for an unknown settings folder (%s)",
    async (harness) => {
      vi.mocked(authenticatedFetch).mockResolvedValue(
        Response.json({ error: "Settings folder not known" }, { status: 400 }),
      );
      mount(host, harness);
      await screen.findByText("Settings folder not known");
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    },
  );

  it("reads without writing, then saves one key with its revision and restores its preimage", async () => {
    mount();
    await screen.findByText("model-one");
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(requests.every((request) => request.method === "GET")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Change Thinking" }));
    fireEvent.change(screen.getByRole("textbox", { name: "New Thinking" }), {
      target: { value: "high" },
    });
    expect(screen.getByText(/Applies outside Omnigent too/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Thinking saved.");
    expect(requests.find((request) => request.method === "PUT")?.body).toEqual({
      key: "model_reasoning_effort",
      value: "high",
      revision: "r1",
    });
    fireEvent.click(screen.getByRole("button", { name: "Undo Thinking" }));
    await screen.findByText("Thinking restored.");
    expect(requests.find((request) => request.method === "POST")).toMatchObject({
      path: "/v1/hosts/desktop/agent-settings/codex-native/undo",
      body: { key: "model_reasoning_effort" },
    });
    expect(screen.getByText("Not set")).toBeTruthy();
  });

  it("cancels a draft without writing and clears it when switching hosts", async () => {
    const mounted = mount();
    await screen.findByText("model-one");
    fireEvent.click(screen.getByRole("button", { name: "Change Model" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(requests.every((request) => request.method === "GET")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Change Model" }));
    mounted.rerender(mounted.tree({ ...host, host_id: "second-host" }));
    await screen.findByText("model-one");
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(requests.some((request) => request.path.includes("second-host/agent-settings"))).toBe(
      true,
    );
  });

  it("switches the root and account, and discards the previous model catalog", async () => {
    mount();
    await screen.findByText("model-one");
    fireEvent.click(screen.getByRole("button", { name: "Change Model" }));
    await waitFor(() => expect(document.querySelector('option[value="first-model"]')).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Change settings folder" }));
    fireEvent.click(screen.getByRole("button", { name: "Choose second folder" }));
    await screen.findByText("Settings folder changed.");
    expect(screen.getByText("/opt/work/second")).toBeTruthy();
    expect(screen.getByText("Not signed in under this folder")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Change Model" }));
    await waitFor(() =>
      expect(document.querySelector('option[value="second-model"]')).toBeTruthy(),
    );
    expect(document.querySelector('option[value="first-model"]')).toBeNull();
  });

  it.each(["Changed since", "Changed since. Not undone."])(
    "reports %s and reads the new revision",
    async (error) => {
      const undo = error === "Changed since. Not undone.";
      if (undo) view.fields[0].undo_available = true;
      mount();
      await screen.findByText("model-one");
      if (!undo) fireEvent.click(screen.getByRole("button", { name: "Change Model" }));
      refusal = { status: 409, error };
      view = {
        ...view,
        revision: "r3",
        fields: [{ ...initial.fields[0], value: "external-model" }],
      };
      fireEvent.click(screen.getByRole("button", { name: undo ? "Undo Model" : "Save" }));
      await screen.findByText(
        error === "Changed since"
          ? "Changed since. Review the current values and try again."
          : error,
      );
      await screen.findByText("external-model");
      expect(screen.queryByRole("textbox")).toBeNull();
      expect(screen.queryByText("Model saved.")).toBeNull();
    },
  );

  it("keeps a JSONC file read only and hides changes for a fixed root", async () => {
    view = {
      ...view,
      family: "agy-acp",
      choosable: false,
      read_only_reason: "Has comments or isn't plain JSON. Change it by hand.",
      fields: [{ ...initial.fields[0], writable: false, undo_available: true }],
    };
    mount();
    await screen.findByText(/Has comments/);
    expect(screen.queryByRole("button", { name: /Change/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Undo/ })).toBeNull();
  });

  it.each([404, 501, 502])("shows an actionable read failure (%s)", async (status) => {
    readError = status;
    mount();
    await screen.findByRole("alert");
    expect(
      screen.getByText(
        status === 502
          ? "Couldn't reach the settings folder. Try again."
          : "Update this host to see its settings.",
      ),
    ).toBeTruthy();
    readError = null;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("model-one");
  });

  it("never requests an offline host", () => {
    mount({ ...host, status: "offline" });
    expect(screen.getByText(/Host offline/)).toBeTruthy();
    expect(requests).toHaveLength(0);
  });

  it("shows a refused write and keeps the draft available", async () => {
    mount();
    await screen.findByText("model-one");
    fireEvent.click(screen.getByRole("button", { name: "Change Model" }));
    refusal = { status: 400, error: "This setting isn't writable here." };
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("This setting isn't writable here.");
    expect(screen.getByLabelText("New Model")).toBeTruthy();
    expect(screen.queryByText("Model saved.")).toBeNull();
  });

  it("reports a read-back value that the agent overwrote instead of claiming success", async () => {
    ignoreWrite = true;
    mount();
    await screen.findByText("model-one");
    fireEvent.click(screen.getByRole("button", { name: "Change Model" }));
    fireEvent.change(screen.getByLabelText("New Model"), {
      target: { value: "rejected-model" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("The agent didn't keep this value. Current settings are shown.");
    expect(screen.getByText("model-one")).toBeTruthy();
    expect(screen.queryByText("Model saved.")).toBeNull();
  });
});
