// Adapted from omnigent-ae-public (Copyright 2026 Analytic Endeavors LLC, licensed under Apache-2.0).

import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BUILD_CHECK_INTERVAL_MS, resetAnnouncedBuildsForTests } from "@/lib/buildCheck";
import { EmbeddedProvider } from "@/lib/embedded";

const mocks = vi.hoisted(() => ({ toast: Object.assign(vi.fn(), { dismiss: vi.fn() }) }));
vi.mock("sonner", () => ({ toast: mocks.toast }));

import { ReloadNotice } from "./ReloadNotice";

const html = (name: string) => `<script type="module" crossorigin src="./assets/${name}"></script>`;

describe("ReloadNotice", () => {
  const fetchMock = vi.fn();
  const serve = (name: string) =>
    fetchMock.mockResolvedValue({ ok: true, text: async () => html(name) });
  const tick = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));

  beforeEach(() => {
    vi.useFakeTimers();
    resetAnnouncedBuildsForTests();
    mocks.toast.mockReset();
    mocks.toast.dismiss.mockReset();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    const script = document.createElement("script");
    script.type = "module";
    script.setAttribute("src", "./assets/index-OLD.js");
    document.head.appendChild(script);
  });
  afterEach(() => {
    document.head.querySelectorAll("script").forEach((s) => s.remove());
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("renders nothing and stays quiet while the build is the same", async () => {
    serve("index-OLD.js");
    const { container } = render(<ReloadNotice />);
    await tick(BUILD_CHECK_INTERVAL_MS * 2);
    expect(container.innerHTML).toBe("");
    expect(mocks.toast).not.toHaveBeenCalled();
  });

  it("shows one persistent toast for a new build, and Later keeps it quiet for that build", async () => {
    serve("index-NEW1.js");
    render(<ReloadNotice />);
    await tick(BUILD_CHECK_INTERVAL_MS * 3);
    expect(mocks.toast).toHaveBeenCalledTimes(1);
    const [title, options] = mocks.toast.mock.calls[0];
    expect(title).toBe("Omnigent was updated.");
    expect(options).toMatchObject({
      id: "reload-notice",
      description: "Reload to get the new version.",
      duration: Number.POSITIVE_INFINITY,
      action: { label: "Reload" },
      cancel: { label: "Later" },
    });
    options.cancel.onClick();
    expect(mocks.toast.dismiss).toHaveBeenCalledWith("reload-notice");
    await tick(BUILD_CHECK_INTERVAL_MS * 2);
    expect(mocks.toast).toHaveBeenCalledTimes(1);
    serve("index-NEW2.js");
    await tick(BUILD_CHECK_INTERVAL_MS);
    expect(mocks.toast).toHaveBeenCalledTimes(2);
  });

  it("stops checking after unmount", async () => {
    serve("index-NEW1.js");
    const { unmount } = render(<ReloadNotice />);
    unmount();
    await tick(BUILD_CHECK_INTERVAL_MS * 2);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("stays quiet for a build after Later even when the component remounts", async () => {
    serve("index-NEW1.js");
    const first = render(<ReloadNotice />);
    await tick(BUILD_CHECK_INTERVAL_MS);
    expect(mocks.toast).toHaveBeenCalledTimes(1);
    mocks.toast.mock.calls[0][1].cancel.onClick();
    first.unmount();
    render(<ReloadNotice />);
    await tick(BUILD_CHECK_INTERVAL_MS * 2);
    expect(mocks.toast).toHaveBeenCalledTimes(1);
  });

  it("does not check at all when embedded", async () => {
    serve("index-NEW1.js");
    render(
      <EmbeddedProvider>
        <ReloadNotice />
      </EmbeddedProvider>,
    );
    await tick(BUILD_CHECK_INTERVAL_MS * 2);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(mocks.toast).not.toHaveBeenCalled();
  });
});
