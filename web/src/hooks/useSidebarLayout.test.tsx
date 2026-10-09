import { act, cleanup, renderHook } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";

import { defaultLayout, insertSection, type SidebarLayout } from "@/lib/sidebarLayout";
import { USER_PREFERENCES_PATCH_REJECTED_EVENT } from "@/lib/userPreferencesSync";
import type * as userPreferencesSyncModule from "@/lib/userPreferencesSync";
import { useSidebarLayout } from "./useSidebarLayout";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));

vi.mock("@/lib/userPreferencesSync", async (importOriginal) => {
  const actual = await importOriginal<typeof userPreferencesSyncModule>();
  return { ...actual, queueUserPreferencePatch: queuePatchMock };
});

vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));

const STORAGE_KEY = "omnigent:sidebar-layout";
const CHANGED_EVENT = "omnigent:sidebar-layout-changed";

afterEach(() => {
  cleanup();
  localStorage.clear();
  queuePatchMock.mockReset();
  vi.mocked(toast.error).mockReset();
});

describe("useSidebarLayout", () => {
  it("reads the default layout when nothing is stored", () => {
    const { result } = renderHook(() => useSidebarLayout());
    expect(result.current.layout).toEqual(defaultLayout());
  });

  it("saves the whole normalized value without the implicit flag and queues one patch", () => {
    const { result } = renderHook(() => useSidebarLayout());
    const next = insertSection(result.current.layout, {
      id: "sec_work",
      kind: "projects",
      name: "Work",
      maxRows: 10,
      projectIds: ["p1"],
    });

    act(() => result.current.saveLayout(next));

    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY)!) as SidebarLayout;
    expect(stored.sections[0]).toEqual({
      id: "sec_work",
      kind: "projects",
      name: "Work",
      maxRows: 10,
      projectIds: ["p1"],
    });
    expect(stored.sections[1]).toEqual({
      id: "default-favorites",
      kind: "favorites",
      name: "Pinned",
      maxRows: null,
      items: [],
    });
    expect(JSON.stringify(stored)).not.toContain("implicit");
    expect(queuePatchMock).toHaveBeenCalledTimes(1);
    expect(queuePatchMock).toHaveBeenCalledWith("sidebar_layout", stored);
    expect(result.current.layout.sections[0].id).toBe("sec_work");
  });

  it("re-reads the stored layout on the category and storage events", () => {
    const { result } = renderHook(() => useSidebarLayout());
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        version: 1,
        sections: [{ id: "a", kind: "projects", name: "A", maxRows: null, projectIds: [] }],
      }),
    );
    act(() => window.dispatchEvent(new Event(CHANGED_EVENT)));
    expect(result.current.layout.sections.map((section) => section.id)).toEqual(["a"]);

    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        version: 1,
        sections: [{ id: "b", kind: "recent", name: "B", maxRows: null, count: 5 }],
      }),
    );
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY })));
    expect(result.current.layout.sections.map((section) => section.id)).toEqual(["b"]);
  });

  it("shows an error toast when the server rejects the namespace", () => {
    renderHook(() => useSidebarLayout());
    act(() =>
      window.dispatchEvent(
        new CustomEvent(USER_PREFERENCES_PATCH_REJECTED_EVENT, {
          detail: { namespace: "sidebar_layout", status: 422 },
        }),
      ),
    );
    expect(toast.error).toHaveBeenCalledWith("Sidebar layout not saved");
  });
});
