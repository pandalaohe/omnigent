import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Combobox, COMBOBOX_OPTION_THRESHOLD } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import type { Host } from "@/hooks/useHosts";
import { CHOICE_SELECT } from "./ChoiceLinkEditor";
import {
  choiceRequest,
  type ChoiceCandidatePreview,
  type SessionChoiceDocument,
} from "./sessionChoiceData";
import type { ChoiceAgent } from "./sessionChoiceEdits";
import { limitRuleErrors } from "./choiceLimitEdits";

const FIELD_LABELS: Record<string, string> = {
  maxRunning: "Max running",
  "reserve.mode": "Usage mode",
  "reserve.window": "Usage window",
  "reserve.pct": "Warning",
  "reserve.stopPct": "Stop",
  reserveExempt: "Warning exemptions",
};

export function ChoiceCandidateCheck({
  document,
  hosts,
  agents,
  host,
  revision,
  previewEpoch,
}: {
  document: SessionChoiceDocument;
  hosts: Host[];
  agents: ChoiceAgent[];
  host: string;
  revision: string;
  previewEpoch?: Readonly<{ current: number }>;
}) {
  const [agent, setAgent] = useState("");
  const [targetHost, setTargetHost] = useState(host);
  const [model, setModel] = useState("");
  const [pending, setPending] = useState<{ key: string; id: number } | null>(null);
  const [error, setError] = useState<{ key: string; message: string } | null>(null);
  const [result, setResult] = useState<{ key: string; data: ChoiceCandidatePreview } | null>(null);
  const requestId = useRef(0);
  const epoch = previewEpoch?.current ?? 0;
  const key = JSON.stringify([document, agent, targetHost, model, revision, epoch]);
  const currentKey = useRef(key);
  currentKey.current = key;
  async function preview() {
    const id = ++requestId.current;
    const requestedKey = key;
    setPending({ key, id });
    setError(null);
    setResult(null);
    try {
      const errors = limitRuleErrors(document);
      if (errors.length) throw new Error(errors.join(" "));
      const data = await choiceRequest<ChoiceCandidatePreview>("/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: "candidate",
          host_id: targetHost,
          candidate: { agent_id: agent, model: model || null },
          settings: document,
        }),
      });
      if (
        id === requestId.current &&
        requestedKey === currentKey.current &&
        epoch === (previewEpoch?.current ?? 0)
      )
        setResult({ key: requestedKey, data });
    } catch (failure) {
      if (
        id === requestId.current &&
        requestedKey === currentKey.current &&
        epoch === (previewEpoch?.current ?? 0)
      )
        setError({
          key: requestedKey,
          message:
            failure instanceof Error ? failure.message : "Could not preview this combination.",
        });
    } finally {
      if (id === requestId.current) setPending(null);
    }
  }
  const data = result?.key === key ? result.data : null;
  return (
    <details className="border-t border-border pt-3">
      <summary className="cursor-pointer text-sm font-medium">Check a combination</summary>
      <div className="mt-3 space-y-3">
        <div className="grid gap-3 sm:grid-cols-3">
          <label className="grid min-w-0 gap-1 text-sm">
            Preview agent
            {agents.length > COMBOBOX_OPTION_THRESHOLD ? (
              <Combobox
                mode="select"
                options={agents.map((item) => ({
                  key: item.id,
                  label: item.display_name,
                  description: item.name,
                }))}
                value={agent}
                onValueChange={(value) => {
                  setAgent(value);
                  setError(null);
                }}
                placeholder="Choose an agent"
                aria-label="Preview agent"
                className="h-auto w-full rounded-none! bg-transparent p-0"
              />
            ) : (
              <select
                className={CHOICE_SELECT}
                value={agent}
                onChange={(e) => {
                  setAgent(e.target.value);
                  setError(null);
                }}
              >
                <option value="">Choose an agent</option>
                {agents.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.display_name}
                  </option>
                ))}
              </select>
            )}
          </label>
          <label className="grid min-w-0 gap-1 text-sm">
            Preview host
            {hosts.length > COMBOBOX_OPTION_THRESHOLD ? (
              <Combobox
                mode="select"
                options={hosts.map((item) => ({
                  key: item.host_id,
                  label: item.name,
                  description: item.status === "online" ? undefined : "Offline",
                }))}
                value={targetHost}
                onValueChange={(value) => {
                  setTargetHost(value);
                  setError(null);
                }}
                placeholder="Choose a host"
                aria-label="Preview host"
                className="h-auto w-full rounded-none! bg-transparent p-0"
              />
            ) : (
              <select
                className={CHOICE_SELECT}
                value={targetHost}
                onChange={(e) => {
                  setTargetHost(e.target.value);
                  setError(null);
                }}
              >
                <option value="">Choose a host</option>
                {hosts.map((item) => (
                  <option key={item.host_id} value={item.host_id}>
                    {item.name}
                  </option>
                ))}
              </select>
            )}
          </label>
          <label className="grid min-w-0 gap-1 text-sm">
            Preview model
            <Input
              value={model}
              placeholder="Use resolved default"
              onChange={(e) => {
                setModel(e.target.value);
                setError(null);
              }}
            />
          </label>
        </div>
        <Button
          variant="outline"
          size="sm"
          disabled={pending?.key === key || !agent || !targetHost}
          onClick={() => void preview()}
        >
          {pending?.key === key ? "Checking…" : "Check draft"}
        </Button>
        {error?.key === key && (
          <p role="alert" className="break-words text-sm text-destructive">
            {error.message}
          </p>
        )}
        {((result && !data) ||
          (pending && pending.key !== key) ||
          (error && error.key !== key)) && (
          <p role="status" className="text-sm text-muted-foreground">
            Draft, combination or quota release changed. Check again.
          </p>
        )}
        {data && (
          <div className="space-y-2 text-sm">
            <p role="status" className="break-words">
              {data.candidate.model} ·{" "}
              {data.check.state === "passes_checks"
                ? "Passes current checks"
                : data.check.state === "blocked"
                  ? "Blocked"
                  : "Unknown"}
              {data.check.reason ? ` · ${data.check.reason}` : ""}
            </p>
            <p className="text-muted-foreground">
              {data.policyActive
                ? "Selection policy enabled."
                : "Rule explanation only; selection policy is not enabled on this host."}{" "}
              {data.configuredInTiers ? "Listed in tiers." : "Outside tiers."} This check does not
              reserve a session.
            </p>
            {data.savedRevision !== revision && (
              <p className="text-destructive">
                Saved settings changed. Reload before saving this draft.
              </p>
            )}
            <dl className="grid gap-2">
              {Object.entries(data.effectiveLimits).map(([name, field]) => (
                <div key={name} className="rounded border border-border p-2">
                  <dt className="font-medium">{FIELD_LABELS[name] ?? name}</dt>
                  <dd className="break-words">
                    {!field.defined
                      ? "No additional limit"
                      : field.value === null
                        ? "Explicitly unlimited / no threshold"
                        : Array.isArray(field.value)
                          ? field.value.join(", ") || "No exemptions"
                          : String(field.value)}
                  </dd>
                  {field.sourceRuleId && (
                    <dd className="break-words text-muted-foreground">
                      Source: rule{" "}
                      {(document.limitRules ?? []).findIndex(
                        (rule) => rule.id === field.sourceRuleId,
                      ) + 1}{" "}
                      ·{" "}
                      {field.members?.length
                        ? field.members
                            .map(
                              (member) =>
                                `${member.agent} / ${member.host ?? "All hosts"} / ${member.model ?? "All models"}`,
                            )
                            .join(" + ")
                        : `${field.scope?.agent} / ${field.scope?.host ?? "All hosts"} / ${field.scope?.model ?? "All models"}`}
                    </dd>
                  )}
                  {name === "maxRunning" && field.defined && (
                    <dd className="text-muted-foreground">
                      Running: {field.countState === "known" ? field.runningCount : "Unknown"} ·
                      Reserved: {field.reservedCount ?? "Unknown"} · Counted across the source
                      rule’s scope
                    </dd>
                  )}
                  {name === "maxRunning" && Boolean(field.bindingRules?.length) && (
                    <dd className="mt-1 space-y-1 text-xs text-muted-foreground">
                      <span className="font-medium text-foreground">Binding caps:</span>{" "}
                      {field.bindingRules!.map((cap) => (
                        <span key={cap.ruleId} className="block break-words">
                          {cap.ruleId}: {cap.countState === "known" ? cap.runningCount : "Unknown"}/
                          {cap.maxRunning} (
                          {cap.members
                            .map(
                              (member) =>
                                `${member.agent} / ${member.host ?? "All hosts"} / ${member.model ?? "All models"}`,
                            )
                            .join(" + ")}
                          )
                        </span>
                      ))}
                    </dd>
                  )}
                </div>
              ))}
            </dl>
            {(data.legacyChecks.limits || data.legacyChecks.linkMaxRunning != null) && (
              <p className="break-words text-muted-foreground">
                Legacy pool constraints still apply: {data.legacyChecks.pool}. Explicit unlimited
                does not remove them.
                {data.legacyChecks.limits?.maxRunning != null
                  ? ` Pool max: ${data.legacyChecks.limits.maxRunning}.`
                  : ""}
                {data.legacyChecks.linkMaxRunning != null
                  ? ` Link max: ${data.legacyChecks.linkMaxRunning}.`
                  : ""}
              </p>
            )}
            {Boolean(data.lines?.length) && (
              <p className="text-muted-foreground">
                {data.lines
                  ?.map(
                    (line) =>
                      `Quota check: ${line.state} · Warning: ${line.warn_line_pct == null ? "Unknown / unset" : `${line.warn_line_pct}%`} · Stop: ${line.stop_line_pct == null ? "Unknown / unset" : `${line.stop_line_pct}%`}${line.released ? " · Floor released" : ""}`,
                  )
                  .join("; ")}
              </p>
            )}
          </div>
        )}
      </div>
    </details>
  );
}
