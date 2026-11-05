import { useQuery } from "@tanstack/react-query";
import { useParams } from "@/lib/routing";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { patchUsageBoardPreferences } from "@/lib/usageContextPreferences";
import { Button } from "@/components/ui/button";
import { UsageBoardFrame } from "./UsageBoardFrame";
import { UsageRangeControl, usageRangeQuery } from "./UsageRangeControl";
import { usageJson } from "./usagePriceSourceData";
import { formatMoney, formatShortDate } from "./usageBoardFormat";

interface ModelSource {
  kind: "host" | "report";
  label: string;
  stale?: boolean;
  configuration?: { label?: string; name?: string; host?: string } | null;
}
export interface UsageModelsResponse {
  account: string;
  display_name: string;
  range: string;
  span: { counted_from: number; until: number } | null;
  models: {
    id: string;
    sources: ModelSource[];
    pricing: { input_per_token: number; output_per_token: number } | null;
    usage: { cost_usd: number | null; tokens: number | null; unpriced: number } | null;
  }[];
}

export function UsageModelsPage() {
  const { accountId = "" } = useParams();
  const board = useUsageContextPreferences().board;
  const params = usageRangeQuery(board.range ?? { key: "d7" });
  params.set("account", accountId);
  const query = useQuery({
    queryKey: ["usageBoard", "models", params.toString()],
    queryFn: () => usageJson<UsageModelsResponse>(`models?${params}`),
    retry: false,
  });
  const tokens = board.unit === "tokens";
  return (
    <UsageBoardFrame
      title={`${query.data?.display_name ?? "Account"} · Models`}
      back={{
        to: `/usage/board/${encodeURIComponent(accountId)}/settings`,
        label: "Account settings",
      }}
      loading={query.isPending}
      error={query.error}
      actions={<UsageRangeControl />}
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          {query.data?.span
            ? `${formatShortDate(query.data.span.counted_from)} – ${formatShortDate(query.data.span.until)}`
            : "No usage in this range"}
        </p>
        <div className="flex gap-1" role="group" aria-label="Usage measure">
          {(["money", "tokens"] as const).map((unit) => (
            <Button
              key={unit}
              size="sm"
              variant={
                board.unit === unit || (!board.unit && unit === "money") ? "secondary" : "ghost"
              }
              aria-pressed={tokens === (unit === "tokens")}
              onClick={() => patchUsageBoardPreferences({ unit })}
            >
              {unit === "money" ? "Money" : "Tokens"}
            </Button>
          ))}
        </div>
      </div>
      {query.data?.models.length === 0 ? (
        <p className="py-8 text-sm text-muted-foreground">
          No model has been reported or used for this account yet.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-[11px] border border-border bg-card">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">
              Available models, their sources, list prices per million tokens and use in the
              selected range
            </caption>
            <thead className="border-b border-border text-xs text-muted-foreground">
              <tr>
                <th scope="col" className="p-3 font-medium">
                  Model / source
                </th>
                <th scope="col" className="p-3 text-right font-medium">
                  List price / 1M
                  <br />
                  <span className="font-normal">Input · Output</span>
                </th>
                <th scope="col" className="p-3 text-right font-medium">
                  Use in range
                </th>
              </tr>
            </thead>
            <tbody>
              {query.data?.models.map((model) => (
                <tr key={model.id} className="border-b border-border last:border-0">
                  <th scope="row" className="max-w-[360px] p-3 font-normal">
                    <span className="break-all font-medium">{model.id}</span>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {model.sources.map((source, index) => (
                        <span
                          key={index}
                          className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground"
                        >
                          {source.label}
                          {source.stale ? " · old" : ""}
                          {source.configuration?.label ? ` · ${source.configuration.label}` : ""}
                          {source.configuration?.name ? `: ${source.configuration.name}` : ""}
                        </span>
                      ))}
                    </div>
                  </th>
                  <td className="whitespace-nowrap p-3 text-right tabular-nums">
                    {model.pricing ? (
                      `${formatMoney(model.pricing.input_per_token * 1e6)} · ${formatMoney(model.pricing.output_per_token * 1e6)}`
                    ) : (
                      <span className="text-muted-foreground">Unpriced</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap p-3 text-right tabular-nums">
                    {tokens ? (
                      model.usage?.tokens == null ? (
                        "—"
                      ) : (
                        Intl.NumberFormat("en-US", {
                          notation: "compact",
                          maximumFractionDigits: 1,
                        }).format(model.usage.tokens)
                      )
                    ) : (
                      <>
                        {model.usage?.unpriced ? "≥ " : ""}
                        {formatMoney(model.usage?.cost_usd)}
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </UsageBoardFrame>
  );
}
