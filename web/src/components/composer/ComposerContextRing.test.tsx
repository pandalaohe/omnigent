import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queueUserPreferencePatch } from "@/lib/userPreferencesSync";
import {
  DEFAULT_USAGE_CONTEXT_PREFERENCES,
  readUsageContextPreferences,
  USAGE_CONTEXT_STORAGE_KEY,
  usageContextLevelKey,
  usageContextSettingsHref,
  usageContextSourceKey,
  writeUsageContextPreferences,
} from "@/lib/usageContextPreferences";

vi.mock("@/lib/userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));

const mocks = vi.hoisted(() => ({ navigate: vi.fn() }));

vi.mock("@/lib/routing", () => ({
  useNavigate: () => mocks.navigate,
}));

import { ComposerContextRing } from "./ComposerContextRing";

function renderRing(contextWindow: number | null, tokensUsed: number | null, sourceKey = "") {
  return render(
    <ComposerContextRing
      contextWindow={contextWindow}
      tokensUsed={tokensUsed}
      sourceKey={sourceKey}
    />,
  );
}

afterEach(cleanup);
beforeEach(() => {
  localStorage.clear();
  vi.mocked(queueUserPreferencePatch).mockClear();
  mocks.navigate.mockClear();
});

describe("ComposerContextRing", () => {
  it("renders nothing when the context window is unknown or zero", () => {
    expect(renderRing(null, 100).container).toBeEmptyDOMElement();
    expect(renderRing(0, 100).container).toBeEmptyDOMElement();
  });

  it("renders nothing when tokensUsed is unknown", () => {
    expect(renderRing(1000, null).container).toBeEmptyDOMElement();
  });

  it("keeps usage text out of the bar while preserving an accessible percentage", () => {
    renderRing(1000, 123);
    expect(screen.getByTestId("composer-context-ring")).not.toHaveTextContent("12%");
    expect(screen.getByTestId("composer-context-ring")).toHaveClass("shrink-0", "size-6");
    expect(screen.getByTestId("composer-context-ring")).not.toHaveClass("gap-1");
    expect(screen.getByLabelText("12% of context used")).toBeInTheDocument();
  });

  it("fits the SVG to the painted ring so its padding does not widen the label gap", () => {
    renderRing(1000, 490);
    const svg = screen.getByTestId("composer-context-ring").querySelector("svg");
    expect(svg).toHaveAttribute("viewBox", "1.5 1.5 13 13");
    expect(svg).toHaveAttribute("width", "13");
    expect(svg).toHaveAttribute("height", "13");
  });

  it("clamps over-full usage to 100%", () => {
    renderRing(1000, 4000);
    expect(screen.getByLabelText("100% of context used")).toBeInTheDocument();
    expect(screen.getByTestId("composer-context-ring")).not.toHaveTextContent("100%");
  });

  it("stays grayscale — never warning/destructive — even at high usage", () => {
    // WHY: the bar is ambient status, not an alarm (#7024). A near-full
    // context must not paint the old warning/destructive colors.
    renderRing(1000, 950);
    const ring = screen.getByTestId("composer-context-ring");
    expect(ring).toHaveClass("text-muted-foreground");
    expect(ring.className).not.toMatch(/text-(warning|destructive)/);
  });

  it("shows the float on hover, pins it on a click, and clears it beside or with Escape", async () => {
    const user = userEvent.setup();
    renderRing(1000, 123);
    expect(screen.queryByTestId("composer-context-float")).not.toBeInTheDocument();

    await user.hover(screen.getByTestId("composer-context-ring"));
    expect(await screen.findByText("12% context used")).toBeInTheDocument();
    expect(screen.getByText("123 / 1K tokens")).toHaveClass(
      "text-neutral-400",
      "dark:text-muted-foreground",
    );
    expect(screen.getByRole("button", { name: "Change context limits" })).toBeInTheDocument();

    // A click pins what hover opened: leaving the trigger no longer clears it.
    fireEvent.click(screen.getByTestId("composer-context-ring"));
    await user.unhover(screen.getByTestId("composer-context-ring"));
    await new Promise((resolve) => {
      setTimeout(resolve, 200);
    });
    expect(screen.getByTestId("composer-context-float")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("composer-context-float")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("composer-context-ring"));
    expect(screen.getByTestId("composer-context-float")).toBeInTheDocument();
    await user.click(document.body);
    expect(screen.queryByTestId("composer-context-float")).not.toBeInTheDocument();
  });

  it("pins the float on a tap and jumps to this session's row in Settings", async () => {
    const source = { hostId: "host-a", agentName: "codex", harness: "codex", model: "gpt-x" };
    const sourceKey = usageContextSourceKey(source);
    renderRing(1000, 123, sourceKey);

    fireEvent.click(screen.getByTestId("composer-context-ring"));
    const button = screen.getByRole("button", { name: "Change context limits" });
    expect(button).toHaveClass("min-h-11");
    expect(screen.getByTestId("composer-context-ring")).toHaveClass(
      "size-6",
      "after:absolute",
      "after:-inset-2.5",
    );

    fireEvent.click(button);
    expect(mocks.navigate).toHaveBeenCalledWith(usageContextSettingsHref(sourceKey));
  });

  it("uses the layered values and explains an ignored buffer below the entry", async () => {
    const source = { hostId: "host-a", agentName: "codex", harness: "codex", model: "gpt-x" };
    writeUsageContextPreferences({
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      overrides: {
        [usageContextLevelKey(source, "cell")]: {
          contextWindowTokens: 400_000,
          autoCompactBufferTokens: 500_000,
        },
        [usageContextLevelKey(source, "column")]: {
          contextWindowTokens: 500_000,
          autoCompactBufferTokens: 20_000,
        },
      },
    });
    localStorage.setItem("omnigent:context-indicator-mode", "compact-key-unused");
    render(
      <ComposerContextRing
        contextWindow={200_000}
        autoCompactTokenLimit={190_000}
        tokensUsed={190_000}
        sourceKey={usageContextSourceKey(source)}
      />,
    );
    await userEvent.setup().hover(screen.getByTestId("composer-context-ring"));
    expect(await screen.findByText("Context · codex on host-a")).toBeInTheDocument();
    expect(screen.getByText("Buffer 20,000 · codex, all computers")).toBeInTheDocument();
    expect(
      screen.getByText("Ignored codex on host-a: buffer ≥ context; using codex, all computers."),
    ).toBeInTheDocument();
  });

  it("never writes a saved value from the composer", async () => {
    const user = userEvent.setup();
    const source = { hostId: "host-a", agentName: "codex", harness: "codex", model: "gpt-x" };
    renderRing(1000, 123, usageContextSourceKey(source));
    await user.hover(screen.getByTestId("composer-context-ring"));
    await user.click(screen.getByTestId("composer-context-ring"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(localStorage.getItem(USAGE_CONTEXT_STORAGE_KEY)).toBeNull();
    expect(readUsageContextPreferences().overrides).toEqual({});
    expect(queueUserPreferencePatch).not.toHaveBeenCalled();
  });
});
