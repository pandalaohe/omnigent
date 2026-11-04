import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TablerIconPicker, searchTablerIcons } from "./TablerIconPicker";

const data = vi.hoisted(() => ({ loadIcons: vi.fn(), loadTags: vi.fn() }));
vi.mock("@/lib/tablerIconData", () => ({
  tablerIconsIfLoaded: () => null,
  loadTablerIcons: data.loadIcons,
  loadTablerIconTags: data.loadTags,
}));

const names = ["folder", "rocket", "rocket-off", "space", "code", "brand-github", "flame", "zoom"];
const path = [["path", { d: "M4 4h16v16h-16z" }]];
const ICON_SET = Object.fromEntries(names.map((name) => [name, path]));
const TAGS = {
  rocket: { category: "Map", tags: ["galaxy", "spaceship"] },
  flame: { category: "Nature", tags: ["fire", "hot"] },
  zoom: { tags: ["magnifier"] },
};

beforeEach(() => {
  data.loadIcons.mockReset();
  data.loadTags.mockReset();
  data.loadIcons.mockResolvedValue(ICON_SET);
  data.loadTags.mockResolvedValue(TAGS);
});

afterEach(cleanup);

async function renderPicker() {
  const onSelect = vi.fn();
  render(<TablerIconPicker onSelect={onSelect} />);
  await screen.findByTestId("tabler-icon-rocket");
  return onSelect;
}

describe("searchTablerIcons", () => {
  it("keeps the given order for an empty query", () => {
    expect(searchTablerIcons(names, TAGS, "  ")).toEqual(names);
  });

  it("ranks exact and prefix names before metadata matches", () => {
    expect(searchTablerIcons(names, TAGS, "rocket")).toEqual(["rocket", "rocket-off"]);
    expect(searchTablerIcons(names, TAGS, "space")).toEqual(["space", "rocket"]);
    expect(searchTablerIcons(names, TAGS, "FIRE")).toEqual(["flame"]);
    expect(searchTablerIcons(names, null, "fire")).toEqual([]);
  });

  it("requires every word to match", () => {
    expect(searchTablerIcons(names, TAGS, "rocket off")).toEqual(["rocket-off"]);
    expect(searchTablerIcons(names, TAGS, "rocket nature")).toEqual([]);
  });
});

describe("TablerIconPicker", () => {
  it("shows suggested icons first, then the remaining set", async () => {
    await renderPicker();
    const order = screen
      .getAllByRole("button")
      .map((button) => button.getAttribute("aria-label"))
      .filter(Boolean);
    expect(order.slice(0, 5)).toEqual(["folder", "rocket", "code", "brand-github", "flame"]);
    expect(order).toContain("zoom");
  });

  it("searches by name and metadata", async () => {
    await renderPicker();
    const search = screen.getByTestId("tabler-search");
    fireEvent.change(search, { target: { value: "rock" } });
    expect(screen.getByTestId("tabler-icon-rocket")).toBeInTheDocument();
    expect(screen.getByTestId("tabler-icon-rocket-off")).toBeInTheDocument();
    expect(screen.queryByTestId("tabler-icon-folder")).toBeNull();

    fireEvent.change(search, { target: { value: "fire" } });
    expect(screen.getByTestId("tabler-icon-flame")).toBeInTheDocument();
    fireEvent.change(search, { target: { value: "nothing-like-this" } });
    expect(screen.getByText(/No icons match/)).toBeInTheDocument();
  });

  it("selects tabler:<name> on click", async () => {
    const onSelect = await renderPicker();
    fireEvent.click(screen.getByTestId("tabler-icon-rocket"));
    expect(onSelect).toHaveBeenCalledWith("tabler:rocket");
  });

  it("moves real focus through the grid and back to search", async () => {
    await renderPicker();
    const search = screen.getByTestId("tabler-search");
    fireEvent.keyDown(search, { key: "ArrowDown" });
    const folder = screen.getByTestId("tabler-icon-folder");
    expect(folder).toHaveFocus();
    expect(folder).toHaveAttribute("tabindex", "0");

    fireEvent.keyDown(folder, { key: "ArrowRight" });
    const rocket = screen.getByTestId("tabler-icon-rocket");
    expect(rocket).toHaveFocus();
    expect(folder).toHaveAttribute("tabindex", "-1");

    fireEvent.keyDown(rocket, { key: "ArrowUp" });
    expect(search).toHaveFocus();
  });

  it("picks the first result with Enter in search", async () => {
    const onSelect = await renderPicker();
    const search = screen.getByTestId("tabler-search");
    fireEvent.change(search, { target: { value: "code" } });
    fireEvent.keyDown(search, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith("tabler:code");
  });

  it("offers Emoji guidance and retries a failed shapes load", async () => {
    data.loadIcons.mockRejectedValueOnce(new Error("offline"));
    render(<TablerIconPicker onSelect={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Icons are offline. Emoji work here.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry icons" }));
    expect(await screen.findByTestId("tabler-icon-rocket")).toBeInTheDocument();
  });

  it("keeps name search working when the tags chunk fails", async () => {
    data.loadTags.mockRejectedValueOnce(new Error("offline"));
    await renderPicker();
    fireEvent.change(screen.getByTestId("tabler-search"), { target: { value: "rock" } });
    expect(screen.getByTestId("tabler-icon-rocket")).toBeInTheDocument();
    expect(screen.queryByTestId("tabler-icon-folder")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
