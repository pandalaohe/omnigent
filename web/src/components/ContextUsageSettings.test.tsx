import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { readUsageContextPreferences, usageContextSourceKey } from "@/lib/usageContextPreferences";

const mocks = vi.hoisted(() => ({
  conversationId: "session-1" as string | null,
  agentName: "polly" as string | null,
  harness: "pi" as string | null,
  model: "model-a" as string | null,
  contextWindow: 330_000 as number | null,
  autoCompactTokenLimit: null as number | null,
  providerUsageLimits: null,
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
  });

  it("shows friendly source names and keeps overrides scoped to the exact source", async () => {
    const { rerender } = render(<ContextUsageSettings />);

    expect(screen.getByText("Office MacBook")).toBeInTheDocument();
    expect(screen.getByText("Pi")).toBeInTheDocument();
    expect(screen.queryByText("host-friendly")).not.toBeInTheDocument();
    expect(screen.getByTestId("provider-usage-source-status")).toHaveTextContent("Not reported");
    expect(screen.getByTestId("provider-usage-source-status")).toHaveAttribute(
      "title",
      expect.stringContaining("Pi has not reported comparable plan windows"),
    );

    const contextInput = screen.getByLabelText("Context window override in tokens");
    const thresholdInput = screen.getByLabelText("Automatic Compact threshold percent");
    expect(contextInput).toHaveAttribute("placeholder", "330000");

    fireEvent.change(contextInput, { target: { value: "330000" } });
    fireEvent.blur(contextInput);
    fireEvent.change(thresholdInput, { target: { value: "93" } });
    fireEvent.blur(thresholdInput);

    const firstKey = usageContextSourceKey({
      hostId: "host-friendly",
      agentName: "polly",
      harness: "pi",
      model: "model-a",
    });
    await waitFor(() =>
      expect(readUsageContextPreferences().overrides[firstKey]).toEqual({
        contextWindowTokens: 330_000,
        autoCompactThresholdPercent: 93,
      }),
    );
    expect(screen.getByText(/Compact 306.9k/)).toBeInTheDocument();

    mocks.model = "model-b";
    rerender(<ContextUsageSettings />);
    await waitFor(() => expect(contextInput).toHaveValue(null));
    expect(thresholdInput).toHaveValue(null);

    fireEvent.change(thresholdInput, { target: { value: "80" } });
    fireEvent.blur(thresholdInput);
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
        autoCompactThresholdPercent: 93,
      });
      expect(saved[secondKey]).toEqual({
        contextWindowTokens: null,
        autoCompactThresholdPercent: 80,
      });
    });
  });
});
