import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import {
  AGENT_BADGE_STORAGE_KEY,
  readAgentBadgePreferences,
  writeAgentBadgePreferences,
} from "@/lib/agentBadgePreferences";
import type { CustomAgent, CustomAgentDetail } from "@/lib/customAgentsApi";
import { AgentsSettings } from "./AgentsSettings";

const mocks = vi.hoisted(() => ({
  available: [] as AvailableAgent[],
  catalog: [] as CustomAgent[],
  refetch: vi.fn(),
  createCustomAgent: vi.fn(),
  deleteCustomAgent: vi.fn(),
  getCustomAgent: vi.fn(),
  importCustomAgent: vi.fn(),
  updateCustomAgent: vi.fn(),
  buildAgentBundle: vi.fn(),
  queueUserPreferencePatch: vi.fn(),
}));

vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({ data: mocks.available, isLoading: false, error: null }),
}));

vi.mock("@/lib/customAgentsApi", () => ({
  CUSTOM_AGENTS_QUERY_KEY: ["custom-agents"],
  useCustomAgents: () => ({
    data: mocks.catalog,
    isLoading: false,
    error: null,
    refetch: mocks.refetch,
  }),
  createCustomAgent: mocks.createCustomAgent,
  deleteCustomAgent: mocks.deleteCustomAgent,
  getCustomAgent: mocks.getCustomAgent,
  importCustomAgent: mocks.importCustomAgent,
  updateCustomAgent: mocks.updateCustomAgent,
}));

vi.mock("@/lib/agentBundle", () => ({ buildAgentBundle: mocks.buildAgentBundle }));
vi.mock("@/lib/userPreferencesSync", () => ({
  queueUserPreferencePatch: mocks.queueUserPreferencePatch,
}));
vi.mock("@/lib/agentLabels", () => ({
  BRAIN_HARNESS_LABELS: { codex: "Codex" },
  useBrainHarnessLabels: () => ({ codex: "Codex" }),
}));
vi.mock("@/lib/analytics", () => ({
  useOmnigentAnalytics: () => ({ trackValueChange: vi.fn() }),
}));
vi.mock("@/hooks/useSuppressBrowserView", () => ({ SuppressBrowserView: () => null }));

const builtin: AvailableAgent = {
  id: "ag_builtin_codex_sdk",
  name: "codex-sdk",
  display_name: "Codex SDK",
  description: "Codex chat agent",
  harness: "codex",
  skills: [],
  builtin: true,
  created_at: 1,
};

const custom: CustomAgent = {
  id: "ag_custom_reviewer",
  name: "Reviewer",
  description: "Reviews changes",
  harness: "codex",
  model: null,
  version: 3,
  created_at: 2,
  updated_at: null,
};

const customDetail: CustomAgentDetail = {
  ...custom,
  instructions: "Review carefully.",
};

function renderSettings() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AgentsSettings />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  mocks.available = [builtin];
  mocks.catalog = [custom];
  mocks.getCustomAgent.mockResolvedValue(customDetail);
  mocks.updateCustomAgent.mockResolvedValue(customDetail);
  mocks.deleteCustomAgent.mockResolvedValue(undefined);
  mocks.createCustomAgent.mockResolvedValue(customDetail);
  mocks.buildAgentBundle.mockResolvedValue(
    new File(["bundle"], "agent.tar.gz", { type: "application/gzip" }),
  );
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AgentsSettings", () => {
  it("adds and removes an optional badge on a built-in Agent", async () => {
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "Edit badge for Codex SDK" }));
    fireEvent.click(await screen.findByRole("switch", { name: "Show badge" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Badge text" }), {
      target: { value: "C" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(readAgentBadgePreferences().entries[builtin.id]).toEqual({
      label: "C",
      borderColor: "#8b5cf6",
      textColor: "theme",
    });
    expect(screen.getByTestId("agent-badge")).toHaveTextContent("C");

    fireEvent.click(screen.getByRole("button", { name: "Edit badge for Codex SDK" }));
    fireEvent.click(await screen.findByRole("switch", { name: "Show badge" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(readAgentBadgePreferences().entries[builtin.id]).toBeUndefined());
    expect(screen.queryByTestId("agent-badge")).not.toBeInTheDocument();
  });

  it("shows custom Agent edit and delete controls and awaits both mutations", async () => {
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "Edit Reviewer" }));
    const dialog = await screen.findByRole("dialog");
    const name = await within(dialog).findByRole("textbox", { name: "Name" });
    fireEvent.change(name, { target: { value: "Release reviewer" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mocks.updateCustomAgent).toHaveBeenCalledWith(custom.id, {
        name: "Release reviewer",
        description: custom.description,
        instructions: customDetail.instructions,
        version: custom.version,
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Delete Reviewer" }));
    const deleteDialog = await screen.findByRole("dialog");
    fireEvent.click(within(deleteDialog).getByRole("button", { name: "Delete Agent" }));

    await waitFor(() => expect(mocks.deleteCustomAgent).toHaveBeenCalledWith(custom.id));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("hides every badge globally without discarding saved entries", async () => {
    const entries = {
      [builtin.id]: { label: "C", borderColor: "#112233", textColor: "#ddeeff" },
      [custom.id]: { label: "R", borderColor: "#445566", textColor: "#ffffff" },
    };
    writeAgentBadgePreferences({
      version: 1,
      enabled: true,
      entries,
    });
    renderSettings();
    expect(screen.getAllByTestId("agent-badge")).toHaveLength(2);

    fireEvent.click(screen.getByRole("switch", { name: "Show Agent badges" }));

    await waitFor(() => expect(screen.queryByTestId("agent-badge")).not.toBeInTheDocument());
    const saved = JSON.parse(localStorage.getItem(AGENT_BADGE_STORAGE_KEY) ?? "null") as {
      enabled: boolean;
      entries: Record<string, unknown>;
    };
    expect(saved.enabled).toBe(false);
    expect(saved.entries).toEqual(entries);
  });

  it("keeps create open on an API error and closes only after a successful retry", async () => {
    mocks.createCustomAgent
      .mockRejectedValueOnce(new Error("Agent name already exists"))
      .mockResolvedValueOnce(customDetail);
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "New" }));
    const dialog = await screen.findByTestId("create-agent-dialog");
    fireEvent.change(within(dialog).getByTestId("create-agent-name"), {
      target: { value: "Reviewer" },
    });
    fireEvent.change(within(dialog).getByTestId("create-agent-model"), {
      target: { value: "gpt-default" },
    });
    fireEvent.click(within(dialog).getByTestId("create-agent-submit"));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Agent name already exists");
    expect(screen.getByTestId("create-agent-dialog")).toBeInTheDocument();

    fireEvent.click(within(dialog).getByTestId("create-agent-submit"));
    await waitFor(() => expect(mocks.createCustomAgent).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByTestId("create-agent-dialog")).not.toBeInTheDocument(),
    );
  });
});
