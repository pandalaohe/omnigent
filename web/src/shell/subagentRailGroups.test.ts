import { describe, expect, it } from "vitest";

import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { childPrimaryLabel, groupChildren, shortenPath } from "./subagentRailGroups";

function child(overrides: Partial<ChildSessionInfo> & { id: string }): ChildSessionInfo {
  return {
    title: null,
    task_summary: null,
    tool: null,
    session_name: null,
    current_task_status: null,
    busy: false,
    last_message_preview: null,
    pending_elicitations_count: 0,
    ...overrides,
  };
}

describe("groupChildren", () => {
  it("groups by host then cwd, ordered by newest child", () => {
    const groups = groupChildren(
      [
        child({ id: "a", host_id: "h1", cwd: "/p/one", created_at: 10 }),
        child({ id: "b", host_id: "h1", cwd: "/p/two", created_at: 30 }),
        child({ id: "c", host_id: "h2", cwd: "/p/one", created_at: 20 }),
        child({ id: "d", host_id: "h1", cwd: "/p/one", created_at: 40 }),
      ],
      null,
    );

    expect(groups.map((group) => group.hostId)).toEqual(["h1", "h2"]);
    expect(groups.map((group) => group.count)).toEqual([3, 1]);
    expect(groups[0].cwdGroups.map((group) => group.cwd)).toEqual(["/p/one", "/p/two"]);
    expect(groups[0].cwdGroups[0].children.map((row) => row.id)).toEqual(["d", "a"]);
    expect(groups[0].cwdGroups[1].children.map((row) => row.id)).toEqual(["b"]);
    expect(groups[1].cwdGroups[0].children.map((row) => row.id)).toEqual(["c"]);
  });

  it("folds hostless children into the root host, then into the local group", () => {
    const withRoot = groupChildren([child({ id: "a" })], "host-root");
    expect(withRoot).toHaveLength(1);
    expect(withRoot[0].hostId).toBe("host-root");

    const withoutRoot = groupChildren([child({ id: "a", cwd: "/p" })], null);
    expect(withoutRoot[0].hostId).toBe("local");
    expect(withoutRoot[0].cwdGroups[0].cwd).toBe("/p");

    const unknownCwd = groupChildren([child({ id: "a" })], null);
    expect(unknownCwd[0].cwdGroups[0].cwd).toBeNull();
  });

  it("keeps input order when timestamps are absent", () => {
    const groups = groupChildren(
      [child({ id: "first" }), child({ id: "second" }), child({ id: "third" })],
      null,
    );
    expect(groups[0].cwdGroups[0].children.map((row) => row.id)).toEqual([
      "first",
      "second",
      "third",
    ]);
  });
});

describe("shortenPath", () => {
  it("collapses /Users/<u> and /home/<u> to ~", () => {
    expect(shortenPath("/Users/me/projects/x")).toBe("~/projects/x");
    expect(shortenPath("/home/me/projects/x")).toBe("~/projects/x");
  });

  it("keeps only the last two segments when the result is still long", () => {
    expect(shortenPath("/opt/work/omnigent/fork/omnigent-scc18-agents-rail")).toBe(
      "…/fork/omnigent-scc18-agents-rail",
    );
    expect(shortenPath("/Users/me/dev/omnigent/fork/omnigent-scc18-agents-rail")).toBe(
      "…/fork/omnigent-scc18-agents-rail",
    );
  });

  it("leaves short non-home paths alone", () => {
    expect(shortenPath("/p/one")).toBe("/p/one");
    expect(shortenPath("")).toBe("");
  });
});

describe("childPrimaryLabel", () => {
  it("prefers the stored name, then the title suffix, then the title, then the summary", () => {
    expect(
      childPrimaryLabel({
        id: "x",
        title: "researcher:auth",
        session_name: "auth",
        task_summary: "T",
      }),
    ).toBe("auth");
    expect(childPrimaryLabel({ id: "x", title: "researcher:fix-sse", task_summary: "T" })).toBe(
      "fix-sse",
    );
    expect(childPrimaryLabel({ id: "x", title: "review", task_summary: "T" })).toBe("review");
    expect(childPrimaryLabel({ id: "x", task_summary: "Investigate" })).toBe("Investigate");
    expect(childPrimaryLabel({ id: "x", tool: "researcher" })).toBe("researcher");
    expect(childPrimaryLabel({ id: "x" })).toBe("x");
  });

  it("keeps the server-resolved tool label for native sub-agents", () => {
    const label = childPrimaryLabel({
      id: "x",
      title: "general-purpose:a09d",
      session_name: "a09d",
      tool: "wave-worker-696",
      labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" },
    });
    expect(label).toBe("wave-worker-696");
  });

  it("derives a native label from the labels when a snapshot has no tool", () => {
    expect(
      childPrimaryLabel({
        id: "x",
        title: "general-purpose:a09d",
        sub_agent_name: "general-purpose:debug-lead",
        labels: {
          "omnigent.wrapper": "claude-code-native-ui-subagent",
          "omnigent.claude_native.description": "wave-worker-696",
        },
      }),
    ).toBe("wave-worker-696");
  });

  it("keeps user-added ui titles on the generic path", () => {
    expect(
      childPrimaryLabel({
        id: "x",
        title: "ui:claude-native-ui:jimmy",
        session_name: "jimmy",
        tool: "claude-native-ui",
        labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" },
      }),
    ).toBe("jimmy");
  });
});
