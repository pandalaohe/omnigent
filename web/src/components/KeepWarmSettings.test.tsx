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
  type KeepWarmCells,
} from "@/lib/keepWarmPreferences";
import {
  initializeUserPreferencesSync,
  resetUserPreferencesSyncForTests,
} from "@/lib/userPreferencesSync";

interface Preview {
  cells: KeepWarmCells;
  columns: string[];
  already: boolean;
  migratedToCellsAt?: number;
}
const state = vi.hoisted(() => ({
  fetch: vi.fn(),
  preview: {} as Preview,
  actual: null as Preview | null,
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
        configured_harnesses: { "claude-native": true, "codex-native": false, pi: true },
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
const columns = ["harness:claude-native", "ca_codex", "harness:pi"];
beforeEach(() => {
  localStorage.clear();
  resetUserPreferencesSyncForTests();
  state.fetch.mockReset();
  state.preview = { cells: {}, columns: [...columns], already: false, migratedToCellsAt: 123 };
  state.actual = null;
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
      const dry = JSON.parse(String(init?.body)).dry_run;
      const result = dry ? state.preview : (state.actual ?? state.preview);
      if (!dry) server = { ...server, cells: result.cells, migratedToCellsAt: 123 };
      return Response.json(result);
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
function latestCells() {
  return patches().at(-1)?.cells as KeepWarmCells;
}
async function saveCell() {
  fireEvent.click(screen.getByRole("button", { name: "Save cell" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
}

describe("KeepWarmSettings table", () => {
  it("keeps the sticky corner above scrolling headers in one table layer", async () => {
    await mount();
    const table = screen.getByRole("table", { name: "Keep-warm settings by host and agent" });
    const corner = within(table).getByRole("columnheader", { name: "Host / Agent" });
    const headers = within(table)
      .getAllByRole("columnheader")
      .filter((header) => header !== corner);
    const rows = within(table).getAllByRole("rowheader");
    // jsdom cannot paint; verify sticky ownership, opaque surfaces and relative layers.
    expect(corner.closest("thead")).not.toHaveClass("sticky");
    expect(corner).toHaveClass("sticky", "top-0", "left-0", "bg-card-solid");
    const layer = (element: HTMLElement) => Number(element.className.match(/\bz-(\d+)\b/)?.[1]);
    expect(headers.length).toBeGreaterThan(0);
    for (const header of headers) {
      expect(header).toHaveClass("sticky", "top-0", "bg-card-solid");
      expect(layer(corner)).toBeGreaterThan(layer(header));
      for (const row of rows) expect(layer(header)).toBeGreaterThan(layer(row));
    }
  });

  it("distinguishes same-name harness columns and keeps editor targets consistent", async () => {
    state.preview.columns = ["harness:pi", "harness:pi-native"];
    state.preview.cells = {
      h1: { "harness:pi": { child: true }, "harness:pi-native": { main: false, child: false } },
    };
    await mount();
    const table = screen.getByRole("table", { name: "Keep-warm settings by host and agent" });
    const sdkHeader = within(table).getByRole("columnheader", { name: /^Pi · pi(?: |$)/ });
    const nativeHeader = within(table).getByRole("columnheader", {
      name: /^Pi · pi-native(?: |$)/,
    });
    expect(within(sdkHeader).getByText("Pi · pi", { exact: true })).toBeVisible();
    expect(within(nativeHeader).getByText("Pi · pi-native", { exact: true })).toBeVisible();
    expect(screen.getByRole("button", { name: "Remove Pi · pi column" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Remove Pi · pi-native column" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit Hawk / Pi · pi-native" }));
    expect(screen.getByRole("dialog", { name: "Hawk / Pi · pi-native" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "Keep main sessions warm" }));
    await saveCell();
    expect(latestCells()).toEqual({
      h1: { "harness:pi": { child: true }, "harness:pi-native": { main: true } },
    });
    fireEvent.click(screen.getByRole("button", { name: "Edit Hawk / Pi · pi" }));
    expect(screen.getByRole("dialog", { name: "Hawk / Pi · pi" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Keep children warm" })).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove Pi · pi-native column" }));
    expect(
      screen.getByRole("dialog", { name: "Remove Pi · pi-native column?" }),
    ).toBeInTheDocument();
  });

  it("previews the authoritative payload without writing, retaining off columns and unknown hosts", async () => {
    state.preview.cells = {
      h1: { "harness:claude-native": { main: true } },
      removed: { orphan: { coldAfterSeconds: 0 } },
    };
    state.preview.columns.push("harness:claude-sdk");
    await mount();
    expect(
      screen.getByText("Converted from your previous settings · saved when you first edit"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit Hawk / Claude Code" })).toHaveTextContent(
      "Main on",
    );
    expect(
      screen.getByRole("button", { name: "Edit Server / harness:claude-sdk" }),
    ).toHaveTextContent("Main off");
    expect(screen.getByRole("button", { name: "Edit removed / orphan" })).toHaveTextContent(
      "not deployed",
    );
    expect(screen.getByRole("button", { name: "Edit Owl / Claude Code" })).toHaveTextContent(
      "not deployed",
    );
    expect(screen.getByRole("button", { name: "Edit Hawk / Claude Code" })).not.toHaveTextContent(
      "not deployed",
    );
    expect(patches()).toEqual([]);
    expect(readKeepWarmPreferences().cells).toBeUndefined();
  });

  it("converts once, applies an edit to the actual response, and waits for PATCH acknowledgement", async () => {
    state.actual = {
      cells: { h2: { ca_codex: { child: true } } },
      columns: [...columns, "race-agent"],
      already: true,
      migratedToCellsAt: 456,
    };
    let finish!: (response: Response) => void;
    state.patchGate = new Promise((resolve) => {
      finish = resolve;
    });
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Edit Hawk / Claude Code" }));
    fireEvent.click(screen.getByRole("switch", { name: "Keep main sessions warm" }));
    fireEvent.click(screen.getByRole("button", { name: "Save cell" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled());
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(latestCells()).toEqual({
      h1: { "harness:claude-native": { main: true } },
      h2: { ca_codex: { child: true } },
    });
    expect(patches()[0].columns).toContain("race-agent");
    expect(patches()[0].migratedToCellsAt).toBe(456);
    expect(patches()[0]).not.toHaveProperty("default");
    expect(patches()[0]).not.toHaveProperty("agents");
    const calls = state.fetch.mock.calls.filter(([url]) => String(url).includes("keep_warm"));
    expect(calls.slice(0, 3).map(([url, init]) => [url, JSON.parse(String(init.body))])).toEqual([
      ["/v1/me/preferences/keep_warm/convert", { dry_run: true }],
      ["/v1/me/preferences/keep_warm/convert", { dry_run: false }],
      ["/v1/me/preferences/keep_warm", { value: patches()[0] }],
    ]);
    finish(Response.json({}));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("shows a rejected patch and retains the edited draft instead of silently reverting", async () => {
    state.patchStatus = 422;
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Edit Hawk / Claude Code" }));
    fireEvent.click(screen.getByRole("switch", { name: "Keep main sessions warm" }));
    fireEvent.click(screen.getByRole("button", { name: "Save cell" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "64 KiB limit: 20 hosts, 30 columns",
    );
    expect(screen.getByRole("switch", { name: "Keep main sessions warm" })).toBeChecked();
    state.patchStatus = 200;
    await saveCell();
    expect(latestCells().h1["harness:claude-native"]).toEqual({ main: true });
    expect(
      state.fetch.mock.calls.filter(
        ([, init]) => JSON.parse(String(init?.body ?? "{}")).dry_run === false,
      ),
    ).toHaveLength(1);
  });

  it("keeps legacy settings when conversion is refused, and makes a 404 read-only", async () => {
    state.conversionStatus = 404;
    await mount({ default: { main: true }, agents: { direct: { child: true } } });
    expect(screen.getByRole("alert")).toHaveTextContent("Server update needed");
    expect(screen.getByText("Default: main on · children off")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add agent" })).toBeDisabled();
    expect(screen.getByRole("spinbutton", { name: /Archive children/ })).toBeDisabled();
    expect(patches()).toEqual([]);
  });

  it("discards a rejected draft only when the user cancels it", async () => {
    state.patchStatus = 422;
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Edit Hawk / Claude Code" }));
    fireEvent.click(screen.getByRole("switch", { name: "Keep main sessions warm" }));
    fireEvent.click(screen.getByRole("button", { name: "Save cell" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Edit Hawk / Claude Code" })).toHaveTextContent(
      "Main off",
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("preserves uneditable values on a saved non-participant while clearing cold-after", async () => {
    state.preview.columns = ["other"];
    state.preview.cells = {
      h1: { other: { intervalSeconds: 600, maxSeconds: 7200, coldAfterSeconds: 0 } },
    };
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Edit Hawk / Other" }));
    expect(screen.getByRole("switch", { name: "Keep main sessions warm" })).toBeDisabled();
    fireEvent.change(screen.getByRole("spinbutton", { name: "Cold after in minutes" }), {
      target: { value: "" },
    });
    await saveCell();
    expect(latestCells()).toEqual({ h1: { other: { intervalSeconds: 600, maxSeconds: 7200 } } });
  });

  it("shows a conversion 422 without queuing a preference edit", async () => {
    const view = await mount();
    state.conversionStatus = 422;
    fireEvent.click(screen.getByRole("button", { name: "Edit Hawk / Claude Code" }));
    fireEvent.click(screen.getByRole("switch", { name: "Keep main sessions warm" }));
    fireEvent.click(screen.getByRole("button", { name: "Save cell" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("80 hosts");
    expect(patches()).toEqual([]);
    expect(readKeepWarmPreferences().cells).toBeUndefined();
    view.unmount();
  });

  it("fills only one host row and clamps each destination family's interval", async () => {
    state.preview.cells = { h2: { ca_codex: { child: true, coldAfterSeconds: 0 } } };
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Edit Hawk / Claude Code" }));
    fireEvent.click(screen.getByRole("switch", { name: "Keep main sessions warm" }));
    fireEvent.click(screen.getByRole("button", { name: "Fill host row" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(latestCells()).toEqual({
      h2: { ca_codex: { child: true, coldAfterSeconds: 0 } },
      h1: {
        "harness:claude-native": { main: true },
        ca_codex: { main: true, intervalSeconds: 1740 },
        "harness:pi": { main: true },
      },
    });
  });

  it("fills only one agent column and clears Cold after Auto cleanly", async () => {
    state.preview.cells = {
      h1: { "harness:claude-native": { coldAfterSeconds: 0 }, ca_codex: { child: true } },
    };
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Edit Hawk / Claude Code" }));
    fireEvent.click(screen.getByRole("switch", { name: "Keep main sessions warm" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "Cold after in minutes" }), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Fill agent column" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(latestCells()).toEqual({
      "": { "harness:claude-native": { main: true } },
      h1: { "harness:claude-native": { main: true }, ca_codex: { child: true } },
      h2: { "harness:claude-native": { main: true } },
    });
  });

  it("turns a cell off with a mask, while column removal confirms harness fallback", async () => {
    state.preview.columns = ["harness:claude-native", "direct"];
    state.preview.cells = {
      h1: { "harness:claude-native": { main: true }, direct: { child: true } },
    };
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Edit Hawk / Direct Claude" }));
    fireEvent.click(screen.getByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(latestCells().h1.direct).toEqual({ main: false, child: false });
    expect(screen.getByRole("button", { name: "Edit Hawk / Direct Claude" })).toHaveTextContent(
      "Main off",
    );
    fireEvent.click(screen.getByRole("button", { name: "Remove Direct Claude column" }));
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "Sessions may then follow matching harness cells",
    );
    fireEvent.click(screen.getByRole("button", { name: /^Remove column$/ }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(latestCells().h1).toEqual({ "harness:claude-native": { main: true } });
    expect(
      screen.queryByRole("button", { name: "Edit Hawk / Direct Claude" }),
    ).not.toBeInTheDocument();
  });

  it("retains first-convert off columns and added axes across a normal remount", async () => {
    state.preview.columns = ["harness:claude-native"];
    const first = await mount();
    fireEvent.click(screen.getByRole("button", { name: "Add agent" }));
    fireEvent.click(screen.getByRole("button", { name: /My Codex.*Your agents/ }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Edit Hawk / My Codex" })).toBeEnabled(),
    );
    expect(patches()[0]).toMatchObject({
      cells: {},
      columns: ["harness:claude-native", "ca_codex"],
    });
    first.unmount();
    client.clear();
    state.preview = { cells: {}, columns: [], already: true };
    await mount(server);
    expect(screen.getByRole("button", { name: "Edit Server / Claude Code" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit Server / My Codex" })).toBeInTheDocument();
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
    expect(
      state.fetch.mock.calls.filter(
        ([, init]) => JSON.parse(String(init?.body ?? "{}")).dry_run === false,
      ),
    ).toHaveLength(1);
  });

  it("hides Every for Pi, exposes its actual cap and filters unsupported picker choices", async () => {
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Edit Hawk / Pi" }));
    expect(screen.queryByRole("spinbutton", { name: /interval/ })).not.toBeInTheDocument();
    expect(screen.getByText("30 min (Pi limit)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Add agent" }));
    fireEvent.click(screen.getByRole("button", { name: /Not supported here/ }));
    expect(
      within(screen.getByRole("dialog", { name: "Add agent" })).getByRole("button", {
        name: /Other.*no declared keep-warm channel/,
      }),
    ).toBeDisabled();
  });

  it("drops an in-flight edit on disposal before it can patch another owner", async () => {
    const view = await mount();
    let converted!: (response: Response) => void;
    state.fetch.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          converted = resolve;
        }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit Hawk / Claude Code" }));
    fireEvent.click(screen.getByRole("switch", { name: "Keep main sessions warm" }));
    fireEvent.click(screen.getByRole("button", { name: "Save cell" }));
    view.unmount();
    state.user = "bob";
    converted(Response.json(state.preview));
    await Promise.resolve();
    expect(patches()).toEqual([]);
  });
});
