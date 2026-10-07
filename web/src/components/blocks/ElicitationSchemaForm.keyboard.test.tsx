// Schema-form cards take the focus chord, primary+Enter to submit once every
// required field is answered, and Esc to leave. Everything else stays native:
// Tab between fields, Space on a checkbox, Enter inside a text field.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useFocusQuestionCardHotkey } from "@/hooks/useQuestionCardHotkeys";
import {
  ElicitationSchemaForm,
  schemaFields,
  type ElicitationAnswers,
} from "./ElicitationSchemaForm";

afterEach(() => {
  cleanup();
  localStorage.clear();
});

beforeEach(() => {
  localStorage.clear();
});

const FIELDS = schemaFields({
  type: "object",
  properties: {
    branch: { type: "string", title: "Release branch" },
    channel: { type: "string", enum: ["beta", "stable"], title: "Channel" },
    force: { type: "boolean", title: "Force push" },
  },
  required: ["branch", "channel"],
});

function HotkeyHost() {
  useFocusQuestionCardHotkey();
  return null;
}

function renderForm() {
  const onSubmit = vi.fn<(content: ElicitationAnswers) => void>();
  const onReject = vi.fn();
  render(
    <div>
      <div data-composer-card>
        <textarea data-testid="composer" defaultValue="draft" />
      </div>
      <ElicitationSchemaForm fields={FIELDS} onSubmit={onSubmit} onReject={onReject} />
      <HotkeyHost />
    </div>,
  );
  return { onSubmit, onReject };
}

function card(): HTMLElement {
  return screen.getByTestId("elicitation-schema-form");
}

function enterCard(): void {
  screen.getByTestId("composer").focus();
  fireEvent.keyDown(window, { key: "F", code: "KeyF", ctrlKey: true, shiftKey: true });
}

function answerRequiredFields(): void {
  fireEvent.change(screen.getByLabelText(/Release branch/), {
    target: { value: "release/2.4" },
  });
  fireEvent.change(screen.getByLabelText(/Channel/), { target: { value: "beta" } });
}

describe("ElicitationSchemaForm — keyboard", () => {
  it("focuses the first field on the focus chord", () => {
    renderForm();

    enterCard();

    expect(document.activeElement).toBe(screen.getByLabelText(/Release branch/));
  });

  it("submits on primary+Enter only once every required field is answered", () => {
    const { onSubmit } = renderForm();
    enterCard();

    fireEvent.keyDown(card(), { key: "Enter", code: "Enter", ctrlKey: true });
    expect(onSubmit).not.toHaveBeenCalled();

    answerRequiredFields();
    fireEvent.keyDown(card(), { key: "Enter", code: "Enter", ctrlKey: true });

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({ branch: "release/2.4", channel: "beta" });
    expect(document.activeElement).toBe(screen.getByTestId("composer"));
  });

  it("leaves to the card's own composer on Esc", () => {
    const { onSubmit } = renderForm();
    enterCard();

    fireEvent.keyDown(card(), { key: "Escape", code: "Escape" });

    expect(document.activeElement).toBe(screen.getByTestId("composer"));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("shows the submit and leave bindings while focus is inside", () => {
    renderForm();

    enterCard();

    expect(screen.getByTestId("elicitation-schema-hint")).toHaveTextContent(
      "Ctrl ↵ submit · Esc leave",
    );
  });

  it("leaves Tab, plain Enter in fields and Space on checkboxes native", () => {
    renderForm();
    enterCard();

    const branch = screen.getByLabelText(/Release branch/);
    expect(fireEvent.keyDown(branch, { key: "Enter", code: "Enter" })).toBe(true);
    const force = screen.getByLabelText("Force push");
    expect(fireEvent.keyDown(force, { key: " ", code: "Space" })).toBe(true);
    expect(fireEvent.keyDown(force, { key: "Tab", code: "Tab" })).toBe(true);
  });

  it("returns focus to the composer for a keyboard-activated Submit", () => {
    const { onSubmit } = renderForm();
    enterCard();
    answerRequiredFields();

    fireEvent.click(screen.getByTestId("elicitation-schema-submit"), { detail: 0 });

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(screen.getByTestId("composer"));
  });

  it("leaves mouse-click focus alone on Submit", () => {
    const { onSubmit } = renderForm();
    enterCard();
    answerRequiredFields();
    const branch = screen.getByLabelText(/Release branch/);

    fireEvent.click(screen.getByTestId("elicitation-schema-submit"), { detail: 1 });

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(branch);
    expect(document.activeElement).not.toBe(screen.getByTestId("composer"));
  });
});
