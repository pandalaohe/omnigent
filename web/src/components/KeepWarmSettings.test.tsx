import type * as RegistryModule from "@/hooks/useAgentRegistry";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as HostsModule from "@/hooks/useHosts";
import type * as IdentityModule from "@/lib/identity";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { KeepWarmSettings } from "./KeepWarmSettings";
import {
  KEEP_WARM_STORAGE_KEY,
  readKeepWarmPreferences,
  type KeepWarmLayers,
} from "@/lib/keepWarmPreferences";
import {
  initializeUserPreferencesSync,
  resetUserPreferencesSyncForTests,
} from "@/lib/userPreferencesSync";

interface Preview {
  layers: KeepWarmLayers;
  already: boolean;
  migratedToLayersAt?: number;
}
const state = vi.hoisted(() => ({
  fetch: vi.fn(),
  preview: {} as Preview,
  conversionStatus: 200,
  patchStatus: 200,
  patchGate: null as Promise<Response> | null,
  user: "alice",
  listeners: new Set<() => void>(),
}));
vi.mock("@/lib/identity", async (original) => ({
  ...(await original<typeof IdentityModule>()),
  authenticatedFetch: state.fetch,
  getCurrentUserId: () => state.user,
  subscribeIdentity: (listener: () => void) => {
    state.listeners.add(listener);
    return () => state.listeners.delete(listener);
  },
}));
vi.mock("@/hooks/useHosts", async (original) => ({
  ...(await original<typeof HostsModule>()),
  useHosts: () => ({
    data: [
      {
        host_id: "h1",
        name: "Hawk",
        owner: "alice",
        status: "online",
        configured_harnesses: {
          "claude-native": true,
          "codex-native": false,
          pi: true,
          "pi-native": true,
        },
      },
      {
        host_id: "h2",
        name: "Owl",
        owner: "alice",
        status: "offline",
        configured_harnesses: { "claude-native": false, "codex-native": true },
      },
    ],
  }),
}));
vi.mock("@/lib/agentLabels", async (original) => ({
  ...(await original<typeof AgentLabelsModule>()),
  useHarnessCatalog: (select: (value: unknown) => unknown) =>
    select({
      capabilitiesByHarness: {
        "claude-native": { keep_warm_family: "claude", integration_mode: "native-tui" },
        "claude-sdk": { keep_warm_family: "claude", integration_mode: "sdk-in-process" },
        "codex-native": { keep_warm_family: "codex", integration_mode: "native-tui" },
        pi: { keep_warm_family: "pi", integration_mode: "cli-subprocess" },
        "pi-native": { keep_warm_family: "pi", integration_mode: "native-tui" },
      },
    }),
}));
vi.mock("@/hooks/useAgentRegistry", async (original) => ({
  ...(await original<typeof RegistryModule>()),
  useAgentRegistry: () => ({
    items: [
      {
        key: "harness:claude-native",
        label: "Claude Code",
        harness: "claude-native",
        section: "native",
        source: "builtin",
        deployed: true,
      },
      {
        key: "ca_codex",
        label: "My Codex",
        harness: "codex-native",
        section: "yours",
        source: "library",
        deployed: true,
      },
      {
        key: "direct",
        label: "Direct Claude",
        harness: "claude-native",
        section: "yours",
        source: "installed",
        deployed: true,
      },
      {
        key: "harness:pi",
        label: "Pi",
        harness: "pi",
        section: "sdk",
        source: "builtin",
        deployed: true,
      },
      {
        key: "harness:pi-native",
        label: "Pi",
        harness: "pi-native",
        section: "native",
        source: "builtin",
        deployed: true,
      },
      {
        key: "other",
        label: "Other",
        harness: "other",
        section: "yours",
        source: "installed",
        deployed: true,
      },
    ],
    legacyLabels: {},
    legacyProducts: {},
    isLoading: false,
    isError: false,
    retry: vi.fn(),
  }),
}));

let client: QueryClient;
let server: Record<string, unknown>;
function layersWith(layers: Partial<KeepWarmLayers> = {}): KeepWarmLayers {
  return { corner: {}, hosts: {}, agents: {}, cells: {}, hostWins: {}, ...layers };
}
beforeEach(() => {
  localStorage.clear();
  resetUserPreferencesSyncForTests();
  state.fetch.mockReset();
  state.preview = { layers: layersWith(), already: false, migratedToLayersAt: 123 };
  state.conversionStatus = 200;
  state.patchStatus = 200;
  state.patchGate = null;
  state.user = "alice";
  server = {};
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  state.fetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/keep_warm/convert")) {
      if (state.conversionStatus !== 200)
        return Response.json(
          { detail: "Conversion needs 80 hosts and exceeds 64 KiB" },
          { status: state.conversionStatus },
        );
      return Response.json(state.preview);
    }
    if (url.endsWith("/preferences/keep_warm")) {
      const response = state.patchGate
        ? await state.patchGate
        : Response.json(
            state.patchStatus === 200 ? {} : { detail: "64 KiB limit: 20 hosts, 30 columns" },
            { status: state.patchStatus },
          );
      if (response.ok) server = { ...server, ...JSON.parse(String(init?.body)).value };
      return response;
    }
    return Response.json({});
  });
});
afterEach(() => {
  cleanup();
  resetUserPreferencesSyncForTests();
  client.clear();
});

async function mount(namespace: Record<string, unknown> = {}) {
  server = namespace;
  await initializeUserPreferencesSync(
    { version: 1, settings: { keep_warm: namespace } },
    state.fetch,
    "alice",
  );
  const view = render(
    <QueryClientProvider client={client}>
      <KeepWarmSettings />
    </QueryClientProvider>,
  );
  await waitFor(() =>
    expect(screen.queryByText("Loading keep-warm settings…")).not.toBeInTheDocument(),
  );
  return view;
}
function patches() {
  return state.fetch.mock.calls
    .filter(([url]) => String(url).endsWith("/preferences/keep_warm"))
    .map(([, init]) => JSON.parse(String(init.body)).value as Record<string, unknown>);
}
function latestLayers() {
  return patches().at(-1)?.layers as KeepWarmLayers;
}
function convertCalls() {
  return state.fetch.mock.calls.filter(([url]) => String(url).endsWith("/keep_warm/convert"));
}
function editName(name: string) {
  return new RegExp(`^${name}(:|$)`);
}
function editButton(name: string): HTMLElement {
  return screen.getByRole("button", { name: editName(name) });
}
function openEditor(name: string): HTMLElement {
  fireEvent.click(editButton(name));
  return document.querySelector('[data-slot="popover-content"][data-state="open"]') as HTMLElement;
}
function choose(editor: HTMLElement, group: string, option: string) {
  fireEvent.click(
    within(within(editor).getByRole("radiogroup", { name: group })).getByRole("radio", {
      name: option,
    }),
  );
}
function clickSave(editor: HTMLElement) {
  fireEvent.click(within(editor).getByRole("button", { name: "Save" }));
}
async function saveAndClose(editor: HTMLElement) {
  clickSave(editor);
  await waitFor(() =>
    expect(document.querySelector('[data-slot="popover-content"][data-state="open"]')).toBeNull(),
  );
}

describe("KeepWarmSettings table", () => {
  it("keeps the sticky corner above the sticky first column on one surface", async () => {
    await mount();
    const corner = screen.getByTestId("layered-corner").closest("th") as HTMLElement;
    const rows = within(screen.getByRole("table", { name: "Keep-warm by agent and host" }))
      .getAllByRole("rowheader")
      .map((row) => row as HTMLElement);
    const headers = screen
      .getAllByRole("columnheader")
      .filter((header) => header !== corner) as HTMLElement[];
    // jsdom cannot paint; verify sticky ownership, opaque surfaces and relative layers.
    expect(corner).toHaveClass("sticky", "left-0", "bg-background");
    const layer = (element: HTMLElement) => Number(element.className.match(/\bz-(\d+)\b/)?.[1]);
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      expect(row).toHaveClass("sticky", "left-0", "bg-background");
      expect(layer(corner)).toBeGreaterThan(layer(row));
    }
    expect(headers.length).toBeGreaterThan(0);
    for (const header of headers) expect(header).toHaveClass("bg-background");
  });

  it("distinguishes same-name harness rows and keeps editor targets consistent", async () => {
    state.preview.layers = layersWith({
      cells: {
        h1: { "harness:pi": { child: true }, "harness:pi-native": { main: false, child: false } },
      },
    });
    await mount();
    expect(screen.getByTestId("layered-row-harness:pi")).toHaveTextContent("Pi · …pi");
    expect(screen.getByTestId("layered-row-harness:pi-native")).toHaveTextContent(
      "Pi · …pi-native",
    );
    const native = openEditor("Edit Pi · …pi-native on Hawk");
    expect(within(native).getByText("Pi · …pi-native on Hawk")).toBeInTheDocument();
    choose(native, "Main sessions", "On");
    await saveAndClose(native);
    expect(latestLayers().cells.h1["harness:pi-native"].main).toBe(true);
    expect(latestLayers().cells.h1["harness:pi"]).toEqual({ child: true });
    const sdk = openEditor("Edit Pi · …pi on Hawk");
    expect(within(sdk).getByText("Pi · …pi on Hawk")).toBeInTheDocument();
    expect(
      within(within(sdk).getByRole("radiogroup", { name: "Children" })).getByRole("radio", {
        name: "On",
      }),
    ).toHaveAttribute("aria-checked", "true");
  });

  it("previews the authoritative payload without writing, retaining unknown hosts", async () => {
    state.preview.layers = layersWith({
      cells: {
        h1: { "harness:claude-native": { main: true } },
        removed: { orphan: { coldAfterSeconds: 0 } },
      },
    });
    await mount();
    expect(screen.getAllByRole("columnheader").map((header) => header.textContent)).toEqual([
      "AllMC–4h",
      "No hostMC–4h",
      "HawkMC–4h",
      "OwlMC–4h",
      "removed (unavailable)MC–4h",
    ]);
    expect(within(screen.getByTestId("layered-col-h1")).getByTitle("Online")).toBeInTheDocument();
    expect(within(screen.getByTestId("layered-col-h2")).getByTitle("Offline")).toBeInTheDocument();
    expect(
      screen.getByText("Converted from your previous settings · saved when you first edit"),
    ).toBeInTheDocument();
    expect(editButton("Edit Claude Code on Hawk")).toHaveAccessibleName(
      /main on( \(set here\))?, children off/,
    );
    const claudeRow = screen.getByTestId("layered-row-harness:claude-native").closest("tr");
    expect(claudeRow).toHaveTextContent("Not installed here");
    expect(screen.queryByRole("button", { name: editName("Edit Claude Code on Owl") })).toBeNull();
    expect(
      screen.queryByRole("button", { name: editName("Edit Claude Code on removed") }),
    ).toBeNull();
    expect(patches()).toEqual([]);
    expect(readKeepWarmPreferences().layers).toBeUndefined();
  });

  it("saves the conversion with the first edit in one PATCH and waits for its acknowledgement", async () => {
    state.preview.layers = layersWith({ cells: { h2: { ca_codex: { child: true } } } });
    state.preview.migratedToLayersAt = 456;
    let finish!: (response: Response) => void;
    state.patchGate = new Promise((resolve) => {
      finish = resolve;
    });
    await mount();
    const editor = openEditor("Edit Claude Code on Hawk");
    choose(editor, "Main sessions", "On");
    clickSave(editor);
    await waitFor(() =>
      expect(within(editor).getByRole("button", { name: "Saving…" })).toBeDisabled(),
    );
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(latestLayers().cells).toEqual({
      h1: { "harness:claude-native": { main: true } },
      h2: { ca_codex: { child: true } },
    });
    expect(patches()[0].migratedToLayersAt).toBe(456);
    expect(convertCalls().map(([, init]) => JSON.parse(String(init.body)))).toEqual([
      { dry_run: true, target: "layers" },
    ]);
    expect(editor).toBeInTheDocument();
    finish(Response.json({}));
    await waitFor(() =>
      expect(document.querySelector('[data-slot="popover-content"][data-state="open"]')).toBeNull(),
    );
    expect(editButton("Edit Claude Code on Hawk")).toHaveAccessibleName(
      /main on( \(set here\))?, children off/,
    );
  });

  it("shows a rejected patch and retains the edited draft instead of silently reverting", async () => {
    state.patchStatus = 422;
    await mount();
    const editor = openEditor("Edit Claude Code on Hawk");
    choose(editor, "Main sessions", "On");
    clickSave(editor);
    expect(await within(editor).findByRole("alert")).toHaveTextContent(
      "64 KiB limit: 20 hosts, 30 columns",
    );
    expect(
      within(within(editor).getByRole("radiogroup", { name: "Main sessions" })).getByRole("radio", {
        name: "On",
      }),
    ).toHaveAttribute("aria-checked", "true");
    state.patchStatus = 200;
    await saveAndClose(editor);
    expect(latestLayers().cells.h1["harness:claude-native"]).toEqual({ main: true });
    expect(convertCalls()).toHaveLength(1);
  });

  it("shows the previous settings read-only when the server cannot convert", async () => {
    state.conversionStatus = 404;
    await mount({ default: { main: true }, agents: { direct: { child: true } } });
    expect(screen.getByRole("alert")).toHaveTextContent("Server update needed");
    expect(screen.getByText("Previous settings")).toBeInTheDocument();
    expect(screen.getByText("Default: main on · children off")).toBeInTheDocument();
    expect(screen.getByText("Direct Claude: main off · children on")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Select cells" })).toBeNull();
    expect(screen.getByRole("spinbutton", { name: /Archive children/ })).toBeDisabled();
    expect(patches()).toEqual([]);
  });

  it("discards a rejected draft only when the user asks to", async () => {
    state.patchStatus = 422;
    await mount();
    const editor = openEditor("Edit Claude Code on Hawk");
    choose(editor, "Main sessions", "On");
    clickSave(editor);
    await within(editor).findByRole("alert");
    expect(editButton("Edit Claude Code on Hawk")).toHaveAccessibleName(
      /main on( \(set here\))?, children off/,
    );
    fireEvent.click(screen.getByRole("button", { name: "Discard unsaved changes" }));
    await waitFor(() =>
      expect(editButton("Edit Claude Code on Hawk")).toHaveAccessibleName(
        /main off( \(set here\))?, children off/,
      ),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("preserves saved values of an agent with no row when another cell is edited", async () => {
    state.preview.layers = layersWith({
      cells: { h1: { other: { intervalSeconds: 600, maxSeconds: 7200, coldAfterSeconds: 0 } } },
    });
    await mount();
    expect(screen.queryByTestId("layered-row-other")).toBeNull();
    const editor = openEditor("Edit Claude Code on Hawk");
    choose(editor, "Main sessions", "On");
    await saveAndClose(editor);
    expect(latestLayers().cells.h1.other).toEqual({
      intervalSeconds: 600,
      maxSeconds: 7200,
      coldAfterSeconds: 0,
    });
  });

  it("shows a conversion 422 with a retry and no table to edit", async () => {
    state.conversionStatus = 422;
    await mount();
    expect(screen.getByRole("alert")).toHaveTextContent("80 hosts");
    expect(screen.queryByRole("table")).toBeNull();
    expect(patches()).toEqual([]);
    expect(readKeepWarmPreferences().layers).toBeUndefined();
    state.conversionStatus = 200;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(
      await screen.findByRole("table", { name: "Keep-warm by agent and host" }),
    ).toBeInTheDocument();
  });

  it("writes a host header to its one column and keeps Ping every per agent", async () => {
    state.preview.layers = layersWith({
      cells: { h2: { ca_codex: { child: true, coldAfterSeconds: 0 } } },
    });
    await mount();
    const column = openEditor("Edit Hawk");
    expect(within(column).queryByRole("spinbutton", { name: "Ping every in minutes" })).toBeNull();
    expect(
      within(column).getByText("Ping every is set per agent: its range depends on the agent."),
    ).toBeInTheDocument();
    choose(column, "Main sessions", "On");
    await saveAndClose(column);
    expect(latestLayers().hosts).toEqual({ h1: { main: true } });
    expect(latestLayers().cells).toEqual({
      h2: { ca_codex: { child: true, coldAfterSeconds: 0 } },
    });
    expect(editButton("Edit Claude Code on Hawk")).toHaveAccessibleName(
      /main on( \(set here\))?, children off/,
    );
    const codex = openEditor("Edit My Codex");
    expect(
      within(codex).getByRole("spinbutton", { name: "Ping every in minutes" }),
    ).toHaveAttribute("max", "29");
    fireEvent.click(within(codex).getByRole("button", { name: "Cancel" }));
    const claude = openEditor("Edit Claude Code");
    expect(
      within(claude).getByRole("spinbutton", { name: "Ping every in minutes" }),
    ).toHaveAttribute("max", "59");
  });

  it("writes an agent header to its one row and clears Cold after to the platform rule", async () => {
    state.preview.layers = layersWith({
      agents: { "harness:claude-native": { coldAfterSeconds: 0 } },
      cells: { h1: { ca_codex: { child: true } } },
    });
    await mount();
    const editor = openEditor("Edit Claude Code");
    choose(editor, "Main sessions", "On");
    fireEvent.change(within(editor).getByRole("spinbutton", { name: "Cold after in minutes" }), {
      target: { value: "" },
    });
    await saveAndClose(editor);
    expect(latestLayers().agents).toEqual({
      "harness:claude-native": { main: true, coldAfterSeconds: null },
    });
    expect(latestLayers().cells).toEqual({ h1: { ca_codex: { child: true } } });
  });

  it("turns one cell off under a warm corner without touching its neighbours", async () => {
    state.preview.layers = layersWith({ corner: { main: true, child: true } });
    await mount();
    const editor = openEditor("Edit Direct Claude on Hawk");
    choose(editor, "Main sessions", "Off");
    choose(editor, "Children", "Off");
    await saveAndClose(editor);
    expect(latestLayers().cells).toEqual({ h1: { direct: { main: false, child: false } } });
    expect(editButton("Edit Direct Claude on Hawk")).toHaveAccessibleName(
      /main off( \(set here\))?, children off/,
    );
    expect(editButton("Edit Claude Code on Hawk")).toHaveAccessibleName(
      /main on( \(set here\))?, children on/,
    );
  });

  it("retains a first edit across a normal remount", async () => {
    const first = await mount();
    const editor = openEditor("Edit Claude Code on Hawk");
    choose(editor, "Main sessions", "On");
    await saveAndClose(editor);
    first.unmount();
    client.clear();
    state.preview = { layers: server.layers as KeepWarmLayers, already: true };
    await mount(server);
    expect(editButton("Edit Claude Code on Hawk")).toHaveAccessibleName(
      /main on( \(set here\))?, children off/,
    );
    expect(
      screen.queryByText("Converted from your previous settings · saved when you first edit"),
    ).toBeNull();
    expect(localStorage.getItem(KEEP_WARM_STORAGE_KEY)).not.toBeNull();
  });

  it("uses the same conversion and acknowledged error path for the archive control", async () => {
    state.patchStatus = 422;
    await mount();
    fireEvent.change(screen.getByRole("spinbutton", { name: /Archive children/ }), {
      target: { value: "0" },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("64 KiB limit");
    expect(patches()[0].hostOfflineArchiveSeconds).toBe(0);
    expect(patches()[0].layers).toEqual(layersWith());
    expect(convertCalls()).toHaveLength(1);
  });

  it("hides Ping every for Pi, exposes its actual cap and lists agents with no channel", async () => {
    await mount();
    const editor = openEditor("Edit Pi · …pi");
    expect(within(editor).queryByRole("spinbutton", { name: /Ping every/ })).toBeNull();
    expect(within(editor).getByText("30 min (Pi limit)")).toBeInTheDocument();
    fireEvent.click(within(editor).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("button", { name: editName("Edit Other") })).toBeNull();
    expect(screen.getByText("No keep-warm:").closest("p")).toHaveTextContent("Other");
  });

  it("refuses an edit made after the signed-in owner changed", async () => {
    await mount();
    const editor = openEditor("Edit Claude Code on Hawk");
    choose(editor, "Main sessions", "On");
    state.user = "bob";
    clickSave(editor);
    // Past the 250 ms patch debounce, so a queued write would have been sent.
    await new Promise((resolve) => {
      setTimeout(resolve, 400);
    });
    expect(patches()).toEqual([]);
  });
});
