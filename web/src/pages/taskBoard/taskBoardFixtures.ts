import type { BoardTask, TaskSource } from "./taskBoardData";
import type { BoardSession } from "./taskBoardNative";

/** Neutral source readings shared by component checks and the interactive acceptance page. */
export function taskBoardFixture(now = Date.now()) {
  const at = (hours: number) => new Date(now + hours * 3_600_000).toISOString();
  const sources: TaskSource[] = [
    {
      id: "engineering",
      name: "Engineering",
      source: "https://tracker.example.com",
      created: at(-72),
    },
    { id: "docs", name: "Documentation", source: "https://docs.example.com", created: at(-72) },
  ];
  const tasks: BoardTask[] = [
    {
      uid: "upload",
      title: "Recover interrupted uploads",
      progress: "in-process",
      created: at(-48),
      updated: at(-1),
      "omnigent.ai:source": "engineering",
      "omnigent.ai:key": "ENG-123",
      "omnigent.ai:group": { id: "reliability", name: "Reliability" },
      "omnigent.ai:next": "Check the retry limit on the slow connection.",
      "omnigent.ai:workedBy": [
        { session: "parent", from: at(-6) },
        { session: "child", from: at(-4) },
      ],
      description: "Resume uploads from the last confirmed chunk.",
      percentComplete: 65,
      links: {
        page: { href: "https://tracker.example.com/upload", rel: "alternate" },
        spec: {
          href: "https://docs.example.com/retry",
          rel: "describedby",
          title: "Retry contract",
        },
      },
      relatedTo: { rollout: { relation: { parent: true } } },
      "omnigent.ai:history": [
        {
          id: "commit-one",
          time: at(-1),
          kind: "commit",
          note: "Keep completed chunks when the connection drops.",
        },
      ],
    },
    {
      uid: "review",
      title: "Choose the retry budget",
      progress: "needs-action",
      created: at(-24),
      updated: at(-2),
      "omnigent.ai:source": "engineering",
      "omnigent.ai:key": "ENG-124",
      "omnigent.ai:group": { id: "reliability", name: "Reliability" },
      "omnigent.ai:waiting": {
        on: "user",
        since: at(-2),
        note: "Approve a limit of three retries.",
      },
      "omnigent.ai:sourceFields": {
        impact: { label: "Impact", value: "Slower connections can recover without restarting." },
      },
      relatedTo: { upload: { relation: { "omnigent.ai:depends-on": true } } },
    },
    {
      uid: "retire",
      title: "Retire the legacy endpoint",
      progress: "cancelled",
      created: at(-60),
      updated: at(-4),
      progressUpdated: at(-4),
      "omnigent.ai:source": "engineering",
      "omnigent.ai:group": { id: "compatibility", name: "Compatibility" },
      links: { page: { href: "javascript:alert(1)", rel: "alternate" } },
    },
    {
      uid: "rollout",
      title: "Ship resumable transfers",
      progress: "needs-action",
      created: at(-72),
      updated: at(-3),
      "omnigent.ai:source": "engineering",
      "omnigent.ai:group": { id: "release", name: "Release" },
    },
    {
      uid: "guide",
      title: "Publish the recovery guide",
      progress: "completed",
      created: at(-40),
      updated: at(-6),
      progressUpdated: at(-6),
      "omnigent.ai:source": "docs",
      "omnigent.ai:group": { id: "guide", name: "Guides" },
      relatedTo: { upload: { relation: { first: true } } },
    },
    ...Array.from({ length: 8 }, (_, i): BoardTask => ({
      uid: `platform-${i}`,
      title: `Check platform ${i + 1}`,
      progress: i === 2 ? "failed" : "completed",
      created: at(-36 + i * 2),
      updated: at(-12 + i),
      progressUpdated: at(-12 + i),
      "omnigent.ai:source": "engineering",
      "omnigent.ai:group": {
        id: i < 4 ? "compatibility" : "release",
        name: i < 4 ? "Compatibility" : "Release",
      },
      relatedTo: { rollout: { relation: { parent: true } } },
    })),
  ];
  const sessions: BoardSession[] = [
    {
      id: "parent",
      title: "Transfer reliability",
      status: "running",
      host_id: "workstation",
      host_online: true,
      workspace: "/opt/work/transfer",
      git_branch: "feature/resume",
      updated_at: now / 1000,
      total_cost_usd: 1.25,
      todos: [
        { content: "Reproduce a dropped connection", status: "completed" },
        {
          content: "Verify resumed chunks",
          activeForm: "Verifying resumed chunks",
          status: "in_progress",
        },
      ],
    },
    {
      id: "child",
      title: "Check retry behaviour",
      status: "waiting",
      parent_session_id: "parent",
      host_id: "workstation",
      host_online: true,
      pending_elicitations_count: 1,
      updated_at: now / 1000,
      total_cost_usd: 0.65,
    },
    {
      id: "archived",
      title: "Document the recovery path",
      status: "idle",
      archived: true,
      archived_at: now / 1000 - 3600,
      host_id: "laptop",
      workspace: "/opt/work/docs",
      total_cost_usd: 0.3,
    },
  ];
  return { tasks, sources, sessions, updated: at(0) };
}
