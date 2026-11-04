import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

interface ChoiceRecord {
  agent: string;
  model?: string;
  effort?: string;
  tier?: number | null;
  pool?: string | null;
  meter?: string | null;
  work_type: string;
  reason: string;
  flags?: string[];
  warnings?: string[];
  skipped?: {
    link: { agent: string; model?: string };
    why: string;
    until?: number | null;
    running?: string[];
  }[];
}

export function sessionChoiceRecord(
  labels: Record<string, string> | undefined,
): ChoiceRecord | null {
  const key = "omnigent.session_choice";
  try {
    let record = JSON.parse(labels?.[key] ?? "null");
    if (record && typeof record.chunks === "number") {
      if (!Number.isInteger(record.chunks) || record.chunks < 1 || record.chunks > 256) return null;
      record = JSON.parse(
        Array.from({ length: record.chunks }, (_, index) => labels?.[`${key}.${index}`] ?? "").join(
          "",
        ),
      );
    }
    if (
      !record ||
      typeof record !== "object" ||
      typeof record.reason !== "string" ||
      typeof record.agent !== "string" ||
      typeof record.work_type !== "string"
    )
      return null;
    const strings = (value: unknown) =>
      Array.isArray(value) && value.every((item) => typeof item === "string");
    if (
      ["model", "effort", "meter", "pool"].some(
        (field) => record[field] != null && typeof record[field] !== "string",
      ) ||
      ["flags", "warnings"].some((field) => record[field] != null && !strings(record[field])) ||
      (record.tier != null && (!Number.isInteger(record.tier) || record.tier < 1))
    )
      return null;
    if (
      record.skipped != null &&
      (!Array.isArray(record.skipped) ||
        record.skipped.some((item: unknown) => {
          if (!item || typeof item !== "object") return true;
          const skipped = item as NonNullable<ChoiceRecord["skipped"]>[number];
          return (
            typeof skipped.why !== "string" ||
            typeof skipped.link?.agent !== "string" ||
            (skipped.link.model != null && typeof skipped.link.model !== "string") ||
            (skipped.until != null && !Number.isFinite(skipped.until)) ||
            (skipped.running != null && !strings(skipped.running))
          );
        }))
    )
      return null;
    return record as ChoiceRecord;
  } catch {
    return null;
  }
}

export function SessionChoiceCard({
  labels,
  agentName,
}: {
  labels?: Record<string, string>;
  agentName?: string;
}) {
  const record = sessionChoiceRecord(labels);
  if (!record) return null;
  const queued = labels?.["omnigent.startup.session_choice"]?.startsWith("queued@") === true;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="h-7 max-w-44 shrink-0 truncate rounded-md border border-border px-2 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label="Why this agent was chosen"
        >
          {agentName ?? "Chosen agent"}
          {record.tier ? ` · Tier ${record.tier}` : ""}
          {queued ? " · Queued" : ""}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="max-h-[70vh] w-80 max-w-[calc(100vw-1rem)] space-y-3 overflow-y-auto text-sm"
      >
        <h3 className="font-medium">Session choice</h3>
        {queued && (
          <p className="text-xs text-muted-foreground">
            Queued. Waiting for capacity or reserve to become available.
          </p>
        )}
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
          <dt className="text-muted-foreground">Work type</dt>
          <dd>{record.work_type}</dd>
          <dt className="text-muted-foreground">Agent</dt>
          <dd className="break-words">{agentName ?? record.agent}</dd>
          <dt className="text-muted-foreground">Model</dt>
          <dd className="break-words">{record.model ?? "Agent default"}</dd>
          {record.effort && (
            <>
              <dt className="text-muted-foreground">Effort</dt>
              <dd>{record.effort}</dd>
            </>
          )}
          <dt className="text-muted-foreground">Meter</dt>
          <dd>{record.meter ?? "No meter"}</dd>
          {record.tier && (
            <>
              <dt className="text-muted-foreground">Tier</dt>
              <dd>{record.tier}</dd>
            </>
          )}
        </dl>
        <p>{record.reason}</p>
        {!!record.skipped?.length && (
          <div className="space-y-2 border-t border-border pt-2">
            <h4 className="text-xs font-medium">Skipped links</h4>
            {record.skipped.map((item, index) => (
              // eslint-disable-next-line react/no-array-index-key -- Read-only entries may repeat the same skipped link.
              <div key={`skipped:${index}`} className="text-xs">
                <p className="break-words">
                  {item.link.model ?? item.link.agent} · {item.why.replaceAll("_", " ")}
                </p>
                {item.until != null && (
                  <p className="text-muted-foreground">
                    Until {new Date(item.until * 1000).toLocaleString()}
                  </p>
                )}
                {!!item.running?.length && (
                  <p className="break-words text-muted-foreground">
                    Running: {item.running.join(", ")}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}
        {record.flags?.map((flag) => (
          <p key={flag} className="text-xs text-muted-foreground">
            {flag.replaceAll("_", " ")}
          </p>
        ))}
        {record.warnings?.map((warning) => (
          <p key={warning} className="text-xs text-warning">
            {warning}
          </p>
        ))}
      </PopoverContent>
    </Popover>
  );
}
