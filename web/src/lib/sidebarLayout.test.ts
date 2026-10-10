import { describe, expect, it } from "vitest";

import {
  addFavorite,
  defaultLayout,
  favoritesRows,
  insertSection,
  kindsAvailableToCreate,
  moveProjectToSection,
  moveSection,
  newSectionId,
  normalizeLayout,
  removeFavorite,
  removeSection,
  sectionOfProject,
  splitFavoritesReorder,
  type FavoriteRef,
  type SectionKind,
  type SidebarLayout,
  type SidebarSectionDef,
} from "./sidebarLayout";

function makeSection(
  id: string,
  kind: SectionKind,
  extra: Partial<SidebarSectionDef> = {},
): SidebarSectionDef {
  return { id, kind, name: id, maxRows: null, ...extra };
}

function layoutOf(...sections: SidebarSectionDef[]): SidebarLayout {
  return { version: 1, sections };
}

function rawLayout(sections: unknown[]): unknown {
  return { version: 1, sections };
}

describe("defaultLayout", () => {
  it("returns today's order with an implicit Pinned section", () => {
    const layout = defaultLayout();
    expect(layout.sections.map((section) => [section.kind, section.name, section.maxRows])).toEqual(
      [
        ["favorites", "Pinned", null],
        ["other_projects", "Projects", null],
        ["other_sessions", "Sessions", null],
      ],
    );
    expect(layout.sections[0].implicit).toBe(true);
    expect(layout.sections[0].items).toEqual([]);
  });
});

describe("newSectionId", () => {
  it("returns distinct non-empty ids", () => {
    const first = newSectionId();
    expect(first).not.toBe("");
    expect(newSectionId()).not.toBe(first);
  });
});

describe("normalizeLayout", () => {
  it("falls back to the default layout for absent or unusable values", () => {
    expect(normalizeLayout(null)).toEqual(defaultLayout());
    expect(normalizeLayout(undefined)).toEqual(defaultLayout());
    expect(normalizeLayout("nope")).toEqual(defaultLayout());
    expect(normalizeLayout({ version: 2, sections: [] })).toEqual(defaultLayout());
    expect(normalizeLayout({ version: 1 })).toEqual(defaultLayout());
    expect(normalizeLayout({ version: 1, sections: "nope" })).toEqual(defaultLayout());
  });

  it("keeps an explicitly empty section list empty", () => {
    expect(normalizeLayout({ version: 1, sections: [] })).toEqual({ version: 1, sections: [] });
  });

  it("drops an unknown kind and keeps a duplicate project id in its first section", () => {
    const layout = normalizeLayout(
      rawLayout([
        { id: "a", kind: "projects", name: "Work", maxRows: 10, projectIds: ["p1", "p2"] },
        { id: "b", kind: "mystery", name: "Nope", maxRows: null },
        { id: "c", kind: "projects", name: "Other", maxRows: null, projectIds: ["p2", "p3"] },
        { id: "d", kind: "other_projects", name: "Projects", maxRows: null },
        { id: "e", kind: "other_sessions", name: "Sessions", maxRows: null },
      ]),
    );

    expect(layout.sections.map((section) => section.id)).toEqual(["a", "c", "d", "e"]);
    expect(layout.sections[0].projectIds).toEqual(["p1", "p2"]);
    expect(layout.sections[1].projectIds).toEqual(["p3"]);
  });

  it("dedupes section ids and drops malformed sections", () => {
    const layout = normalizeLayout(
      rawLayout([
        { id: "a", kind: "projects", name: "Work", maxRows: null, projectIds: [] },
        { id: "a", kind: "projects", name: "Duplicate", maxRows: null, projectIds: [] },
        { id: "", kind: "projects", name: "No id", maxRows: null },
        { id: "b", kind: "projects", name: 7, maxRows: null },
        "nope",
      ]),
    );
    expect(layout.sections.map((section) => section.id)).toEqual(["a"]);
  });

  it("dedupes and drops malformed favorite refs", () => {
    const layout = normalizeLayout(
      rawLayout([
        {
          id: "f",
          kind: "favorites",
          name: "F",
          maxRows: null,
          items: [
            { type: "session", id: "s1" },
            { type: "session", id: "s1" },
            { type: "session", id: "" },
            { type: "mystery", id: "x" },
            { type: "project", id: "p1" },
            { type: "project", id: "p1" },
          ],
        },
      ]),
    );
    expect(layout.sections[0].items).toEqual([
      { type: "session", id: "s1" },
      { type: "project", id: "p1" },
    ]);
  });

  it("clamps recent counts and maxRows to their allowed values", () => {
    const firstSection = (sections: unknown[]): SidebarSectionDef =>
      normalizeLayout(rawLayout(sections)).sections[0];

    const zero = firstSection([{ id: "r", kind: "recent", name: "Zero", maxRows: 7, count: 0 }]);
    expect([zero.count, zero.maxRows]).toEqual([1, null]);

    const high = firstSection([{ id: "r", kind: "recent", name: "High", maxRows: 30, count: 26 }]);
    expect([high.count, high.maxRows]).toEqual([20, 30]);

    const text = firstSection([
      { id: "r", kind: "recent", name: "Text", maxRows: true, count: "9" },
    ]);
    expect([text.count, text.maxRows]).toEqual([5, null]);

    const missing = firstSection([{ id: "r", kind: "recent", name: "Missing", maxRows: 10 }]);
    expect([missing.count, missing.maxRows]).toEqual([5, 10]);
  });

  it("keeps the first favorites section and merges a second one's refs, deduped", () => {
    const layout = normalizeLayout(
      rawLayout([
        { id: "p", kind: "projects", name: "Work", maxRows: null, projectIds: [] },
        {
          id: "f1",
          kind: "favorites",
          name: "Favs",
          maxRows: null,
          items: [
            { type: "session", id: "s1" },
            { type: "project", id: "p1" },
          ],
        },
        {
          id: "f2",
          kind: "favorites",
          name: "Favs 2",
          maxRows: 10,
          items: [
            { type: "project", id: "p1" },
            { type: "session", id: "s2" },
          ],
        },
        { id: "r1", kind: "recent", name: "Recent", maxRows: null, count: 5 },
        { id: "r2", kind: "recent", name: "Recent 2", maxRows: null, count: 3 },
        { id: "op1", kind: "other_projects", name: "Projects", maxRows: null },
        { id: "op2", kind: "other_projects", name: "Projects 2", maxRows: null },
        { id: "os1", kind: "other_sessions", name: "Sessions", maxRows: null },
        { id: "os2", kind: "other_sessions", name: "Sessions 2", maxRows: null },
      ]),
    );

    expect(layout.sections.map((section) => section.id)).toEqual(["p", "f1", "r1", "op1", "os1"]);
    expect(layout.sections[1].items).toEqual([
      { type: "session", id: "s1" },
      { type: "project", id: "p1" },
      { type: "session", id: "s2" },
    ]);
    expect(kindsAvailableToCreate(layout)).toEqual(["projects"]);
  });

  it("caps section, project id, and ref counts", () => {
    const sections = Array.from({ length: 60 }, (_, index) =>
      makeSection(`s${index}`, "projects", { projectIds: [] }),
    );
    expect(normalizeLayout(layoutOf(...sections)).sections).toHaveLength(50);

    const projectIds = Array.from({ length: 250 }, (_, index) => `p${index}`);
    const projects = normalizeLayout(layoutOf(makeSection("a", "projects", { projectIds })));
    expect(projects.sections[0].projectIds).toHaveLength(200);

    const items: FavoriteRef[] = Array.from({ length: 250 }, (_, index) => ({
      type: "session",
      id: `s${index}`,
    }));
    const favorites = normalizeLayout(layoutOf(makeSection("f", "favorites", { items })));
    expect(favorites.sections[0].items).toHaveLength(200);
  });
});

describe("sectionOfProject", () => {
  it("finds the projects section holding the id", () => {
    const layout = layoutOf(
      makeSection("f", "favorites"),
      makeSection("a", "projects", { projectIds: ["p1", "p2"] }),
      makeSection("b", "projects", { projectIds: ["p3"] }),
    );
    expect(sectionOfProject(layout, "p1")).toBe("a");
    expect(sectionOfProject(layout, "p3")).toBe("b");
    expect(sectionOfProject(layout, "p9")).toBeNull();
  });
});

describe("moveProjectToSection", () => {
  const layout = layoutOf(
    makeSection("f", "favorites"),
    makeSection("a", "projects", { projectIds: ["p1", "p2"] }),
    makeSection("b", "projects", { projectIds: ["p3"] }),
  );

  it("moves a project between projects sections", () => {
    const moved = moveProjectToSection(layout, "p2", "b");
    expect(sectionOfProject(moved, "p2")).toBe("b");
    expect(moved.sections[1].projectIds).toEqual(["p1"]);
    expect(moved.sections[2].projectIds).toEqual(["p3", "p2"]);
  });

  it("removes a project from every projects section with null", () => {
    const removed = moveProjectToSection(layout, "p2", null);
    expect(sectionOfProject(removed, "p2")).toBeNull();
    expect(removed.sections[1].projectIds).toEqual(["p1"]);
  });

  it("returns the layout unchanged for an unknown or non-projects target", () => {
    expect(moveProjectToSection(layout, "p1", "missing")).toBe(layout);
    expect(moveProjectToSection(layout, "p1", "f")).toBe(layout);
  });
});

describe("insertSection", () => {
  it("inserts at the requested index and defaults to the top", () => {
    const layout = layoutOf(makeSection("a", "projects"), makeSection("b", "projects"));
    expect(insertSection(layout, makeSection("c", "projects")).sections.map((s) => s.id)).toEqual([
      "c",
      "a",
      "b",
    ]);
    expect(
      insertSection(layout, makeSection("c", "projects"), 1).sections.map((s) => s.id),
    ).toEqual(["a", "c", "b"]);
    expect(
      insertSection(layout, makeSection("c", "projects"), 99).sections.map((s) => s.id),
    ).toEqual(["a", "b", "c"]);
  });

  it("refuses a second favorites, recent, or system kind", () => {
    const layout = layoutOf(
      makeSection("p", "projects"),
      makeSection("f", "favorites"),
      makeSection("o", "other_sessions"),
    );
    expect(insertSection(layout, makeSection("f2", "favorites"))).toBe(layout);
    expect(insertSection(layout, makeSection("o2", "other_sessions"))).toBe(layout);
    expect(insertSection(layout, makeSection("op", "other_projects")).sections[0].kind).toBe(
      "other_projects",
    );
    expect(insertSection(layout, makeSection("op", "other_projects"), 0)).not.toBe(layout);

    const withRecent = insertSection(layout, makeSection("r", "recent"));
    expect(withRecent.sections[0].id).toBe("r");
    expect(insertSection(withRecent, makeSection("r2", "recent"))).toBe(withRecent);
    expect(insertSection(layout, makeSection("p2", "projects")).sections[0].id).toBe("p2");
  });
});

describe("moveSection", () => {
  const layout = layoutOf(
    makeSection("a", "projects"),
    makeSection("b", "projects"),
    makeSection("c", "projects"),
  );

  it("moves a section by direction, to an edge, or to an index", () => {
    expect(moveSection(layout, "b", "up").sections.map((s) => s.id)).toEqual(["b", "a", "c"]);
    expect(moveSection(layout, "b", "down").sections.map((s) => s.id)).toEqual(["a", "c", "b"]);
    expect(moveSection(layout, "c", "top").sections.map((s) => s.id)).toEqual(["c", "a", "b"]);
    expect(moveSection(layout, "a", "bottom").sections.map((s) => s.id)).toEqual(["b", "c", "a"]);
    expect(moveSection(layout, "a", 2).sections.map((s) => s.id)).toEqual(["b", "c", "a"]);
  });

  it("returns the layout unchanged when nothing moves or the id is unknown", () => {
    expect(moveSection(layout, "a", "up")).toBe(layout);
    expect(moveSection(layout, "a", 0)).toBe(layout);
    expect(moveSection(layout, "missing", "down")).toBe(layout);
  });
});

describe("removeSection", () => {
  it("removes the section and returns the layout for an unknown id", () => {
    const layout = layoutOf(
      makeSection("a", "projects", { projectIds: ["p1"] }),
      makeSection("b", "projects"),
    );
    const removed = removeSection(layout, "a");
    expect(removed.sections.map((s) => s.id)).toEqual(["b"]);
    expect(sectionOfProject(removed, "p1")).toBeNull();
    expect(removeSection(layout, "missing")).toBe(layout);
  });
});

describe("kindsAvailableToCreate", () => {
  it("always offers projects and only absent kinds otherwise", () => {
    expect(kindsAvailableToCreate(defaultLayout())).toEqual(["projects", "recent"]);
    const bare = layoutOf(makeSection("p", "projects"));
    expect(kindsAvailableToCreate(bare)).toEqual([
      "projects",
      "favorites",
      "recent",
      "other_projects",
      "other_sessions",
    ]);
  });
});

describe("favoritesRows", () => {
  it("follows pin order with an old-client reorder while project slots stay", () => {
    const section = makeSection("f", "favorites", {
      items: [
        { type: "session", id: "s1" },
        { type: "project", id: "p1" },
        { type: "session", id: "s2" },
      ],
    });
    expect(favoritesRows(section, ["s2", "s1"], new Set(["p1"]))).toEqual([
      { type: "session", id: "s2" },
      { type: "project", id: "p1" },
      { type: "session", id: "s1" },
    ]);
  });

  it("appends a pin with no ref in any section after the slots", () => {
    const section = makeSection("f", "favorites", {
      items: [
        { type: "project", id: "p2" },
        { type: "session", id: "s1" },
      ],
    });
    expect(favoritesRows(section, ["s1", "s3"], new Set(["p2"]))).toEqual([
      { type: "project", id: "p2" },
      { type: "session", id: "s1" },
      { type: "session", id: "s3" },
    ]);
  });

  it("skips unknown projects, dangling refs, and empty slots", () => {
    const section = makeSection("f", "favorites", {
      items: [
        { type: "session", id: "s1" },
        { type: "session", id: "dangling" },
        { type: "session", id: "s2" },
        { type: "project", id: "unknown" },
      ],
    });
    expect(favoritesRows(section, ["s2"], new Set())).toEqual([{ type: "session", id: "s2" }]);
  });

  it("gives a ref unpinned elsewhere no slot, so unreferenced pins stay at the end", () => {
    const section = makeSection("f", "favorites", {
      items: [
        { type: "session", id: "gone" },
        { type: "project", id: "p1" },
        { type: "session", id: "s1" },
      ],
    });
    expect(favoritesRows(section, ["s1", "s9"], new Set(["p1"]))).toEqual([
      { type: "project", id: "p1" },
      { type: "session", id: "s1" },
      { type: "session", id: "s9" },
    ]);
  });

  it("fills the slots with every pin in pin order, so partial refs stay in order", () => {
    // One session ref but two pins: the slot takes the first pin (a) and the
    // remaining pin (b) appends, so the rendered order is the full pin order.
    const section = makeSection("f", "favorites", {
      items: [{ type: "session", id: "b" }],
    });
    expect(favoritesRows(section, ["a", "b"], new Set())).toEqual([
      { type: "session", id: "a" },
      { type: "session", id: "b" },
    ]);
  });

  it("returns no rows for a non-favorites section", () => {
    expect(favoritesRows(makeSection("p", "projects"), ["s1"], new Set())).toEqual([]);
  });
});

describe("splitFavoritesReorder", () => {
  it("returns the new slot pattern and the session order", () => {
    const next: FavoriteRef[] = [
      { type: "session", id: "s2" },
      { type: "project", id: "p1" },
      { type: "session", id: "s1" },
      { type: "session", id: "s2" },
    ];
    expect(splitFavoritesReorder(next)).toEqual({
      items: [
        { type: "session", id: "s2" },
        { type: "project", id: "p1" },
        { type: "session", id: "s1" },
      ],
      sessionOrder: ["s2", "s1"],
    });
  });
});

describe("addFavorite", () => {
  it("appends to the existing favorites section without duplicates", () => {
    const layout = layoutOf(
      makeSection("f", "favorites", { items: [{ type: "session", id: "s1" }] }),
    );
    const added = addFavorite(layout, { type: "project", id: "p1" });
    expect(added.sections[0].items).toEqual([
      { type: "session", id: "s1" },
      { type: "project", id: "p1" },
    ]);
    expect(addFavorite(added, { type: "session", id: "s1" })).toBe(added);
  });

  it("creates a Favorites section at the top when none exists", () => {
    const layout = layoutOf(
      makeSection("op", "other_projects"),
      makeSection("os", "other_sessions"),
    );
    const added = addFavorite(layout, { type: "session", id: "s1" });
    expect(added.sections.map((section) => section.kind)).toEqual([
      "favorites",
      "other_projects",
      "other_sessions",
    ]);
    expect(added.sections[0].name).toBe("Favorites");
    expect(added.sections[0].maxRows).toBe(10);
    expect(added.sections[0].implicit).toBeUndefined();
    expect(added.sections[0].items).toEqual([{ type: "session", id: "s1" }]);
  });
});

describe("removeFavorite", () => {
  it("drops the ref and leaves an empty section in place", () => {
    const layout = layoutOf(
      makeSection("f", "favorites", {
        items: [
          { type: "session", id: "s1" },
          { type: "project", id: "p1" },
        ],
      }),
    );
    const removed = removeFavorite(layout, { type: "session", id: "s1" });
    expect(removed.sections[0].items).toEqual([{ type: "project", id: "p1" }]);
    expect(removeFavorite(removed, { type: "session", id: "s1" })).toBe(removed);
  });
});
