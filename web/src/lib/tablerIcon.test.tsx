import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectRowIcon } from "@/shell/ProjectPicker";
import { ProjectIconGlyph, TABLER_PREFIX, parseProjectIcon, tablerValue } from "./tablerIcon";

const data = vi.hoisted(() => ({ loadIcons: vi.fn(), loadTags: vi.fn() }));
vi.mock("@/lib/tablerIconData", () => ({
  tablerIconsIfLoaded: () => null,
  loadTablerIcons: data.loadIcons,
  loadTablerIconTags: data.loadTags,
}));

const ROCKET_D = "M4 13a8 8 0 0 1 7 7";
const ICON_SET = { rocket: [["path", { d: ROCKET_D }]] };

beforeEach(() => {
  data.loadIcons.mockReset();
  data.loadTags.mockReset();
  data.loadIcons.mockResolvedValue(ICON_SET);
  data.loadTags.mockResolvedValue({});
});

afterEach(cleanup);

describe("parseProjectIcon", () => {
  it("distinguishes empty, Emoji and Tabler values", () => {
    expect(parseProjectIcon(null)).toEqual({ kind: "none" });
    expect(parseProjectIcon("")).toEqual({ kind: "none" });
    expect(parseProjectIcon("🚀")).toEqual({ kind: "emoji", text: "🚀" });
    expect(parseProjectIcon("tabler:rocket")).toEqual({ kind: "tabler", name: "rocket" });
    expect(parseProjectIcon("tabler:")).toEqual({ kind: "tabler", name: "" });
    expect(tablerValue("rocket")).toBe(`${TABLER_PREFIX}rocket`);
  });
});

describe("ProjectRowIcon with Tabler values", () => {
  it("keeps Emoji as text and does not load the Tabler chunk", () => {
    render(<ProjectRowIcon icon="🚀" />);
    expect(screen.getByTestId("project-icon")).toHaveTextContent("🚀");
    expect(data.loadIcons).not.toHaveBeenCalled();
  });

  it("holds an empty box while loading, then draws in currentColor", async () => {
    let resolve!: (set: typeof ICON_SET) => void;
    data.loadIcons.mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    render(<ProjectRowIcon icon="tabler:rocket" className="size-4 text-[16px]" />);
    const box = screen.getByTestId("project-icon");
    expect(box).toHaveAttribute("data-tabler-icon", "rocket");
    expect(box.querySelector("svg")).toBeNull();
    expect(box.className).toContain("size-4");

    await act(async () => {
      resolve(ICON_SET);
      await Promise.resolve();
    });
    await waitFor(() => expect(box.querySelector("svg path")).not.toBeNull());
    expect(box.querySelector("svg path")?.getAttribute("d")).toBe(ROCKET_D);
    expect(box.querySelector("svg")?.getAttribute("stroke")).toBe("currentColor");
  });

  it("falls back to the folder for an unknown name", async () => {
    const { container } = render(<ProjectRowIcon icon="tabler:nope" className="size-4" />);
    await waitFor(() => expect(container.querySelector(".lucide-folder")).not.toBeNull());
    expect(container.querySelector(".lucide-folder")?.getAttribute("class")).toContain("size-4");
  });

  it("falls back to the folder when the chunk fails", async () => {
    data.loadIcons.mockRejectedValueOnce(new Error("offline"));
    const { container } = render(<ProjectRowIcon icon="tabler:rocket" />);
    await waitFor(() => expect(container.querySelector(".lucide-folder")).not.toBeNull());
  });
});

describe("ProjectIconGlyph", () => {
  it("renders Emoji as text and Tabler as a 1em SVG", async () => {
    const { container, rerender } = render(<ProjectIconGlyph icon="🔥" />);
    expect(container.textContent).toBe("🔥");
    rerender(<ProjectIconGlyph icon="tabler:rocket" />);
    await waitFor(() => expect(container.querySelector("svg path")).not.toBeNull());
    expect(container.querySelector("svg")?.getAttribute("class")).toContain("size-[1em]");
  });
});
