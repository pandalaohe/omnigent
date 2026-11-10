import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ModelEffortFields } from "./ModelEffortFields";
import { useScheduledHarnessSettings } from "./useScheduledHarnessSettings";
import { testAgent } from "@/test/agentFixtures";

vi.mock("@/hooks/useHosts", () => ({ useHostModelOptions: () => ({ data: undefined }) }));
vi.mock("@/hooks/useSandboxModelOptions", () => ({
  useSandboxModelOptions: () => ({ data: undefined }),
  previewIsUnsupported: () => false,
}));
vi.mock("@/lib/agentLabels", () => ({ useHarnessCapabilities: () => ({}) }));
afterEach(cleanup);

function Fields({ harness }: { harness: string }) {
  const settings = useScheduledHarnessSettings(
    testAgent("agent", "fixture", { harness }),
    undefined,
    false,
    "saved-model",
    true,
  );
  return (
    <ModelEffortFields
      model="saved-model"
      effort="saved-effort"
      permissionMode=""
      speed=""
      settings={settings}
      onModelChange={vi.fn()}
      onEffortChange={vi.fn()}
      onPermissionModeChange={vi.fn()}
      onSpeedChange={vi.fn()}
    />
  );
}

it.each(["pi-native", "pi", "acp:neutral", "devin-native"])(
  "does not offer Claude aliases for %s without its catalog",
  async (harness) => {
    render(<Fields harness={harness} />);
    fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
    expect(await screen.findByRole("option", { name: "saved-model" })).toBeInTheDocument();
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(screen.queryByRole("option", { name: "Opus" })).not.toBeInTheDocument();
  },
);
