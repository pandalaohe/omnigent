// Fork-owned: the four optional s10 props (badges, activeIndex, onTileHover,
// onBadgeClick) added to upstream's ComposerAttachments. Upstream's own
// ComposerAttachments.test.tsx stays unedited and covers the unchanged
// default rendering.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/host", () => ({ getEmbedRoot: () => null }));

import { ImageLightboxProvider } from "./ImageLightbox";
import { ComposerAttachments } from "./ComposerAttachments";

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => "blob:mock");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function files() {
  return [
    new File([new Uint8Array(4)], "shot.png", { type: "image/png" }),
    new File([new Uint8Array(4)], "notes.txt", { type: "text/plain" }),
  ];
}

function renderList(props: Partial<React.ComponentProps<typeof ComposerAttachments>> = {}) {
  return render(
    <ImageLightboxProvider>
      <ComposerAttachments files={files()} onRemove={vi.fn()} {...props} />
    </ImageLightboxProvider>,
  );
}

describe("ComposerAttachments s10 props", () => {
  it("renders a badge with the token label", () => {
    renderList({ badges: [{ label: "[image 1]", unreferenced: false }, null] });
    expect(screen.getByText("[image 1]")).toBeInTheDocument();
  });

  it("marks an unreferenced badge with a muted note", () => {
    renderList({ badges: [{ label: "[image 1]", unreferenced: true }, null] });
    expect(screen.getByText("· not in text")).toBeInTheDocument();
  });

  it("rings the tile at activeIndex", () => {
    const { container } = renderList({ activeIndex: 0 });
    const tiles = container.querySelectorAll(".relative.shrink-0");
    expect(tiles[0].querySelector(".ring-2")).not.toBeNull();
    expect(tiles[1].querySelector(".ring-2")).toBeNull();
  });

  it("calls onTileHover with the index on mouseenter/mouseleave", () => {
    const onTileHover = vi.fn();
    const { container } = renderList({ onTileHover });
    const tile = container.querySelectorAll(".relative.shrink-0")[0];
    fireEvent.mouseEnter(tile);
    expect(onTileHover).toHaveBeenCalledWith(0);
    fireEvent.mouseLeave(tile);
    expect(onTileHover).toHaveBeenCalledWith(null);
  });

  it("badge click calls onBadgeClick and does not open the lightbox", () => {
    const onBadgeClick = vi.fn();
    renderList({ badges: [{ label: "[image 1]", unreferenced: false }, null], onBadgeClick });
    fireEvent.click(screen.getByText("[image 1]"));
    expect(onBadgeClick).toHaveBeenCalledWith(0);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("image click still opens the lightbox", () => {
    renderList({ badges: [{ label: "[image 1]", unreferenced: false }, null] });
    fireEvent.click(screen.getByRole("button", { name: "Zoom image: shot.png" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
