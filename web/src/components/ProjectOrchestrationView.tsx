import type { ProjectConfig } from "@/lib/projectsApi";
import type { ReactNode } from "react";

export function ProjectOrchestrationView({
  orchestration,
  choiceControl,
}: {
  orchestration: ProjectConfig["orchestration"];
  choiceControl?: ReactNode;
}) {
  const standingApproval = orchestration?.standing_approval ?? orchestration?.standing_pass;
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
        <dt className="text-muted-foreground">Removed approval kinds</dt>
        <dd className="break-words">{standingApproval?.remove?.join(", ") || "None"}</dd>
        <dt className="text-muted-foreground">Approval exceptions</dt>
        <dd className="space-y-1 break-words">
          {standingApproval?.exceptions?.length
            ? standingApproval.exceptions.map((exception) => <p key={exception}>{exception}</p>)
            : "None"}
        </dd>
      </dl>
      {choiceControl}
    </section>
  );
}
