import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SystemMessageView } from "./SystemMessage";

afterEach(cleanup);

describe("SystemMessageView", () => {
  it("hides sub-agent wake notices instead of rendering a centered System row", () => {
    const { container } = render(
      <SystemMessageView
        message={{
          kind: "subagent_wake",
          label: "Sub-agent result ready",
          body: "",
        }}
      />,
    );

    expect(screen.queryByTestId("system-message")).toBeNull();
    expect(container.textContent).toBe("");
  });

  it("shows full questions, selections and multiline free text in a borderless record", () => {
    render(
      <SystemMessageView
        message={{
          kind: "question_answer",
          label: "Question answered",
          body: "",
          answers: [
            { question: "Which modes?", answer: "Read, Write" },
            { question: "Why?", answer: "First line\nSecond line" },
          ],
        }}
      />,
    );
    const record = screen.getByTestId("system-message");
    expect(record).toHaveAttribute("data-system-kind", "question_answer");
    expect(record).not.toHaveClass("border", "bg-muted");
    expect(screen.getByText("Which modes?")).toBeVisible();
    expect(screen.getByText("Read, Write")).toBeVisible();
    const multiline = screen.getByText("Why?").nextElementSibling;
    expect(multiline?.textContent).toBe("First line\nSecond line");
    expect(multiline).toHaveClass("whitespace-pre-wrap");
  });

  it("keeps an ambiguous historical reply readable without a filled box", () => {
    const body = "Why?\nGive context → because\nA → B is part of the answer";
    render(
      <SystemMessageView
        message={{
          kind: "question_answer",
          label: "Question answered",
          body,
          cardRef: "qabcdef",
        }}
      />,
    );
    const record = screen.getByTestId("system-message");
    expect(record.textContent).toContain(body);
    expect(record).not.toHaveClass("bg-muted", "border");
    expect(record.querySelector("p:last-child")).toHaveClass("whitespace-pre-wrap");
  });
});
