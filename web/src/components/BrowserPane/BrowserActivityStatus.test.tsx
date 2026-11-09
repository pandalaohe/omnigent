import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { beginBrowserActivity, BROWSER_ACTIVITY_QUIET_MS } from "@/lib/browserActivity";
import { BrowserActivityStatus } from "./BrowserActivityStatus";

const stop = vi.hoisted(() => vi.fn());
vi.mock("@/store/chatStore", () => ({ useChatStore: { getState: () => ({ stop }) } }));

afterEach(() => {
  cleanup();
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
  stop.mockReset();
});

it("keeps concurrent actions visible through remount and clears after the last quiet period", () => {
  vi.useFakeTimers();
  const first = beginBrowserActivity("browser-tab:session:one");
  const second = beginBrowserActivity("browser-tab:session:one");
  const pane = render(<BrowserActivityStatus viewId="browser-tab:session:one" />);
  expect(screen.getByRole("status")).toHaveTextContent("Session is operating this page");
  act(() => {
    first();
    vi.advanceTimersByTime(BROWSER_ACTIVITY_QUIET_MS * 2);
  });
  expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled();
  pane.unmount();
  render(<BrowserActivityStatus viewId="browser-tab:session:one" />);
  fireEvent.click(screen.getByRole("button", { name: "Stop" }));
  expect(stop).toHaveBeenCalledWith("session");
  act(() => {
    second();
    second();
    vi.advanceTimersByTime(BROWSER_ACTIVITY_QUIET_MS - 1);
  });
  expect(screen.getByRole("status")).toBeInTheDocument();
  act(() => vi.advanceTimersByTime(1));
  expect(screen.queryByRole("status")).toBeNull();
});

it("renews the quiet period and never labels another tab as operating", () => {
  vi.useFakeTimers();
  const finish = beginBrowserActivity("session");
  finish();
  const pane = render(<BrowserActivityStatus viewId="browser-tab:session:other" />);
  expect(screen.queryByRole("button")).toBeNull();
  pane.rerender(<BrowserActivityStatus viewId="session" />);
  act(() => vi.advanceTimersByTime(2000));
  let end!: () => void;
  act(() => {
    end = beginBrowserActivity("session");
    vi.advanceTimersByTime(1000);
  });
  expect(screen.getByRole("status")).toBeInTheDocument();
  act(() => {
    end();
    vi.advanceTimersByTime(BROWSER_ACTIVITY_QUIET_MS);
  });
  expect(screen.queryByRole("status")).toBeNull();
});
