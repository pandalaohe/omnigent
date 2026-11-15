import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { QuotaPreferencesSettings } from "./QuotaPreferencesSettings";
import type * as AgentRegistry from "@/hooks/useAgentRegistry";
import type * as AgentLabels from "@/lib/agentLabels";
import type * as UserPreferencesSync from "@/lib/userPreferencesSync";
import {
  SESSION_COLLAB_DEFAULTS,
  readSessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";

vi.mock("@/hooks/useAgentRegistry", async (original) => ({
  ...(await original<typeof AgentRegistry>()),
  useAgentRegistry: () => ({ items: [], isLoading: false, isError: false }),
}));
vi.mock("@/hooks/useSessionCollabPreferences", () => ({
  useSessionCollabPreferences: () => SESSION_COLLAB_DEFAULTS,
}));
vi.mock("@/lib/agentLabels", async (original) => ({
  ...(await original<typeof AgentLabels>()),
  useHarnessCatalog: () => ({}),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: [{ id: "agent-a", display_name: "Agent A", harness: "codex-native" }],
  }),
}));
vi.mock("@/lib/callingDefaultsApi", () => ({
  listCallingDefaultCatalogs: async () => [
    { harness: "codex-native", models: [{ id: "model-x" }] },
  ],
}));
vi.mock("@/lib/userPreferencesSync", async (original) => ({
  ...(await original<typeof UserPreferencesSync>()),
  queueUserPreferencePatch: vi.fn(),
  isUserPreferenceNamespaceDirty: () => false,
}));
afterEach(() => {
  cleanup();
  localStorage.clear();
});

it("edits an agent/model exception in place and persists exact model identity", async () => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <QuotaPreferencesSettings />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByText("Deciding period"));
  fireEvent.click(screen.getByRole("button", { name: "Add agent exception" }));
  fireEvent.change(screen.getByLabelText("Exception 1 agent"), { target: { value: "agent-a" } });
  fireEvent.change(screen.getByLabelText("Exception 1 model"), { target: { value: "model-x" } });
  fireEvent.change(screen.getByLabelText("Exception 1 period"), { target: { value: "5h" } });
  fireEvent.click(screen.getByRole("button", { name: "Save quota display" }));
  expect(readSessionCollabPreferences().quotaPeriodRules).toEqual([
    { agent: "agent-a", model: "model-x", period: "5h" },
  ]);
  fireEvent.change(screen.getByLabelText("Exception 1 model"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Save quota display" }));
  expect(readSessionCollabPreferences().quotaPeriodRules).toEqual([
    { agent: "agent-a", period: "5h" },
  ]);
  fireEvent.click(screen.getByRole("button", { name: "Remove exception 1" }));
  fireEvent.click(screen.getByRole("button", { name: "Save quota display" }));
  expect(readSessionCollabPreferences().quotaPeriodRules).toEqual([]);
});
