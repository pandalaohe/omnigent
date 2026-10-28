import type { ProjectConfig } from "@/lib/projectsApi";

export function ProjectOrchestrationView({
  orchestration,
}: {
  orchestration: ProjectConfig["orchestration"];
}) {
  return (
    <section
      aria-labelledby="project-orchestration-heading"
      className="space-y-3 border-t border-border pt-4"
    >
      <h3 id="project-orchestration-heading" className="text-ui font-medium">
        Session orchestration
      </h3>
      <p className="text-ui text-muted-foreground">
        Project overrides for new parent sessions. Ask your agent to change these settings; each
        update requires your approval.
      </p>
      <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 text-ui">
        <dt className="text-muted-foreground">Child session limit</dt>
        <dd>
          {orchestration?.child_cap === "none"
            ? "Unlimited"
            : (orchestration?.child_cap ?? "Use account default")}
        </dd>
        <dt className="text-muted-foreground">Removed pass kinds</dt>
        <dd className="break-words">
          {orchestration?.standing_pass?.remove?.join(", ") || "None"}
        </dd>
        <dt className="text-muted-foreground">Pass exceptions</dt>
        <dd className="space-y-1 break-words">
          {orchestration?.standing_pass?.exceptions?.length
            ? orchestration.standing_pass.exceptions.map((exception) => (
                <p key={exception}>{exception}</p>
              ))
            : "None"}
        </dd>
      </dl>
    </section>
  );
}
