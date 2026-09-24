import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { CreateAgentDialog } from "./CreateAgentDialog";
import { setOmnigentHostConfig } from "@/lib/host";

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onCreate = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <CreateAgentDialog
        open
        onOpenChange={vi.fn()}
        onCreate={onCreate}
        extraFields={<div data-testid="badge-fields">Badge settings</div>}
      />
    </QueryClientProvider>,
  );
  return { onCreate };
}

function openTrigger() {
  const trigger = screen.getByTestId("agent-member-trigger");
  if (trigger.getAttribute("data-state") === "closed") {
    fireEvent.pointerDown(trigger, { button: 0, pointerType: "mouse" });
  }
}

async function choose(sectionTestId: string, optionTestId: string) {
  openTrigger();
  fireEvent.click(screen.getByTestId(sectionTestId));
  fireEvent.click(await screen.findByTestId(optionTestId));
}

afterEach(() => {
  cleanup();
  setOmnigentHostConfig({});
});

describe("CreateAgentDialog", () => {
  it("gives the form scroll region room for the fields' focus ring", () => {
    renderDialog();

    const scrollRegion = screen
      .getByTestId("create-agent-dialog")
      .querySelector(".overflow-y-auto");
    if (!scrollRegion) throw new Error("create-agent scroll region not found");
    // overflow-y-auto also clips horizontally at the padding box, so the
    // full-width fields need horizontal padding or their 3px focus ring is
    // chopped at the container's left/right edges. Negative margins keep the fields
    // visually aligned with the dialog header/footer.
    expect(scrollRegion).toHaveClass("px-3", "-mx-3", "py-2", "-my-2");
    expect(scrollRegion).toContainElement(screen.getByTestId("badge-fields"));
    expect(scrollRegion).not.toContainElement(screen.getByTestId("create-agent-submit"));
  });

  it("reports the lead's harness, model, and effort through the member trigger", async () => {
    const { onCreate } = renderDialog();
    fireEvent.change(screen.getByTestId("create-agent-name"), {
      target: { value: "release-lead" },
    });

    // No host is reachable in jsdom, so the Claude harness lists its static
    // aliases — the same fallback the scheduled-task fields use.
    await choose("agent-member-model", "agent-member-model-opus");
    await choose("agent-member-effort", "agent-member-effort-high");
    fireEvent.click(screen.getByTestId("create-agent-submit"));

    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        harness: "claude-sdk",
        model: "opus",
        reasoningEffort: "high",
      }),
    );
  });

  it("clears model and effort when the harness changes, and drops a ladderless Effort row", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("create-agent-name"), {
      target: { value: "release-lead" },
    });
    await choose("agent-member-model", "agent-member-model-opus");
    await choose("agent-member-effort", "agent-member-effort-high");

    await choose("agent-member-harness", "agent-member-harness-cursor");

    expect(screen.getByTestId("agent-member-agent-model-value")).toHaveTextContent("Default");
    expect(screen.queryByTestId("agent-member-agent-effort-value")).toBeNull();
    // Cursor has no effort ladder, so the submenu disappears with the label.
    expect(screen.queryByTestId("agent-member-effort")).toBeNull();
    expect(screen.getByTestId("create-agent-submit")).toBeDisabled();
  });

  it("accepts a hand-typed model id when the harness has no catalog rows", async () => {
    const { onCreate } = renderDialog();
    fireEvent.change(screen.getByTestId("create-agent-name"), {
      target: { value: "cursor-lead" },
    });
    // Cursor has no static aliases and jsdom reaches no host, so the Model
    // submenu offers only "Default" — the manual entry is the only way to pick.
    await choose("agent-member-harness", "agent-member-harness-cursor");

    openTrigger();
    fireEvent.click(screen.getByTestId("agent-member-model"));
    fireEvent.click(await screen.findByTestId("agent-member-model-other"));
    const field = await screen.findByTestId("agent-member-model-input");
    // Whitespace-only drafts are ignored.
    fireEvent.change(field, { target: { value: "   " } });
    fireEvent.keyDown(field, { key: "Enter" });
    expect(screen.getByTestId("create-agent-submit")).toBeDisabled();

    fireEvent.change(field, { target: { value: "  composer-2  " } });
    fireEvent.keyDown(field, { key: "Enter" });

    expect(screen.getByTestId("agent-member-agent-model-value")).toHaveTextContent("composer-2");
    expect(screen.getByTestId("create-agent-submit")).toBeEnabled();

    fireEvent.click(screen.getByTestId("create-agent-submit"));
    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({ harness: "cursor", model: "composer-2" }),
    );
  });

  it("reaches the manual model entry by keyboard through the Other model row", async () => {
    const user = userEvent.setup();
    const { onCreate } = renderDialog();
    await user.type(screen.getByTestId("create-agent-name"), "cursor-lead");

    // Keyboard-only journey: open the trigger, pick Cursor (whose empty
    // catalog leaves one model row), then arrow to the manual entry.
    const trigger = screen.getByTestId("agent-member-trigger");
    trigger.focus();
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{ArrowRight}{ArrowDown}{ArrowDown}");
    expect(screen.getByTestId("agent-member-harness-cursor")).toHaveFocus();
    await user.keyboard("{Enter}");
    await user.keyboard("{ArrowLeft}");
    expect(screen.getByTestId("agent-member-harness")).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByTestId("agent-member-model")).toHaveFocus();
    await user.keyboard("{ArrowRight}{ArrowDown}");
    expect(screen.getByTestId("agent-member-model-other")).toHaveFocus();
    await user.keyboard("{Enter}");
    const field = screen.getByTestId("agent-member-model-input");
    expect(field).toHaveFocus();

    await user.keyboard("composer-2");
    await user.keyboard("{Enter}");

    expect(screen.getByTestId("agent-member-agent-model-value")).toHaveTextContent("composer-2");
    await user.click(screen.getByTestId("create-agent-submit"));
    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({ harness: "cursor", model: "composer-2" }),
    );
  });

  it("emits the harness analytics event the replaced Select used to send", async () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    renderDialog();

    await choose("agent-member-harness", "agent-member-harness-cursor");

    expect(analytics).toHaveBeenCalledWith({
      type: "value_change",
      componentId: "create_agent.harness",
      componentKind: "select",
      value: "cursor",
    });
  });

  it("keeps Create disabled with a hint until a model is picked", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("create-agent-name"), {
      target: { value: "release-lead" },
    });
    expect(screen.getByTestId("create-agent-submit")).toBeDisabled();
    expect(screen.getByText(/Pick a model/)).toBeInTheDocument();

    await choose("agent-member-model", "agent-member-model-opus");

    expect(screen.getByTestId("create-agent-submit")).toBeEnabled();
    expect(screen.queryByText(/Pick a model/)).toBeNull();
  });
});
