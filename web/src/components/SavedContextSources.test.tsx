import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { Host } from "@/hooks/useHosts";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import {
  readUsageContextPreferences,
  usageContextSourceKey,
  usageContextSourceFromKey,
  usageContextLevelKey,
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
  reportedContextWindow?: number | null;
}) {
  const result = render(
    <SavedContextSources
      preferences={options.preferences}
      currentSourceKey={options.currentSourceKey ?? sourceKey("host-a", "polly", "pi", "model-a")}
      hosts={HOSTS}
      currentOverride={options.currentOverride ?? override(null, null)}
      reportedContextWindow={options.reportedContextWindow}
    />,
  );
  openManager();
  return result;
}

function openManager() {
  fireEvent.click(screen.getByRole("button", { name: "Manage Alpha × polly · Pi" }));
}

function LiveSources() {
  const preferences = useUsageContextPreferences();
  return (
    <SavedContextSources
      preferences={preferences}
      currentSourceKey={sourceKey("host-a", "polly", "pi", "model-a")}
      hosts={HOSTS}
      currentOverride={override(null, null)}
    />
  );
}

function renderLiveSources() {
  const result = render(<LiveSources />);
  openManager();
  return result;
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(cleanup);

describe("SavedContextSources", () => {
  const polly = sourceKey("host-a", "polly", "pi", "model-a");
  const claude = sourceKey("host-a", "polly", "pi", "model-b");

  it("keeps the count grid bounded and model names in the manager", () => {
    const longModel = "model-with-a-long-identical-prefix-but-a-distinct-suffix";
    renderSources({
      preferences: preferencesWith({
        [polly]: override(100_000, null),
        [sourceKey("host-a", "polly", "pi", longModel)]: override(200_000, null),
      }),
    });
    expect(screen.getByText(longModel)).toHaveClass("break-all");
    expect(screen.queryByRole("combobox", { name: "Computer" })).not.toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Saved source computers" })).toHaveTextContent(
      "Alpha",
    );
    expect(screen.getByText("All models")).toBeInTheDocument();
  });

  it("applies a batch to the visible selection, keeping each row's context", () => {
    renderSources({
      preferences: preferencesWith({
        [polly]: override(100_000, 10_000),
        [claude]: override(200_000, 20_000),
      }),
    });

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-a" }));
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

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-b" }));
    expect(header).toHaveAttribute("aria-checked", "true");

    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-checked", "false");
  });

  it("clears selection when another host is chosen in the tree", () => {
    renderSources({
      preferences: preferencesWith({
        [polly]: override(100_000, null),
        [claude]: override(200_000, null),
      }),
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Select all shown" }));
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    const navigation = screen.getByRole("navigation");
    const beta = within(navigation).getByText("Beta").parentElement!;
    fireEvent.click(within(beta).getByRole("button", { name: "polly · Pi" }));
    expect(screen.queryByText("2 selected")).not.toBeInTheDocument();
    expect(screen.queryByText("model-a")).not.toBeInTheDocument();
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

    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha polly model-b" }));
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

  it("retains a saved token buffer when editing its percentage and rejects a smaller total", () => {
    renderSources({ preferences: preferencesWith({ [polly]: override(100_000, 20_000) }) });
    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha polly model-a" }));
    const context = screen.getByLabelText("Edit context total");
    fireEvent.change(context, { target: { value: "20000" } });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("less than the context total");
    fireEvent.change(context, { target: { value: "100000" } });
    fireEvent.click(
      within(screen.getByRole("group", { name: "Edit buffer unit" })).getByRole("button", {
        name: "Percent",
      }),
    );
    const percent = screen.getByLabelText("Edit compact buffer percent");
    fireEvent.change(percent, { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(readUsageContextPreferences().overrides[polly]).toEqual({
      contextWindowTokens: 100_000,
      autoCompactBufferTokens: 20_000,
      autoCompactBufferPercent: 10,
      compactBufferMode: "percent",
    });
  });

  it("rejects a saved row buffer at the known current source's reported total", () => {
    renderSources({
      preferences: preferencesWith({ [polly]: override(null, 20_000) }),
      currentSourceKey: polly,
      reportedContextWindow: 100_000,
    });
    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha polly model-a" }));
    fireEvent.change(screen.getByLabelText("Edit compact buffer"), { target: { value: "100000" } });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(readUsageContextPreferences().overrides).toEqual({});
  });

  it("rejects a batch buffer at the known current source's reported total", () => {
    renderSources({
      preferences: preferencesWith({ [polly]: override(null, 20_000) }),
      currentSourceKey: polly,
      reportedContextWindow: 100_000,
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-a" }));
    fireEvent.change(screen.getByLabelText("Compact buffer"), { target: { value: "100000" } });
    expect(screen.getByRole("button", { name: "Apply to 1 selected" })).toBeDisabled();
    expect(readUsageContextPreferences().overrides).toEqual({});
  });

  it("applies zero percent to selected sources without copying their token buffers", () => {
    renderSources({
      preferences: preferencesWith({
        [polly]: override(100_000, 20_000),
        [claude]: override(200_000, 30_000),
      }),
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Select all shown" }));
    fireEvent.click(
      within(screen.getByRole("group", { name: "Selected buffer unit" })).getByRole("button", {
        name: "Percent",
      }),
    );
    fireEvent.change(screen.getByLabelText("Compact buffer percent"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply to 2 selected" }));
    expect(readUsageContextPreferences().overrides[polly]).toEqual({
      contextWindowTokens: 100_000,
      autoCompactBufferTokens: 20_000,
      autoCompactBufferPercent: 0,
      compactBufferMode: "percent",
    });
    expect(readUsageContextPreferences().overrides[claude]).toEqual({
      contextWindowTokens: 200_000,
      autoCompactBufferTokens: 30_000,
      autoCompactBufferPercent: 0,
      compactBufferMode: "percent",
    });
  });

  it("rejects a batch context total that would meet a selected source's token buffer", () => {
    renderSources({
      preferences: preferencesWith({
        [polly]: override(100_000, 20_000),
        [claude]: override(200_000, 30_000),
      }),
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Select all shown" }));
    fireEvent.change(screen.getByLabelText("Context total"), { target: { value: "30000" } });
    expect(screen.getByRole("button", { name: "Apply to 2 selected" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("less than every selected context total");
    expect(readUsageContextPreferences().overrides).toEqual({});
  });

  it("applies the visible Percent unit to a token source in the next batch", () => {
    writeUsageContextPreferences(
      preferencesWith({
        [polly]: override(100_000, 20_000),
        [claude]: override(200_000, 30_000),
      }),
    );
    renderLiveSources();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-a" }));
    fireEvent.click(
      within(screen.getByRole("group", { name: "Selected buffer unit" })).getByRole("button", {
        name: "Percent",
      }),
    );
    fireEvent.change(screen.getByLabelText("Compact buffer percent"), { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply to 1 selected" }));

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-b" }));
    fireEvent.change(screen.getByLabelText("Compact buffer percent"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply to 1 selected" }));
    expect(readUsageContextPreferences().overrides[claude]).toMatchObject({
      autoCompactBufferTokens: 30_000,
      autoCompactBufferPercent: 5,
      compactBufferMode: "percent",
    });
  });

  it("applies the visible Tokens unit to a percent source in the next batch", () => {
    writeUsageContextPreferences(
      preferencesWith({
        [polly]: override(100_000, 20_000),
        [claude]: {
          ...override(200_000, 30_000),
          autoCompactBufferPercent: 5,
          compactBufferMode: "percent",
        },
      }),
    );
    renderLiveSources();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-a" }));
    fireEvent.change(screen.getByLabelText("Compact buffer"), { target: { value: "10000" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply to 1 selected" }));

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-b" }));
    fireEvent.change(screen.getByLabelText("Compact buffer"), { target: { value: "4000" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply to 1 selected" }));
    expect(readUsageContextPreferences().overrides[claude]).toMatchObject({
      autoCompactBufferTokens: 4_000,
      autoCompactBufferPercent: 5,
      compactBufferMode: "tokens",
    });
  });

  it("applies the visible Percent unit when Auto is chosen in the next batch", () => {
    writeUsageContextPreferences(
      preferencesWith({
        [polly]: override(100_000, 20_000),
        [claude]: override(200_000, 30_000),
      }),
    );
    renderLiveSources();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-a" }));
    fireEvent.click(
      within(screen.getByRole("group", { name: "Selected buffer unit" })).getByRole("button", {
        name: "Percent",
      }),
    );
    fireEvent.change(screen.getByLabelText("Compact buffer percent"), { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply to 1 selected" }));

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-b" }));
    fireEvent.click(screen.getByRole("button", { name: "Auto compact buffer" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply to 1 selected" }));
    expect(readUsageContextPreferences().overrides[claude]).toEqual({
      contextWindowTokens: 200_000,
      autoCompactBufferTokens: 30_000,
      compactBufferMode: "percent",
    });
  });

  it("keeps each selected source's buffer unit when a batch changes only context total", () => {
    const preferences = preferencesWith({
      [polly]: override(100_000, 20_000),
      [claude]: {
        ...override(200_000, 30_000),
        autoCompactBufferPercent: 5,
        compactBufferMode: "percent",
      },
    });
    renderSources({ preferences });
    fireEvent.click(screen.getByRole("checkbox", { name: "Select all shown" }));
    fireEvent.change(screen.getByLabelText("Context total"), { target: { value: "300000" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply to 2 selected" }));
    expect(readUsageContextPreferences().overrides[polly]).toEqual({
      contextWindowTokens: 300_000,
      autoCompactBufferTokens: 20_000,
    });
    expect(readUsageContextPreferences().overrides[claude]).toEqual({
      contextWindowTokens: 300_000,
      autoCompactBufferTokens: 30_000,
      autoCompactBufferPercent: 5,
      compactBufferMode: "percent",
    });
  });

  it("closes the inline editor when its saved row changes or is deleted", () => {
    writeUsageContextPreferences(
      preferencesWith({
        [polly]: override(100_000, 10_000),
        [claude]: override(200_000, 20_000),
      }),
    );
    renderLiveSources();

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
    const untouched = sourceKey("host-b", "polly", "pi", "model-b");
    renderSources({
      preferences: preferencesWith({
        [polly]: override(100_000, 10_000),
        [claude]: override(200_000, 20_000),
        [untouched]: override(300_000, 30_000),
      }),
    });

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-a" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha polly model-b" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    const dialog = screen.getByRole("dialog", { name: "Delete 2 saved sources?" });
    expect(within(dialog).getByText("Alpha · Pi · model-a (Current)")).toBeInTheDocument();
    expect(within(dialog).getByText("Alpha · Pi · model-b")).toBeInTheDocument();
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

  it("edits cell and column records without materializing an inherited buffer", () => {
    const source = usageContextSourceFromKey(polly)!;
    const cell = usageContextLevelKey(source, "cell");
    const column = usageContextLevelKey(source, "column");
    renderSources({
      preferences: preferencesWith({
        [column]: override(400_000, 20_000),
        [polly]: override(300_000, null),
      }),
    });
    expect(
      screen.getByText("polly · all hosts: 400,000 context · 20,000 buffer"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha polly All models" }));
    expect(screen.getByLabelText("Edit compact buffer")).toHaveValue(null);
    fireEvent.change(screen.getByLabelText("Edit context total"), { target: { value: "350000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(readUsageContextPreferences().overrides[cell]).toEqual(override(350_000, null));
    expect(readUsageContextPreferences().overrides[polly]).toEqual(override(300_000, null));
    fireEvent.click(screen.getByRole("button", { name: "Edit column values" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit Server polly All hosts" }));
    fireEvent.change(screen.getByLabelText("Edit context total"), { target: { value: "500000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(readUsageContextPreferences().overrides[column]).toEqual(override(500_000, 20_000));
  });

  it("shows the header editor, counts and a fixed row count independently of model count", () => {
    const many = Object.fromEntries(
      Array.from({ length: 20 }, (_, index) => [
        sourceKey("host-a", "polly", "pi", `model-${index}`),
        override(400_000 + index, null),
      ]),
    );
    render(
      <SavedContextSources
        preferences={preferencesWith(many)}
        currentSourceKey={polly}
        hosts={HOSTS}
        currentOverride={override(null, null)}
      />,
    );
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(4);
    expect(table).toHaveTextContent("20 model overrides");
    expect(table).not.toHaveTextContent("model-19");
    fireEvent.click(screen.getByRole("button", { name: "Edit polly · Pi · all hosts" }));
    expect(screen.getByRole("button", { name: "Edit Server polly All hosts" })).toBeInTheDocument();
  });

  it("shows invalid buffer notes with effective provenance in the manager", () => {
    const column = usageContextLevelKey(usageContextSourceFromKey(polly)!, "column");
    renderSources({
      preferences: preferencesWith({
        [polly]: override(100_000, null),
        [column]: override(null, 150_000),
      }),
    });
    expect(
      screen.getByText("Ignored polly · all hosts: buffer ≥ context; using reported."),
    ).toBeInTheDocument();
    expect(screen.getByText("Buffer Auto · reported")).toBeInTheDocument();
  });

  it("keeps the nine records until Merge and collapses only the seven identical model records", () => {
    const overrides = Object.fromEntries([
      ...["model-a", "model-b", "model-c", "model-d", "model-e"].map((model) => [
        sourceKey("host-a", "polly", "pi", model),
        override(550_000, 32_000),
      ]),
      ...["gpt-a", "gpt-b"].map((model) => [
        sourceKey("host-a", "codex", "codex-native", model),
        override(400_000, 0),
      ]),
      [sourceKey("host-a", "opencode", "opencode", ""), override(200_000, null)],
      [sourceKey("host-b", "codex", "codex", "gpt-a"), override(300_000, null)],
    ]);
    writeUsageContextPreferences(preferencesWith(overrides));
    renderLiveSources();
    expect(
      screen.getByText("7 model records with identical values can be merged into 2 cell values."),
    ).toBeInTheDocument();
    expect(readUsageContextPreferences().overrides).toEqual(overrides);
    fireEvent.click(screen.getByRole("button", { name: "Keep" }));
    expect(readUsageContextPreferences().overrides).toEqual(overrides);
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "Manage saved sources" })).getByRole("button", {
        name: "Close",
      }),
    );
    openManager();
    fireEvent.click(screen.getByRole("button", { name: "Merge" }));
    const saved = readUsageContextPreferences().overrides;
    expect(Object.keys(saved)).toHaveLength(4);
    expect(saved[usageContextLevelKey(usageContextSourceFromKey(polly)!, "cell")]).toEqual(
      override(550_000, 32_000),
    );
    expect(screen.queryByRole("button", { name: "Merge" })).not.toBeInTheDocument();
    expect(screen.queryByText("model-a")).not.toBeInTheDocument();
    expect(screen.getByText("All models")).toBeInTheDocument();
  });
});
