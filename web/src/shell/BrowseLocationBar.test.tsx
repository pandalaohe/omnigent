import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BrowseLocationBar } from "./BrowseLocationBar";

vi.mock("./WorkspacePickerDialog", () => ({
  WorkspacePickerDialog: ({ open }: { open: boolean }) =>
    open ? <div data-testid="stub-picker-dialog" /> : null,
}));

const WORKSPACE = "/home/user/proj";

function renderBar(overrides: Partial<ComponentProps<typeof BrowseLocationBar>> = {}) {
  return render(
    <BrowseLocationBar
      current={WORKSPACE}
      workspace={WORKSPACE}
      hostId="host_test"
      canBrowseOutside={true}
      reach={{ unconfined: true, roots: [] }}
      onNavigate={vi.fn()}
      onOpenPath={vi.fn(async () => null)}
      {...overrides}
    />,
  );
}

function startEditing(): HTMLInputElement {
  fireEvent.click(screen.getByTestId("browse-location-path"));
  return screen.getByRole<HTMLInputElement>("textbox", { name: "File or folder path" });
}

describe("BrowseLocationBar editable path", () => {
  afterEach(cleanup);

  it("turns the path into a fully selected input on click", () => {
    renderBar();

    const input = startEditing();

    expect(input).toHaveValue(WORKSPACE);
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe(WORKSPACE.length);
    expect(screen.queryByTestId("browse-location-path")).toBeNull();
  });

  it("commits the typed text with Enter and closes on success", async () => {
    const onOpenPath = vi.fn(async () => null);
    renderBar({ onOpenPath });

    const input = startEditing();
    fireEvent.change(input, { target: { value: "report/sub" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(onOpenPath).toHaveBeenCalledWith("report/sub"));
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "File or folder path" })).toBeNull(),
    );
    expect(screen.getByTestId("browse-location-path")).toBeInTheDocument();
  });

  it("keeps the field open with the typed text and shows the error when the commit fails", async () => {
    renderBar({ onOpenPath: vi.fn(async () => "Not found: nope") });

    const input = startEditing();
    fireEvent.change(input, { target: { value: "nope" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() =>
      expect(screen.getByTestId("browse-location-error")).toHaveTextContent("Not found: nope"),
    );
    expect(screen.getByRole("textbox", { name: "File or folder path" })).toHaveValue("nope");
  });

  it("ignores further Enter while a commit is pending", async () => {
    let release: (value: string | null) => void = () => undefined;
    const onOpenPath = vi.fn(
      () =>
        new Promise<string | null>((resolve) => {
          release = resolve;
        }),
    );
    renderBar({ onOpenPath });

    const input = startEditing();
    fireEvent.change(input, { target: { value: "report/sub" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onOpenPath).toHaveBeenCalledTimes(1);
    release(null);
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "File or folder path" })).toBeNull(),
    );
  });

  it("honours a second commit after cancelling a pending one and drops the first result", async () => {
    const resolvers: ((value: string | null) => void)[] = [];
    const onOpenPath = vi.fn(
      () =>
        new Promise<string | null>((resolve) => {
          resolvers.push(resolve);
        }),
    );
    renderBar({ onOpenPath });

    const first = startEditing();
    fireEvent.change(first, { target: { value: "src" } });
    fireEvent.keyDown(first, { key: "Enter" });
    fireEvent.keyDown(first, { key: "Escape" });

    const second = startEditing();
    fireEvent.change(second, { target: { value: "other" } });
    fireEvent.keyDown(second, { key: "Enter" });

    // The live Enter starts its own attempt even while the first is in flight.
    expect(onOpenPath).toHaveBeenCalledTimes(2);
    expect(onOpenPath).toHaveBeenLastCalledWith("other");

    // The first attempt's late error must not surface under the new draft.
    resolvers[0]("Not found: src");
    await act(async () => {});
    expect(screen.queryByTestId("browse-location-error")).toBeNull();
    expect(screen.getByRole("textbox", { name: "File or folder path" })).toHaveValue("other");

    resolvers[1](null);
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "File or folder path" })).toBeNull(),
    );
  });

  it("restores the path with Escape without committing", () => {
    const onOpenPath = vi.fn(async () => null);
    renderBar({ onOpenPath });

    const input = startEditing();
    fireEvent.change(input, { target: { value: "somewhere/else" } });
    fireEvent.keyDown(input, { key: "Escape" });

    expect(screen.queryByRole("textbox", { name: "File or folder path" })).toBeNull();
    expect(screen.getByTestId("browse-location-path")).toHaveTextContent(WORKSPACE);
    expect(onOpenPath).not.toHaveBeenCalled();
  });

  it("restores the path on blur", () => {
    renderBar();

    const input = startEditing();
    fireEvent.change(input, { target: { value: "somewhere/else" } });
    fireEvent.blur(input);

    expect(screen.queryByRole("textbox", { name: "File or folder path" })).toBeNull();
    expect(screen.getByTestId("browse-location-path")).toHaveTextContent(WORKSPACE);
  });

  it("edits the path even when the session cannot roam", async () => {
    const onOpenPath = vi.fn(async () => null);
    renderBar({ canBrowseOutside: false, reach: null, hostId: null, onOpenPath });

    const input = startEditing();
    fireEvent.change(input, { target: { value: "src" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onOpenPath).toHaveBeenCalledWith("src");
    expect(screen.queryByRole("button", { name: "Browse folders" })).toBeNull();
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "File or folder path" })).toBeNull(),
    );
  });

  it("shows the folder picker button only while roaming", () => {
    const { rerender } = renderBar({ canBrowseOutside: false, reach: null, hostId: null });
    expect(screen.queryByRole("button", { name: "Browse folders" })).toBeNull();

    rerender(
      <BrowseLocationBar
        current={WORKSPACE}
        workspace={WORKSPACE}
        hostId="host_test"
        canBrowseOutside={true}
        reach={{ unconfined: true, roots: [] }}
        onNavigate={vi.fn()}
        onOpenPath={vi.fn(async () => null)}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Browse folders" }));

    expect(screen.getByTestId("stub-picker-dialog")).toBeInTheDocument();
  });
});
