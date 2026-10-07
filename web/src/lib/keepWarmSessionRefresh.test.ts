import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { childSessionsQueryKey } from "@/hooks/useChildSessions";
import { installKeepWarmSessionRefresh } from "./keepWarmSessionRefresh";
import { USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT } from "./userPreferencesSync";

describe("keep-warm session refresh", () => {
  const uninstallers: (() => void)[] = [];

  afterEach(() => {
    for (const uninstall of uninstallers.splice(0)) uninstall();
  });

  it("refetches every session list on a keep-warm acknowledgement only", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries").mockResolvedValue(undefined);
    uninstallers.push(installKeepWarmSessionRefresh(client));

    window.dispatchEvent(
      new CustomEvent(USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT, {
        detail: { namespace: "host_colors" },
      }),
    );
    expect(invalidate).not.toHaveBeenCalled();

    window.dispatchEvent(
      new CustomEvent(USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT, {
        detail: { namespace: "keep_warm" },
      }),
    );

    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["conversations"],
      refetchType: "all",
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["project-sessions"],
      refetchType: "all",
    });
    const childCall = invalidate.mock.calls.find(
      ([filters]) => typeof filters?.predicate === "function",
    );
    expect(childCall?.[0]).toMatchObject({ refetchType: "all" });
    const predicate = childCall?.[0]?.predicate;
    expect(predicate?.({ queryKey: childSessionsQueryKey("conv-1") } as never)).toBe(true);
    expect(predicate?.({ queryKey: [...childSessionsQueryKey("conv-1"), "past"] } as never)).toBe(
      true,
    );
    expect(predicate?.({ queryKey: ["conversation", "conv-1", "messages"] } as never)).toBe(false);
    expect(predicate?.({ queryKey: ["conversations"] } as never)).toBe(false);
  });
});
