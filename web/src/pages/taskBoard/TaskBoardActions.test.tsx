import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TaskBoardActions, taskActions } from "./TaskBoardActions";

vi.mock("@/hooks/useConversations", () => ({
  useProjects: () => ({
    data: [
      { id: "project-id", name: "Example" },
      { id: "other", name: "project-id" },
    ],
    isLoading: false,
  }),
}));
vi.mock("@/shell/NewChatDialog", () => ({
  NewChatLandingScreen: ({
    seed,
    onCreatingChange,
  }: {
    seed: { project: string; prompt: string };
    onCreatingChange: (creating: boolean) => void;
  }) => (
    <>
      <div role="status">{JSON.stringify(seed)}</div>
      <button type="button" onClick={() => onCreatingChange(true)}>
        Start request
      </button>
      <button type="button" onClick={() => onCreatingChange(false)}>
        Finish request
      </button>
    </>
  ),
}));
afterEach(cleanup);

it("keeps the composer open while its create request is pending", async () => {
  render(
    <TaskBoardActions
      actions={taskActions([{ kind: "new-session", label: "Continue", prompt: "Review" }])}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(await screen.findByRole("button", { name: "Start request" }));
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.getByRole("dialog")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Finish request" }));
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("skips unknown and malformed actions and bounds the button count", () => {
  const valid = { kind: "new-session", label: "Continue", prompt: "Review results" };
  expect(taskActions(null)).toEqual([]);
  expect(taskActions({})).toEqual([]);
  expect(
    taskActions([
      null,
      {},
      { ...valid, kind: "future" },
      { ...valid, project: null },
      { ...valid, label: " " },
      { ...valid, prompt: "x".repeat(2001) },
      valid,
    ]),
  ).toEqual([valid]);
  expect(taskActions(Array(9).fill(valid))).toHaveLength(8);
});

it.each([
  ["project-id", "Example"],
  ["Example", "Example"],
  ["missing", ""],
  [undefined, ""],
])(
  "opens a dismissible composer with project %s resolved to %s only after clicking",
  async (project, expected) => {
    render(
      <TaskBoardActions
        actions={taskActions([
          { kind: "new-session", label: "Continue", project, prompt: "Review results" },
        ])}
      />,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(
      await screen.findByText((text) => {
        try {
          const seed = JSON.parse(text);
          return seed.project === expected && seed.prompt === "Review results";
        } catch {
          return false;
        }
      }),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("button", { name: "Continue" })).toBeTruthy();
  },
);
