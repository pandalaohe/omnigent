import { describe, expect, it } from "vitest";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { childStatus } from "./subagentStatus";

const child: ChildSessionInfo = {
  id: "child",
  title: "Native child",
  task_summary: null,
  tool: "Claude",
  session_name: null,
  current_task_status: "in_progress",
  busy: true,
  native_activity_unverified: true,
  last_message_preview: null,
  pending_elicitations_count: 0,
};

describe("native snapshot connectivity", () => {
  it("shows uncertainty without changing the active task", () => {
    expect(childStatus(child).activity).toBe("unverified");
    expect(child.busy).toBe(true);
    expect(child.current_task_status).toBe("in_progress");
  });

  it("retains pending approval priority", () => {
    expect(childStatus({ ...child, pending_elicitations_count: 1 }).activity).toBe("awaiting");
  });

  it("does not cover an authoritative terminal outcome", () => {
    expect(childStatus({ ...child, busy: false, current_task_status: "completed" }).activity).toBe(
      "done",
    );
  });
});
