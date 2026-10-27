import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  readUsageContextPreferences,
  usageContextSourceKey,
  usageContextLevelKey,
  DEFAULT_USAGE_CONTEXT_PREFERENCES,
  writeUsageContextPreferences,
  USAGE_CONTEXT_STORAGE_KEY,
} from "@/lib/usageContextPreferences";
import type { ProviderUsageLimitsSnapshot } from "@/lib/providerUsageLimits";
import type { RegistryItem } from "@/hooks/useAgentRegistry";

const mocks = vi.hoisted(() => ({
  conversationId: "session-1" as string | null,
  agentName: "polly" as string | null,
  harness: "pi" as string | null,
  model: "model-a" as string | null,
  contextWindow: 330_000 as number | null,
  autoCompactTokenLimit: null as number | null,
  providerUsageLimits: null as ProviderUsageLimitsSnapshot | null,
  registryItems: [] as RegistryItem[],
}));

vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({
      conversationId: mocks.conversationId,
      boundAgentName: mocks.agentName,
      sessionHarness: mocks.harness,
      llmModel: mocks.model,
      contextWindow: mocks.contextWindow,
      autoCompactTokenLimit: mocks.autoCompactTokenLimit,
      providerUsageLimits: mocks.providerUsageLimits,
    }),
}));

vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: { hostId: "host-friendly" }, isLoading: false, error: null }),
}));

vi.mock("@/hooks/useAgentRegistry", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useAgentRegistry: () => ({ items: mocks.registryItems }),
}));

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({
    data: [
      {
        host_id: "host-friendly",
        name: "Office MacBook",
        owner: "alice",
        status: "online",
      },
    ],
  }),
  useCodexRateLimits: () => ({ data: null, isLoading: false }),
}));

import { ContextUsageSettings } from "./ContextUsageSettings";

describe("ContextUsageSettings", () => {
  beforeEach(() => {
    localStorage.clear();
    mocks.conversationId = "session-1";
    mocks.agentName = "polly";
    mocks.harness = "pi";
    mocks.model = "model-a";
    mocks.contextWindow = 330_000;
    mocks.autoCompactTokenLimit = null;
    mocks.providerUsageLimits = null;
    mocks.registryItems = [];
  });

  it("uses registry labels for the active source", () => {
    mocks.registryItems = [
      {
        key: "agent-polly",
        name: "polly",
        label: "Polly · library",
        section: "yours",
        harness: "pi",
        description: null,
        deployed: true,
        source: "library",
      },
      {
        key: "harness:pi",
        label: "Pi SDK",
        section: "sdk",
        harness: "pi",
        description: null,
        deployed: true,
        source: "builtin",
      },
    ];
    render(<ContextUsageSettings />);
    expect(screen.getByText("Polly · library")).toBeInTheDocument();
    expect(screen.getByText("Pi SDK")).toBeInTheDocument();
  });

  it("shows friendly source names and keeps overrides scoped to the exact source", async () => {
    const { rerender } = render(<ContextUsageSettings />);

    expect(screen.getAllByText("Office MacBook")).toHaveLength(2);
    expect(screen.getByText("Pi")).toBeInTheDocument();
    expect(screen.queryByText("host-friendly")).not.toBeInTheDocument();
    expect(screen.getByTestId("provider-usage-source-status")).toHaveTextContent("No data");
    expect(screen.getByTestId("provider-usage-source-status")).toHaveAttribute(
      "title",
      expect.stringContaining("No usage limits received from Pi"),
    );

    const contextInput = screen.getByLabelText("Context window override in tokens");
    const bufferInput = screen.getByLabelText("Automatic Compact buffer in tokens");
    expect(contextInput).toHaveAttribute("placeholder", "330000");

    fireEvent.change(contextInput, { target: { value: "330000" } });
    fireEvent.blur(contextInput);
    fireEvent.change(bufferInput, { target: { value: "33000" } });
    fireEvent.blur(bufferInput);

    const firstKey = usageContextSourceKey({
      hostId: "host-friendly",
      agentName: "polly",
      harness: "pi",
      model: "model-a",
    });
    await waitFor(() =>
      expect(readUsageContextPreferences().overrides[firstKey]).toEqual({
        contextWindowTokens: 330_000,
        autoCompactBufferTokens: 33_000,
      }),
    );
    expect(screen.getByText(/Compact 297k/)).toBeInTheDocument();

    mocks.model = "model-b";
    rerender(<ContextUsageSettings />);
    await waitFor(() => expect(contextInput).toHaveValue(null));
    expect(bufferInput).toHaveValue(null);

    fireEvent.change(bufferInput, { target: { value: "40000" } });
    fireEvent.blur(bufferInput);
    const secondKey = usageContextSourceKey({
      hostId: "host-friendly",
      agentName: "polly",
      harness: "pi",
      model: "model-b",
    });
    await waitFor(() => {
      const saved = readUsageContextPreferences().overrides;
      expect(saved[firstKey]).toEqual({
        contextWindowTokens: 330_000,
        autoCompactBufferTokens: 33_000,
      });
      expect(saved[secondKey]).toEqual({
        contextWindowTokens: null,
        autoCompactBufferTokens: 40_000,
      });
    });
    expect(screen.getByTestId("saved-context-sources")).toHaveTextContent("Saved sources");
    expect(screen.getByTestId("saved-context-sources")).toHaveTextContent("2 model overrides");
    expect(screen.getByTestId("saved-context-sources")).not.toHaveTextContent("model-a");
    expect(screen.getByText(/2 sources saved/)).toBeInTheDocument();
    expect(screen.getByText(/Manual values change only this indicator/)).toBeInTheDocument();
  });

  it("clears the current source inputs when its saved row is deleted", async () => {
    render(<ContextUsageSettings />);

    const contextInput = screen.getByLabelText("Context window override in tokens");
    const bufferInput = screen.getByLabelText("Automatic Compact buffer in tokens");
    fireEvent.change(contextInput, { target: { value: "330000" } });
    fireEvent.blur(contextInput);
    fireEvent.change(bufferInput, { target: { value: "33000" } });
    fireEvent.blur(bufferInput);

    await waitFor(() =>
      expect(screen.getByTestId("saved-context-sources")).toHaveTextContent("1 model override"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Manage saved sources" }));

    fireEvent.click(screen.getByRole("button", { name: "Delete Office MacBook polly model-a" }));
    const dialog = screen.getByRole("dialog", { name: "Delete saved source?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "Manage saved sources" })).getByRole("button", {
        name: "Close",
      }),
    );

    await waitFor(() => expect(contextInput).toHaveValue(null));
    expect(bufferInput).toHaveValue(null);
    expect(screen.getByTestId("saved-context-sources")).toHaveTextContent("0 model overrides");
  });

  it("shows the reported Compact buffer as the input placeholder", () => {
    mocks.contextWindow = 330_000;
    mocks.autoCompactTokenLimit = 297_000;

    render(<ContextUsageSettings />);

    expect(screen.getByLabelText("Automatic Compact buffer in tokens")).toHaveAttribute(
      "placeholder",
      "33000",
    );
  });

  it("keeps both manual buffer units, accepts zero, and rejects a token buffer at capacity", async () => {
    mocks.contextWindow = 100_000;
    render(<ContextUsageSettings />);
    const key = usageContextSourceKey({
      hostId: "host-friendly",
      agentName: "polly",
      harness: "pi",
      model: "model-a",
    });
    const context = screen.getByLabelText("Context window override in tokens");
    fireEvent.change(context, { target: { value: "100000" } });
    fireEvent.blur(context);
    const tokens = screen.getByLabelText("Automatic Compact buffer in tokens");
    fireEvent.change(tokens, { target: { value: "20000" } });
    fireEvent.blur(tokens);

    fireEvent.click(
      within(screen.getByRole("group", { name: "Buffer unit" })).getByRole("button", {
        name: "Percent",
      }),
    );
    const percent = screen.getByLabelText("Compact buffer percent");
    fireEvent.change(percent, { target: { value: "10" } });
    fireEvent.blur(percent);
    await waitFor(() =>
      expect(readUsageContextPreferences().overrides[key]).toMatchObject({
        autoCompactBufferTokens: 20_000,
        autoCompactBufferPercent: 10,
        compactBufferMode: "percent",
      }),
    );
    expect(screen.getByText(/Compact 90k/)).toBeInTheDocument();

    fireEvent.change(percent, { target: { value: "0" } });
    fireEvent.blur(percent);
    await waitFor(() =>
      expect(readUsageContextPreferences().overrides[key]?.autoCompactBufferPercent).toBe(0),
    );
    fireEvent.click(
      within(screen.getByRole("group", { name: "Buffer unit" })).getByRole("button", {
        name: "Tokens",
      }),
    );
    const tokensAgain = screen.getByLabelText("Automatic Compact buffer in tokens");
    expect(tokensAgain).toHaveValue(20000);
    fireEvent.change(tokensAgain, { target: { value: "100000" } });
    fireEvent.blur(tokensAgain);
    expect(screen.getByRole("alert")).toHaveTextContent("less than the context total");
    expect(readUsageContextPreferences().overrides[key]?.autoCompactBufferTokens).toBe(20_000);

    fireEvent.change(tokensAgain, { target: { value: "50000" } });
    fireEvent.blur(tokensAgain);
    await waitFor(() =>
      expect(readUsageContextPreferences().overrides[key]?.autoCompactBufferTokens).toBe(50_000),
    );
    fireEvent.change(context, { target: { value: "50000" } });
    fireEvent.blur(context);
    expect(screen.getByRole("alert")).toHaveTextContent("must exceed");
    expect(readUsageContextPreferences().overrides[key]?.contextWindowTokens).toBe(100_000);

    fireEvent.change(context, { target: { value: "100000" } });
    fireEvent.blur(context);
    fireEvent.change(tokensAgain, { target: { value: "0" } });
    fireEvent.blur(tokensAgain);
    await waitFor(() =>
      expect(readUsageContextPreferences().overrides[key]).toMatchObject({
        autoCompactBufferTokens: 0,
        autoCompactBufferPercent: 0,
        compactBufferMode: "tokens",
      }),
    );
  });

  it("validates a buffer edit against the saved total after rejecting an invalid total draft", () => {
    const key = usageContextSourceKey({
      hostId: "host-friendly",
      agentName: "polly",
      harness: "pi",
      model: "model-a",
    });
    localStorage.setItem(
      USAGE_CONTEXT_STORAGE_KEY,
      JSON.stringify({
        version: 5,
        showProviderUsageLimits: true,
        overrides: { [key]: { contextWindowTokens: 100_000, autoCompactBufferTokens: 20_000 } },
        lastProviderUsageLimits: {},
      }),
    );
    mocks.contextWindow = 330_000;
    render(<ContextUsageSettings />);

    const context = screen.getByLabelText("Context window override in tokens");
    fireEvent.change(context, { target: { value: "0" } });
    fireEvent.blur(context);
    expect(readUsageContextPreferences().overrides[key]?.contextWindowTokens).toBe(100_000);

    const buffer = screen.getByLabelText("Automatic Compact buffer in tokens");
    fireEvent.change(buffer, { target: { value: "150000" } });
    fireEvent.blur(buffer);
    expect(screen.getByRole("alert")).toHaveTextContent("less than the context total");
    expect(readUsageContextPreferences().overrides[key]?.autoCompactBufferTokens).toBe(20_000);
  });

  it("does not present an expired cached usage snapshot as current", () => {
    mocks.agentName = "claude-native";
    mocks.harness = "claude-native";
    mocks.providerUsageLimits = {
      provider: "Claude",
      capturedAt: Math.floor(Date.now() / 1000) - 3601,
      windows: [{ label: "5h", ariaLabel: "5 hour", usedPercent: 12 }],
    };

    render(<ContextUsageSettings />);

    expect(screen.getByTestId("provider-usage-source-status")).toHaveTextContent("No data");
    expect(screen.getByTestId("provider-usage-source-status")).toHaveAttribute(
      "title",
      expect.stringContaining("No usage limits received"),
    );
  });

  it("edits only the selected level and logical field while showing inherited provenance", () => {
    const source = { hostId: "host-friendly", agentName: "polly", harness: "pi", model: "model-a" };
    const key = usageContextSourceKey(source);
    const cell = usageContextLevelKey(source, "cell");
    const column = usageContextLevelKey(source, "column");
    writeUsageContextPreferences({
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [cell]: { contextWindowTokens: 400_000, autoCompactBufferTokens: 20_000 },
        [column]: { contextWindowTokens: 500_000, autoCompactBufferTokens: 30_000 },
      },
    });
    render(<ContextUsageSettings />);
    const context = screen.getByLabelText("Context window override in tokens");
    expect(context).toHaveValue(null);
    expect(screen.getByLabelText("Automatic Compact buffer in tokens")).toHaveValue(null);
    expect(screen.getByTestId("current-source-resolution")).toHaveTextContent(
      "Context 400,000 · Office MacBook × polly",
    );
    fireEvent.change(context, { target: { value: "300000" } });
    fireEvent.blur(context);
    expect(readUsageContextPreferences().overrides[key]).toEqual({
      contextWindowTokens: 300_000,
      autoCompactBufferTokens: null,
    });
    expect(screen.getByTestId("current-source-resolution")).toHaveTextContent(
      "Context 300,000 · model override",
    );
    expect(screen.getByTestId("current-source-resolution")).toHaveTextContent(
      "Buffer 20,000 · Office MacBook × polly",
    );
    fireEvent.click(screen.getByRole("combobox", { name: "Edit source level" }));
    fireEvent.click(screen.getByRole("option", { name: "polly · all hosts" }));
    expect(context).toHaveValue(500000);
    fireEvent.change(context, { target: { value: "600000" } });
    fireEvent.blur(context);
    expect(readUsageContextPreferences().overrides[column]).toEqual({
      contextWindowTokens: 600_000,
      autoCompactBufferTokens: 30_000,
    });
    expect(readUsageContextPreferences().overrides[cell]?.contextWindowTokens).toBe(400_000);
    expect(readUsageContextPreferences().overrides[key]?.contextWindowTokens).toBe(300_000);
  });

  it("shows ignored inherited buffer levels and the reported fallback", () => {
    const source = { hostId: "host-friendly", agentName: "polly", harness: "pi", model: "model-a" };
    writeUsageContextPreferences({
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [usageContextLevelKey(source, "column")]: {
          contextWindowTokens: null,
          autoCompactBufferTokens: 150_000,
        },
      },
    });
    mocks.autoCompactTokenLimit = 300_000;
    render(<ContextUsageSettings />);
    const context = screen.getByLabelText("Context window override in tokens");
    fireEvent.change(context, { target: { value: "100000" } });
    fireEvent.blur(context);
    expect(screen.getByTestId("current-source-resolution")).toHaveTextContent(
      "Buffer 30,000 · reported",
    );
    expect(screen.getByTestId("current-source-resolution")).toHaveTextContent(
      "Ignored polly · all hosts: buffer ≥ context; using reported.",
    );
  });

  it.each([true, false])(
    "copies the exact model through Use current source while editing a column (column saved: %s)",
    (hasColumn) => {
      const source = {
        hostId: "host-friendly",
        agentName: "polly",
        harness: "pi",
        model: "model-a",
      };
      const exact = usageContextSourceKey(source);
      const target = usageContextSourceKey({ ...source, model: "model-b" });
      const column = usageContextLevelKey(source, "column");
      const exactOverride = { contextWindowTokens: 300_000, autoCompactBufferTokens: 10_000 };
      const columnOverride = { contextWindowTokens: 500_000, autoCompactBufferTokens: 20_000 };
      writeUsageContextPreferences({
        ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
        overrides: {
          [exact]: exactOverride,
          [target]: { contextWindowTokens: 200_000, autoCompactBufferTokens: 5_000 },
          ...(hasColumn ? { [column]: columnOverride } : {}),
        },
      });
      render(<ContextUsageSettings />);
      fireEvent.click(screen.getByRole("combobox", { name: "Edit source level" }));
      fireEvent.click(screen.getByRole("option", { name: "polly · all hosts" }));
      expect(screen.getByLabelText("Context window override in tokens")).toHaveValue(
        hasColumn ? 500_000 : null,
      );
      fireEvent.click(screen.getByRole("button", { name: "Manage saved sources" }));
      fireEvent.click(
        screen.getByRole("checkbox", { name: "Select Office MacBook polly model-b" }),
      );
      const useCurrent = screen.getByRole("button", { name: "Use current source" });
      expect(useCurrent).toBeEnabled();
      fireEvent.click(useCurrent);
      expect(screen.getByLabelText("Context total")).toHaveValue(300_000);
      expect(screen.getByLabelText("Compact buffer")).toHaveValue(10_000);
      fireEvent.click(screen.getByRole("button", { name: "Apply to 1 selected" }));
      const saved = readUsageContextPreferences().overrides;
      expect(saved[target]).toEqual(exactOverride);
      expect(saved[exact]).toEqual(exactOverride);
      expect(saved[column]).toEqual(hasColumn ? columnOverride : undefined);
    },
  );
});
