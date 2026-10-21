import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ModelEffortFields } from "./ModelEffortFields";

vi.mock("@/hooks/useHosts", () => ({ useHostModelOptions: () => ({ data: undefined }) }));
afterEach(cleanup);

it.each(["pi-native", "pi", "acp:neutral", "devin-native"])(
  "does not offer Claude aliases for %s without its catalog",
  async (harness) => {
    render(
      <ModelEffortFields
        model="saved-model"
        effort="saved-effort"
        permissionMode=""
        speed=""
        harness={harness}
        hostId=""
        onModelChange={vi.fn()}
        onEffortChange={vi.fn()}
        onPermissionModeChange={vi.fn()}
        onSpeedChange={vi.fn()}
      />,
    );
    fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
    expect(await screen.findByRole("option", { name: "saved-model" })).toBeInTheDocument();
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(screen.queryByRole("option", { name: "Opus" })).not.toBeInTheDocument();
  },
);
