import { useState } from "react";
import { CopyIcon } from "lucide-react";
import { MessageResponse } from "@/components/ai-elements/message";
import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import type { InstructionPreview, LayerKey } from "@/hooks/useInstructionLayers";

const LABELS: Record<string, string> = {
  shared: "Shared",
  agent: "Agent",
  host: "Host",
  independent: "Independent",
  mother: "Mother block",
  child: "Child block",
  author: "Agent instructions",
  per_request: "Per-request instructions",
  skills_hint: "Skills",
  framework: "Omnigent",
  worktree: "Working tree",
  project_code: "Project code",
  mother_block: "Mother block",
  child_block: "Child block",
  quota_line: "Child allowance",
  peer_grant: "Peer messages",
  child_rule: "Child results",
  child_question: "Child questions",
  routing_note: "Routing",
};

export function RouteView({
  data,
  canEdit,
  onEdit,
  budget,
}: {
  data: InstructionPreview;
  canEdit: boolean;
  onEdit: (key: LayerKey, title: string) => void;
  budget: number;
}) {
  const [rendered, setRendered] = useState(true);
  const [expanded, setExpanded] = useState(
    () => new Set(data.parts.flatMap((part, index) => (part.readonly ? [] : [index]))),
  );
  const [copyState, setCopyState] = useState("");
  const sent = data.parts.filter((part) => part.sent);
  const replaced = data.parts.filter((part) => !part.sent && part.scope);
  const section = (part: InstructionPreview["parts"][number], index: number) => {
    const label = LABELS[part.scope ?? part.kind] ?? part.kind.replaceAll("_", " ");
    const partIndex = data.parts.indexOf(part);
    return (
      <details
        key={`${index}-${part.kind}-${part.scope ?? ""}`}
        open={expanded.has(partIndex)}
        onToggle={(event) => {
          const open = event.currentTarget.open;
          setExpanded((previous) => {
            if (previous.has(partIndex) === open) return previous;
            const next = new Set(previous);
            if (open) next.add(partIndex);
            else next.delete(partIndex);
            return next;
          });
        }}
        className={`group border-l-2 pl-5 ${part.sent ? "border-border" : "border-dashed opacity-60"}`}
      >
        <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm outline-offset-4 focus-visible:outline-2">
          <span className="font-medium">{label}</span>
          <span className="text-xs text-muted-foreground">
            {part.text === null ? "Set at launch" : `${part.chars.toLocaleString()} chars`}
            {part.readonly ? " · Read-only" : ""}
            {!part.sent ? " · Not sent on this route" : ""}
          </span>
          <span className="ml-auto text-xs text-muted-foreground group-open:hidden">Expand</span>
        </summary>
        <div className="max-w-prose break-words pb-5 text-sm leading-7">
          {part.text === null ? (
            <p className="text-muted-foreground">
              This section depends on the session or turn. Its text is not available in a route
              preview.
            </p>
          ) : rendered ? (
            <MessageResponse>{part.text || "No text in this layer."}</MessageResponse>
          ) : (
            <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-6">
              {part.text}
            </pre>
          )}
          {canEdit && part.scope && (
            <Button
              className="mt-2"
              variant="ghost"
              size="sm"
              onClick={() =>
                onEdit(
                  {
                    scope: part.scope!,
                    agent_key: part.agent_key ?? "",
                    host_id: part.host_id ?? "",
                  },
                  label,
                )
              }
            >
              Edit layer
            </Button>
          )}
        </div>
      </details>
    );
  };
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3 border-y py-3">
        <p className="text-sm">
          Previewing <strong>{data.subject.name}</strong>
          <span className="ml-2 text-xs text-muted-foreground">{data.subject.harness}</span>
        </p>
        <div className="flex flex-wrap gap-1">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setExpanded(new Set(data.parts.map((_, index) => index)))}
          >
            Expand all
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setExpanded(new Set())}>
            Collapse all
          </Button>
          <Button size="sm" variant="outline" onClick={() => setRendered(!rendered)}>
            {rendered ? "Read raw" : "Read formatted"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={async () => {
              try {
                await copyText(data.text);
                setCopyState("Copied complete text.");
              } catch {
                setCopyState("Could not copy. Select the raw text to copy it manually.");
              }
            }}
          >
            <CopyIcon /> Copy all
          </Button>
        </div>
      </div>
      {copyState && (
        <p role="status" className="text-sm">
          {copyState}
        </p>
      )}
      {data.framework_unavailable && (
        <p role="alert" className="rounded-md border p-3 text-sm">
          Omnigent's instructions cannot be shown because the agent spec is unavailable. This
          preview is incomplete.
        </p>
      )}
      <div className="space-y-3">{sent.map(section)}</div>
      {replaced.length > 0 && (
        <section className="space-y-3 border-t pt-5">
          <h3 className="text-sm font-medium">Replaced by the independent setting</h3>
          {replaced.map(section)}
        </section>
      )}
      <footer className="space-y-2 border-t pt-4 text-sm">
        <p>
          <strong>{data.total_chars.toLocaleString()} characters</strong> including separators
          {data.total_chars > budget ? ` · Over ${budget.toLocaleString()} reminder` : ""}. Text is
          never truncated.
        </p>
        <p className="text-xs text-muted-foreground">
          Host-side files such as CLAUDE.md and AGENTS.md are loaded separately and are not visible
          here. Launch- and turn-specific placeholders are not counted. Hosts on another Omnigent
          build may carry different framework text.
        </p>
      </footer>
    </div>
  );
}
