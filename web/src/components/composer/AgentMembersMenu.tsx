import { LockIcon, PencilIcon, TriangleAlertIcon } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { ComposerAgentIcon } from "@/components/composer/ComposerAgentIcon";
import { AgentEditor } from "@/components/AgentEditor";
import { DropdownMenuItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { CUSTOM_AGENTS_QUERY_KEY, useCustomAgents, type CustomAgent } from "@/lib/customAgentsApi";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import {
  memberUnavailableReason,
  unavailableMembers,
  type SessionMember,
} from "@/lib/sessionMembers";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";

/** Stable empty child list for default props. */
const NO_CHILD_SESSIONS: readonly ChildSessionInfo[] = [];

/**
 * A member's settled child session, matched by sub-agent name only: the server
 * titles a child ``"{sub_agent_name}:{session_name}"``, so the summary's
 * ``tool`` is the role this member dispatched under. The instance
 * ``session_name`` can itself read as a role (``executor:reviewer``), so
 * matching on it would hand another role's child to this member.
 */
export function childSessionForRole(
  children: readonly ChildSessionInfo[],
  role: string,
): ChildSessionInfo | undefined {
  return children.find((child) => child.tool === role);
}

function memberConfigLine(member: SessionMember): string {
  return [member.host, member.model, member.effort].filter((part) => part !== null).join(" · ");
}

function AgentMemberRow({
  member,
  child,
}: {
  member: SessionMember;
  child: ChildSessionInfo | undefined;
}) {
  const unavailable = member.unavailable !== null;
  return (
    <div
      data-testid={`agent-member-row-${member.role}`}
      data-unavailable={unavailable ? "true" : undefined}
      className={cn(
        "composer-agent-row flex min-h-8 w-full items-start gap-2 rounded-lg px-2 py-1",
        unavailable && "opacity-60",
      )}
    >
      <span className="flex h-5 shrink-0 items-center">
        <ComposerAgentIcon agent={{ name: "", harness: member.harness }} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-1.5 text-[13px] leading-5">
          <span className="truncate font-medium">{member.role}</span>
          {member.lead && (
            <span
              data-testid={`agent-member-lead-${member.role}`}
              className="shrink-0 rounded-full border border-border px-1.5 text-[10px] leading-4 text-muted-foreground"
            >
              Lead
            </span>
          )}
          {unavailable && (
            <span
              data-testid={`agent-member-unavailable-${member.role}`}
              className="shrink-0 text-xs text-warning"
              title={memberUnavailableReason(member.unavailable ?? "")}
            >
              unavailable
            </span>
          )}
        </div>
        {memberConfigLine(member) !== "" && (
          <div className="truncate text-xs leading-4 text-muted-foreground">
            {memberConfigLine(member)}
          </div>
        )}
      </div>
      {child && (
        <DropdownMenuItem asChild className="shrink-0 p-0">
          <Link
            to={`/c/${child.id}`}
            data-testid={`agent-member-open-${member.role}`}
            className="flex h-6 items-center gap-0.5 rounded-md px-1.5 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none"
          >
            Open
          </Link>
        </DropdownMenuItem>
      )}
    </div>
  );
}

/**
 * Read-only members menu for a 2+ member session's composer trigger: one row
 * per member (lead first), a lock note, and a way to the saved Agent editor.
 * The session's own harness / model / effort controls are intentionally absent
 * — the roster is fixed at launch (member hand-over), so edits apply to new
 * sessions only.
 */
export function AgentMembersMenu({
  members,
  childSessions = NO_CHILD_SESSIONS,
  agentName = null,
  onEditAgent,
}: {
  members: readonly SessionMember[];
  childSessions?: readonly ChildSessionInfo[];
  agentName?: string | null;
  /** Present only when the session carries a saved-Agent template id. */
  onEditAgent?: () => void;
}) {
  return (
    <div data-testid="agent-members-menu">
      {agentName !== null && (
        <div className="truncate px-2 py-1 text-sm font-medium" data-testid="agent-members-title">
          {agentName}
        </div>
      )}
      <div
        data-testid="agent-members-lock-note"
        className="flex items-start gap-1.5 px-2 py-1 text-xs leading-4 text-muted-foreground"
      >
        <LockIcon className="mt-0.5 size-3 shrink-0" aria-hidden="true" />
        Members are fixed for this session; edits apply to new sessions
      </div>
      <div className="flex flex-col gap-0.5 py-1">
        {members.map((member) => (
          <AgentMemberRow
            key={member.role}
            member={member}
            child={childSessionForRole(childSessions, member.role)}
          />
        ))}
      </div>
      {onEditAgent !== undefined && (
        <>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            data-testid="composer-agent-edit-agent"
            onSelect={() => onEditAgent()}
            className="items-center text-13"
          >
            <PencilIcon className="size-4" />
            Edit agent
          </DropdownMenuItem>
        </>
      )}
    </div>
  );
}

/**
 * Chat banner above the composer naming every member that cannot run, with a
 * human reason per member. The lead unavailable pauses coordination; nothing
 * is substituted and sending is never blocked.
 */
export function AgentMembersBanner({ members }: { members: readonly SessionMember[] }) {
  const unavailable = unavailableMembers(members);
  if (unavailable.length === 0) return null;
  const lead = unavailable.find((member) => member.lead);
  const others = unavailable.filter((member) => !member.lead);
  return (
    <div
      data-testid="agent-members-banner"
      role="status"
      className="mx-3 mb-2 flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs leading-4 text-foreground"
    >
      <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden="true" />
      <div className="min-w-0 space-y-0.5">
        {lead && (
          <p data-testid="agent-members-banner-lead">
            Coordination is paused — {lead.role} (lead) can't run:{" "}
            {memberUnavailableReason(lead.unavailable)}.
          </p>
        )}
        {others.map((member) => (
          <p key={member.role} data-testid={`agent-members-banner-${member.role}`}>
            {member.role} can't run — {memberUnavailableReason(member.unavailable)}. Work sent to{" "}
            {member.role} is paused; nothing was handed to another member.
          </p>
        ))}
      </div>
    </div>
  );
}

/**
 * The saved Agent editor opened from a member session's menu. Mounted only on
 * demand (it fetches the custom-Agent catalog), so a session without the
 * template label never pays for the query. A save changes the saved Agent, not
 * the running session.
 */
export function EditMemberAgentDialog({
  templateId,
  onClose,
}: {
  templateId: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const agents = useCustomAgents();
  const agent: CustomAgent | undefined = agents.data?.find((row) => row.id === templateId);
  if (!agent) return null;
  return (
    <AgentEditor
      agent={agent}
      onClose={onClose}
      onSaved={async () => {
        await queryClient.invalidateQueries({ queryKey: CUSTOM_AGENTS_QUERY_KEY });
      }}
    />
  );
}
