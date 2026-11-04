import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { Host } from "@/hooks/useHosts";
import {
  readUsageContextPreferences,
  usageContextLevelKey,
  usageContextSourceKey,
  writeUsageContextPreferences,
  DEFAULT_USAGE_CONTEXT_PREFERENCES,
  type UsageContextOverride,
  type UsageContextPreferences,
} from "@/lib/usageContextPreferences";

import { SavedContextSources } from "./SavedContextSources";

const HOSTS: Host[] = [
  { host_id: "host-a", name: "Alpha", owner: "alice", status: "online" },
  { host_id: "host-b", name: "Beta", owner: "alice", status: "online" },
];

const SOURCE = { hostId: "host-a", agentName: "polly", harness: "pi", model: "" };
const CURRENT = usageContextSourceKey({ ...SOURCE, model: "model-a" });

function override(
  contextWindowTokens: number | null,
  autoCompactBufferTokens: number | null,
): UsageContextOverride {
  return { contextWindowTokens, autoCompactBufferTokens };
}

function preferencesWith(overrides: Record<string, UsageContextOverride>): UsageContextPreferences {
  return {
    version: 6,
    showProviderUsageLimits: true,
    overrides,
    lastProviderUsageLimits: {},
    board: DEFAULT_USAGE_CONTEXT_PREFERENCES.board,
  };
}

function renderSources(preferences: UsageContextPreferences) {
  writeUsageContextPreferences(preferences);
  const result = render(
    <SavedContextSources
      preferences={readUsageContextPreferences()}
      currentSourceKey={CURRENT}
      hosts={HOSTS}
      initialSource={SOURCE}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "List all saved values" }));
  return result;
}

beforeEach(() => localStorage.clear());
afterEach(cleanup);

describe("SavedContextSources", () => {
  const modelA = usageContextSourceKey({ ...SOURCE, model: "model-a" });
  const modelB = usageContextSourceKey({ ...SOURCE, model: "model-b" });

  it("lists every saved value, the corner and computer-wide levels included", () => {
    renderSources(
      preferencesWith({
        [modelA]: override(100_000, null),
        [usageContextLevelKey(SOURCE, "cell")]: override(200_000, 10_000),
        [usageContextLevelKey(SOURCE, "host")]: override(300_000, null),
        [usageContextLevelKey({ ...SOURCE, agentName: "", harness: "" }, "all")]: override(
          400_000,
          null,
        ),
      }),
    );

    const dialog = screen.getByRole("dialog", { name: "All saved values" });
    expect(within(dialog).getByText("Every computer")).toBeInTheDocument();
    expect(within(dialog).getByText("All agents, all hosts")).toBeInTheDocument();
    expect(within(dialog).getByText("All agents on Alpha")).toBeInTheDocument();
    expect(
      within(dialog).getByRole("checkbox", { name: "Select Alpha polly model-a" }),
    ).toBeInTheDocument();
  });

  it("deletes one saved value after confirmation", async () => {
    renderSources(preferencesWith({ [modelA]: override(100_000, null) }));

    fireEvent.click(screen.getByRole("button", { name: "Delete Alpha polly model-a" }));
    const confirm = screen.getByRole("dialog", { name: "Delete saved value?" });
    fireEvent.click(within(confirm).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(readUsageContextPreferences().overrides[modelA]).toBeUndefined());
  });

  it("applies a batch to the selection and keeps each row's value it does not set", async () => {
    renderSources(
      preferencesWith({
        [modelA]: override(100_000, 10_000),
        [modelB]: override(200_000, 20_000),
      }),
    );

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-a" }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Compact buffer"), { target: { value: "50000" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply to 1 selected" }));

    await waitFor(() =>
      expect(readUsageContextPreferences().overrides[modelA]).toMatchObject({
        contextWindowTokens: 100_000,
        autoCompactBufferTokens: 50_000,
      }),
    );
    expect(readUsageContextPreferences().overrides[modelB]).toMatchObject({
      contextWindowTokens: 200_000,
      autoCompactBufferTokens: 20_000,
    });
  });

  it("refuses a batch buffer that would meet a selected context total", () => {
    renderSources(preferencesWith({ [modelA]: override(100_000, 10_000) }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-a" }));
    fireEvent.change(screen.getByLabelText("Context total"), { target: { value: "50000" } });
    fireEvent.change(screen.getByLabelText("Compact buffer"), { target: { value: "60000" } });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Token buffer must be less than every selected context total",
    );
    expect(screen.getByRole("button", { name: "Apply to 1 selected" })).toBeDisabled();
  });

  it("fills the batch fields from this session without applying them", () => {
    renderSources(
      preferencesWith({
        [modelA]: override(100_000, 10_000),
        [modelB]: override(200_000, 20_000),
      }),
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-b" }));
    fireEvent.click(screen.getByRole("button", { name: "Use this session's values" }));
    expect(screen.getByLabelText("Context total")).toHaveValue(100_000);
    expect(screen.getByLabelText("Compact buffer")).toHaveValue(10_000);
    expect(readUsageContextPreferences().overrides[modelB]).toMatchObject({
      contextWindowTokens: 200_000,
    });
  });

  it("collapses identical model values into their cell only when Merge is pressed", () => {
    renderSources(
      preferencesWith({
        [modelA]: override(100_000, null),
        [modelB]: override(100_000, null),
      }),
    );
    expect(
      screen.getByText("2 model values with identical values can be merged into 1 cell values."),
    ).toBeInTheDocument();
    expect(readUsageContextPreferences().overrides[modelA]).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: "Merge" }));
    const saved = readUsageContextPreferences().overrides;
    expect(saved[modelA]).toBeUndefined();
    expect(saved[modelB]).toBeUndefined();
    expect(saved[usageContextLevelKey(SOURCE, "cell")]).toMatchObject({
      contextWindowTokens: 100_000,
    });
  });

  it("keeps an agent's every-computer value out of this computer's list", () => {
    renderSources(
      preferencesWith({ [usageContextLevelKey(SOURCE, "column")]: override(100_000, null) }),
    );
    expect(screen.getByText("Nothing saved here.")).toBeInTheDocument();
    expect(
      screen.getByText("100,000 context · Auto buffer for every computer"),
    ).toBeInTheDocument();
  });
});
