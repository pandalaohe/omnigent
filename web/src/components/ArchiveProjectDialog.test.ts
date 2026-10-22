import { describe, expect, it } from "vitest";
import type { ProjectArchivePreview, ProjectArchiveResult } from "@/lib/projectsApi";
import { projectAncestorIdsToMove, projectArchiveResultMessage } from "./ArchiveProjectDialog";

function session(
  id: string,
  parent_id: string | null,
  project_id: string | null,
  belongs_to_project?: boolean,
): ProjectArchivePreview["sessions"][number] {
  return {
    id,
    title: id,
    status: "idle",
    parent_id,
    project_id,
    belongs_to_project,
    via_parent_outside_project: false,
    host_keeps_cli: false,
  };
}

describe("project archive preview", () => {
  it("moves source ancestors through a foreign intermediate and includes legacy membership", () => {
    const crossProject = [
      session("A", null, "P"),
      session("B", "A", "Q", false),
      session("C", "B", "P"),
    ];
    expect(projectAncestorIdsToMove(crossProject, ["C"], "P")).toEqual(["A"]);

    const legacy = [session("A", null, null, true), ...crossProject.slice(1)];
    expect(projectAncestorIdsToMove(legacy, ["C"], "P")).toEqual(["A"]);
  });

  it("names each resumed automation and its returned next run", () => {
    const result: ProjectArchiveResult = {
      status: "complete",
      project: { id: "P", name: "Project" },
      batch: "batch",
      sessions: { done: ["C"], skipped: [] },
      tasks: {
        done: [{ id: "task", name: "Morning brief", next_run_at: "2026-10-23T08:00:00Z" }],
        skipped: [],
      },
      failed: [],
    };
    const message = projectArchiveResultMessage("Project", result, true);
    expect(message).toContain("Morning brief (next:");
    expect(message).toContain(new Date("2026-10-23T08:00:00Z").toLocaleString());
    expect(message).toContain("resumed");
  });
});
