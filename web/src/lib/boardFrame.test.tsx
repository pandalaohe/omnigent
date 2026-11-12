import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { boardPath, boardExitProps, isFramePath } from "./boardFrame";
import { BoardFrameLayout, BoardFrameSetup } from "@/pages/BoardFrameLayout";

const auth = vi.hoisted(() => ({ url: null as string | null, listener: () => {} }));
vi.mock("./identity", () => ({
  getFrameLoginUrl: () => auth.url,
  subscribeFrameAuth: (listener: () => void) => {
    auth.listener = listener;
    return () => {};
  },
}));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete window.__OMNIGENT_BASE_PATH__;
  auth.url = null;
});

it("keeps board destinations within the frame and app destinations in a new tab, including a base path", () => {
  window.__OMNIGENT_BASE_PATH__ = "/panel";
  vi.stubGlobal("location", { pathname: "/panel/frame/usage/board" });
  expect(isFramePath()).toBe(true);
  expect(boardPath("/usage/board/account/settings")).toBe("/frame/usage/board/account/settings");
  expect(boardExitProps()).toEqual({ target: "_blank", rel: "noopener" });
  expect(isFramePath("/panel/framed/task-board")).toBe(false);
  vi.stubGlobal("location", { pathname: "/panel/task-board" });
  expect(boardPath("/usage/board")).toBe("/usage/board");
  expect(boardExitProps()).toEqual({});
});

it("renders the cached sign-in state and Retry reloads the current frame", () => {
  const reload = vi.fn();
  vi.stubGlobal("location", { reload });
  auth.url = "/login?return_to=%2Ftask-board";
  render(
    <MemoryRouter>
      <BoardFrameLayout />
    </MemoryRouter>,
  );
  const link = screen.getByRole("link", { name: "Sign in to Omnigent" });
  expect(link.getAttribute("href")).toBe(auth.url);
  expect(link.getAttribute("target")).toBe("_blank");
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(reload).toHaveBeenCalledOnce();
});

it("offers setup in the app from an unprovisioned frame", () => {
  render(
    <MemoryRouter>
      <BoardFrameSetup />
    </MemoryRouter>,
  );
  expect(screen.getByRole("heading").textContent).toBe("Omnigent is not set up yet");
  expect(screen.getByRole("link").getAttribute("target")).toBe("_blank");
});

it("replaces a loaded outlet when sign-in expires", () => {
  render(
    <MemoryRouter>
      <BoardFrameLayout />
    </MemoryRouter>,
  );
  expect(screen.queryByRole("link", { name: "Sign in to Omnigent" })).toBeNull();
  act(() => {
    auth.url = "/auth/login?return_to=%2Fusage%2Fboard";
    auth.listener();
  });
  expect(screen.getByRole("link", { name: "Sign in to Omnigent" }).getAttribute("href")).toBe(
    auth.url,
  );
});
