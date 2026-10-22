import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { invalidateProjectArchiveQueries } from "./useConversations";

describe("project archive cache invalidation", () => {
  it("invalidates the live archive and project query families", async () => {
    const client = new QueryClient();
    const keys = [
      ["projects", "with-archived"],
      ["conversations", "all"],
      ["project-sessions", "Project"],
      ["archived-conversations", "owner"],
      ["archived-session-facets", "owner"],
      ["archived-project-names", "owner"],
      ["scheduled-tasks", "owner"],
    ];
    for (const key of keys) client.setQueryData(key, { rows: [] });
    client.setQueryData(["archived"], { rows: [] });

    await invalidateProjectArchiveQueries(client);

    for (const key of keys) expect(client.getQueryState(key)?.isInvalidated).toBe(true);
    expect(client.getQueryState(["archived"])?.isInvalidated).toBe(false);
  });
});
