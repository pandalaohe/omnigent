import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { choiceRequest, type ChoiceConversionPlan } from "./sessionChoiceData";

export function ChoiceConversion({
  host,
  revision,
  dirty,
  onApply,
}: {
  host: string;
  revision: string;
  dirty: boolean;
  onApply: (plan: ChoiceConversionPlan) => void;
}) {
  const agents = useAvailableAgents();
  const [plan, setPlan] = useState<ChoiceConversionPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const current = plan?.sourceRevision === revision && !dirty;
  return (
    <div className="space-y-2 border-t border-border pt-3">
      <Button
        size="sm"
        variant="outline"
        disabled={!host || dirty || busy}
        onClick={async () => {
          setBusy(true);
          setError("");
          setPlan(null);
          try {
            const result = await choiceRequest<{ legacyConversion: ChoiceConversionPlan }>(
              "/preview",
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ host_id: host, conversion: true }),
              },
            );
            setPlan(result.legacyConversion);
          } catch (failure) {
            setError(failure instanceof Error ? failure.message : "Could not preview conversion.");
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? "Checking conversion…" : "Preview legacy conversion"}
      </Button>
      {dirty && (
        <p className="text-xs text-muted-foreground">
          Save or discard your edits before previewing conversion.
        </p>
      )}
      {plan && (
        <div className="space-y-2 text-sm">
          <p>
            {plan.newRules.length} equivalent rules. {plan.unmapped.length} legacy constraints
            retained.
          </p>
          {plan.newRules.map((rule) => (
            <p key={rule.id}>
              {agents.data?.find((agent) => agent.id === rule.agent)?.display_name ?? rule.agent} /{" "}
              {rule.model ?? "All models"} / All hosts · Max running:{" "}
              {rule.maxRunning ?? "Unlimited"} · Warning:{" "}
              {rule.reserve?.pct == null ? "No threshold" : `${rule.reserve.pct}%`} · Stop:{" "}
              {rule.reserve?.stopPct == null ? "No threshold" : `${rule.reserve.stopPct}%`}
            </p>
          ))}
          {plan.unmapped.map((row) => (
            <p key={JSON.stringify(row.path)} className="break-words text-muted-foreground">
              Retained {row.path.join(" / ")}: {row.reason.replaceAll("_", " ")}
            </p>
          ))}
          <Button
            size="sm"
            disabled={!current || !plan.convertible || !plan.settings}
            onClick={() => onApply(plan)}
          >
            Apply conversion to draft
          </Button>
          {!current && <p>Preview expired. Preview again after saving your edits.</p>}
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
