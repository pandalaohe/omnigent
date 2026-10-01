import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { Host } from "@/hooks/useHosts";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import {
  readUsageContextPreferences,
  usageContextSourceKey,
  writeUsageContextPreferences,
  type UsageContextOverride,
  type UsageContextPreferences,
} from "@/lib/usageContextPreferences";

import { SavedContextSources } from "./SavedContextSources";

const HOSTS: Host[] = [
  { host_id: "host-a", name: "Alpha", owner: "alice", status: "online" },
  { host_id: "host-b", name: "Beta", owner: "alice", status: "online" },
];

function sourceKey(hostId: string, agentName: string, harness: string, model: string): string {
  return usageContextSourceKey({ hostId, agentName, harness, model });
}

function override(
  contextWindowTokens: number | null,
  autoCompactBufferTokens: number | null,
): UsageContextOverride {
  return { contextWindowTokens, autoCompactBufferTokens };
}

function preferencesWith(overrides: Record<string, UsageContextOverride>): UsageContextPreferences {
  return { version: 5, showProviderUsageLimits: true, overrides, lastProviderUsageLimits: {} };
}

function renderSources(options: {
  preferences: UsageContextPreferences;
  currentSourceKey?: string;
  currentOverride?: UsageContextOverride;
}) {
  return render(
    <SavedContextSources
      preferences={options.preferences}
      currentSourceKey={options.currentSourceKey ?? ""}
      hosts={HOSTS}
      currentOverride={options.currentOverride ?? override(null, null)}
    />,
  );
}

function LiveSources() {
  const preferences = useUsageContextPreferences();
  return (
    <SavedContextSources
      preferences={preferences}
      currentSourceKey=""
      hosts={HOSTS}
      currentOverride={override(null, null)}
    />
  );
}

function chooseFilter(name: "Computer" | "Agent", option: string) {
  fireEvent.click(screen.getByRole("combobox", { name }));
  fireEvent.click(screen.getByRole("option", { name: option }));
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(cleanup);

describe("SavedContextSources", () => {
  const polly = sourceKey("host-a", "polly", "pi", "model-a");
  const claude = sourceKey("host-a", "claude", "claude-sdk", "opus");

  it("renders nothing without saved sources", () => {
    renderSources({ preferences: preferencesWith({}) });

    expect(screen.queryByTestId("saved-context-sources")).not.toBeInTheDocument();
  });

  it("hides each filter until the saved rows hold two distinct values", () => {
    const otherAgent = sourceKey("host-a", "claude", "claude-sdk", "opus");
    const otherHost = sourceKey("host-b", "polly", "pi", "model-a");

    renderSources({ preferences: preferencesWith({ [polly]: override(1, null) }) });
    expect(screen.queryByRole("combobox", { name: "Computer" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Agent" })).not.toBeInTheDocument();

    cleanup();
    renderSources({
      preferences: preferencesWith({ [polly]: override(1, null), [otherAgent]: override(2, null) }),
    });
    expect(screen.queryByRole("combobox", { name: "Computer" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Agent" })).toBeInTheDocument();

    cleanup();
    renderSources({
      preferences: preferencesWith({ [polly]: override(1, null), [otherHost]: override(2, null) }),
    });
    expect(screen.getByRole("combobox", { name: "Computer" })).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Agent" })).not.toBeInTheDocument();
  });

  it("counts the shown rows and resets active filters", () => {
    renderSources({
      preferences: preferencesWith({ [polly]: override(1, null), [claude]: override(2, null) }),
    });

    expect(screen.getByText("Showing 2 of 2")).toBeInTheDocument();

    chooseFilter("Agent", "polly");
    expect(screen.getByText("Showing 1 of 2")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Reset filters" }));
    expect(screen.getByText("Showing 2 of 2")).toBeInTheDocument();
  });

  it("applies a batch to the visible selection, keeping each row's context", () => {
    renderSources({
      preferences: preferencesWith({
        [polly]: override(100_000, 10_000),
        [claude]: override(200_000, 20_000),
      }),
    });

    chooseFilter("Agent", "polly");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select all shown" }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Compact buffer"), { target: { value: "33000" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply to 1 selected" }));

    const saved = readUsageContextPreferences().overrides;
    expect(saved[polly]).toEqual({
      contextWindowTokens: 100_000,
      autoCompactBufferTokens: 33_000,
    });
    expect(saved[claude]).toEqual({
      contextWindowTokens: 200_000,
      autoCompactBufferTokens: 20_000,
    });
    expect(screen.queryByText("1 selected")).not.toBeInTheDocument();
  });

  it("reflects partial and full selection in the header checkbox", () => {
    renderSources({
      preferences: preferencesWith({ [polly]: override(1, null), [claude]: override(2, null) }),
    });

    const header = screen.getByRole("checkbox", { name: "Select all shown" });
    expect(header).toHaveAttribute("aria-checked", "false");

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-a" }));
    expect(header).toHaveAttribute("aria-checked", "mixed");

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha claude opus" }));
    expect(header).toHaveAttribute("aria-checked", "true");

    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-checked", "false");
  });

  it("drops hidden rows from the selection when the filter changes", () => {
    renderSources({
      preferences: preferencesWith({ [polly]: override(1, null), [claude]: override(2, null) }),
    });

    fireEvent.click(screen.getByRole("checkbox", { name: "Select all shown" }));
    expect(screen.getByText("2 selected")).toBeInTheDocument();

    chooseFilter("Agent", "polly");
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("fills the batch fields from the current source without applying them", () => {
    const { rerender } = renderSources({
      preferences: preferencesWith({ [polly]: override(100_000, 10_000) }),
      currentSourceKey: polly,
      currentOverride: override(330_000, null),
    });

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-a" }));
    fireEvent.click(screen.getByRole("button", { name: "Use current source" }));

    expect(screen.getByLabelText("Context total")).toHaveValue(330000);
    expect(screen.getByRole("button", { name: "Auto compact buffer" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const bufferInput = screen.getByLabelText("Compact buffer");
    expect(bufferInput).toBeDisabled();
    expect(bufferInput).toHaveAttribute("placeholder", "Auto");
    expect(readUsageContextPreferences().overrides).toEqual({});

    rerender(
      <SavedContextSources
        preferences={preferencesWith({ [polly]: override(100_000, 10_000) })}
        currentSourceKey={polly}
        hosts={HOSTS}
        currentOverride={override(null, null)}
      />,
    );
    expect(screen.getByRole("button", { name: "Use current source" })).toBeDisabled();
  });

  it("clears a field to Auto for every selected row", () => {
    renderSources({
      preferences: preferencesWith({
        [polly]: override(100_000, 10_000),
        [claude]: override(200_000, 20_000),
      }),
    });

    fireEvent.click(screen.getByRole("checkbox", { name: "Select all shown" }));
    fireEvent.click(screen.getByRole("button", { name: "Auto compact buffer" }));
    expect(screen.getByLabelText("Compact buffer")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Apply to 2 selected" }));

    const saved = readUsageContextPreferences().overrides;
    expect(saved[polly]).toEqual({ contextWindowTokens: 100_000, autoCompactBufferTokens: null });
    expect(saved[claude]).toEqual({ contextWindowTokens: 200_000, autoCompactBufferTokens: null });
  });

  it("edits one row at a time and leaves without writing on cancel or Escape", () => {
    renderSources({
      preferences: preferencesWith({
        [polly]: override(100_000, 10_000),
        [claude]: override(200_000, 20_000),
      }),
    });

    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha polly model-a" }));
    expect(screen.getByLabelText("Edit context total")).toHaveValue(100000);
    expect(screen.getByLabelText("Edit compact buffer")).toHaveValue(10000);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Edit context total")).not.toBeInTheDocument();
    expect(readUsageContextPreferences().overrides).toEqual({});

    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha claude opus" }));
    expect(screen.getAllByLabelText("Edit context total")).toHaveLength(1);
    expect(screen.getByLabelText("Edit context total")).toHaveValue(200000);

    fireEvent.change(screen.getByLabelText("Edit context total"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Edit compact buffer"), { target: { value: "" } });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.getByText("Use Delete to remove this source.")).toBeInTheDocument();

    fireEvent.keyDown(screen.getByLabelText("Edit context total"), { key: "Escape" });
    expect(screen.queryByLabelText("Edit context total")).not.toBeInTheDocument();
    expect(readUsageContextPreferences().overrides).toEqual({});

    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha polly model-a" }));
    fireEvent.change(screen.getByLabelText("Edit context total"), { target: { value: "150000" } });
    fireEvent.change(screen.getByLabelText("Edit compact buffer"), { target: { value: "25000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    const saved = readUsageContextPreferences().overrides;
    expect(saved[polly]).toEqual({ contextWindowTokens: 150_000, autoCompactBufferTokens: 25_000 });
    expect(saved[claude]).toEqual({
      contextWindowTokens: 200_000,
      autoCompactBufferTokens: 20_000,
    });
  });

  it("closes the inline editor when its saved row changes or is deleted", () => {
    writeUsageContextPreferences(
      preferencesWith({
        [polly]: override(100_000, 10_000),
        [claude]: override(200_000, 20_000),
      }),
    );
    render(<LiveSources />);

    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha polly model-a" }));
    expect(screen.getByLabelText("Edit context total")).toHaveValue(100000);

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-a" }));
    fireEvent.change(screen.getByLabelText("Compact buffer"), { target: { value: "33000" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply to 1 selected" }));

    expect(screen.queryByLabelText("Edit context total")).not.toBeInTheDocument();
    expect(readUsageContextPreferences().overrides[polly]).toEqual({
      contextWindowTokens: 100_000,
      autoCompactBufferTokens: 33_000,
    });

    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha polly model-a" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Alpha polly model-a" }));
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "Delete saved source?" })).getByRole("button", {
        name: "Delete",
      }),
    );
    expect(screen.queryByLabelText("Edit context total")).not.toBeInTheDocument();

    act(() => {
      writeUsageContextPreferences(
        preferencesWith({
          [polly]: override(100_000, 33_000),
          [claude]: override(200_000, 20_000),
        }),
      );
    });
    expect(screen.getByRole("button", { name: "Edit Alpha polly model-a" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Edit context total")).not.toBeInTheDocument();
  });

  it("deletes a single saved source after confirmation", () => {
    renderSources({
      preferences: preferencesWith({
        [polly]: override(100_000, 10_000),
        [claude]: override(2, null),
      }),
      currentSourceKey: polly,
    });

    fireEvent.click(screen.getByRole("button", { name: "Delete Alpha polly model-a" }));
    const dialog = screen.getByRole("dialog", { name: "Delete saved source?" });
    expect(within(dialog).getByText("Alpha · Pi · model-a (Current)")).toBeInTheDocument();
    expect(within(dialog).getByText(/go back to Auto/)).toBeInTheDocument();
    expect(readUsageContextPreferences().overrides).toEqual({});

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    const saved = readUsageContextPreferences().overrides;
    expect(saved[polly]).toBeUndefined();
    expect(saved[claude]).toEqual({ contextWindowTokens: 2, autoCompactBufferTokens: null });
  });

  it("deletes only the selected sources after batch confirmation", () => {
    const untouched = sourceKey("host-b", "claude", "claude-sdk", "sonnet");
    renderSources({
      preferences: preferencesWith({
        [polly]: override(100_000, 10_000),
        [claude]: override(200_000, 20_000),
        [untouched]: override(300_000, 30_000),
      }),
    });

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-a" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha claude opus" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    const dialog = screen.getByRole("dialog", { name: "Delete 2 saved sources?" });
    expect(within(dialog).getByText("Alpha · Pi · model-a")).toBeInTheDocument();
    expect(within(dialog).getByText("Alpha · Claude SDK · opus")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete 2" }));

    const saved = readUsageContextPreferences().overrides;
    expect(saved[polly]).toBeUndefined();
    expect(saved[claude]).toBeUndefined();
    expect(saved[untouched]).toEqual({
      contextWindowTokens: 300_000,
      autoCompactBufferTokens: 30_000,
    });
    expect(screen.queryByText("2 selected")).not.toBeInTheDocument();
  });
});
