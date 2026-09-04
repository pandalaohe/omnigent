import { describe, expect, it } from "vitest";

import {
  buildArchiveConversationFilters,
  parseArchiveDateRange,
  type ArchiveLibraryViewState,
} from "./ArchiveLibraryToolbar";

const view: ArchiveLibraryViewState = {
  searchQuery: "handoff",
  searchScope: "content",
  project: "Omnigent",
  hostId: "host-win",
  agentName: "codex",
  createdRange: "260901-260904",
  archivedRange: "260902-260903",
  sortField: "title",
  order: "asc",
};

describe("ArchiveLibraryToolbar date ranges", () => {
  it("parses YYMMDD ranges with an inclusive end date", () => {
    const range = parseArchiveDateRange("260901-260904");
    expect(range).not.toBeNull();
    expect(new Date((range?.after ?? 0) * 1000).getDate()).toBe(1);
    expect(new Date((range?.before ?? 0) * 1000).getDate()).toBe(5);
  });

  it.each(["260901", "260931-261001", "260904-260901"])(
    "rejects invalid range %s",
    (value) => expect(parseArchiveDateRange(value)).toBeNull(),
  );

  it("maps compact view state to the Server filter contract", () => {
    const filters = buildArchiveConversationFilters(view, "debounced handoff");
    expect(filters).toMatchObject({
      searchQuery: "debounced handoff",
      searchScope: "content",
      project: "Omnigent",
      hostId: "host-win",
      agentName: "codex",
      sortField: "title",
      order: "asc",
    });
    expect(filters.createdAfter).toBeTypeOf("number");
    expect(filters.createdBefore).toBeGreaterThan(filters.createdAfter ?? 0);
    expect(filters.archivedBefore).toBeGreaterThan(filters.archivedAfter ?? 0);
  });
});
