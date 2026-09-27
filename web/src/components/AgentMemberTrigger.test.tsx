import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentMemberTrigger } from "./AgentMemberTrigger";

vi.mock("@/hooks/useHosts", () => ({
  useHostModelOptions: () => ({ data: undefined, isLoading: false, error: null }),
}));

const HARNESS_OPTIONS = [
  { id: "claude-native", label: "Claude Code" },
  { id: "claude-sdk", label: "Claude SDK" },
  { id: "codex", label: "Codex" },
];

function renderTrigger(harness: string) {
  return render(
    <AgentMemberTrigger
      harness={harness}
      model={null}
      effort={null}
      harnessOptions={HARNESS_OPTIONS}
      hostId={null}
      onChange={vi.fn()}
    />,
  );
}

afterEach(cleanup);

describe("AgentMemberTrigger", () => {
  it.each(["claude-sdk", "codex"])(
    "draws the 15px SDK mark before the vendor logo for the %s harness",
    (harness) => {
      renderTrigger(harness);
      const mark = screen.getByTestId("sdk-mark");
      expect(mark).toHaveClass("size-[15px]");
      const img = screen.getByTestId("agent-member-trigger").querySelector("img");
      expect(img).toBeTruthy();
      expect(mark.compareDocumentPosition(img!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    },
  );

  it("leaves a native harness without the SDK mark", () => {
    renderTrigger("claude-native");
    expect(screen.queryByTestId("sdk-mark")).toBeNull();
    expect(screen.getByTestId("agent-member-trigger").querySelector("img")).toBeTruthy();
  });
});
