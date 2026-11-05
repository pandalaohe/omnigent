import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { HelpTip } from "@/components/HelpTip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { formatShortDate } from "./usageBoardFormat";
import {
  savePriceSource,
  usePriceSource,
  type PriceFormat,
  type PriceSelection,
} from "./usagePriceSourceData";

export const PRICE_SOURCE_NAMES = {
  builtin: "Built-in catalog",
  litellm: "LiteLLM map",
  openrouter: "OpenRouter",
  modelsdev: "models.dev",
  custom: "Custom URL",
};

export function UsagePriceSourceSettings() {
  const query = usePriceSource();
  const client = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [url, setUrl] = useState("");
  const [format, setFormat] = useState<PriceFormat>("litellm");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const current = query.data;

  async function save(selection: PriceSelection) {
    setSaving(true);
    setMessage("");
    setError("");
    try {
      const saved = await savePriceSource(selection);
      client.setQueryData(["usageBoard", "priceSource"], saved);
      await client.invalidateQueries({ queryKey: ["usageBoard", "models"] });
      setAdding(false);
      setMessage("Saved");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Price source could not be saved");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="border-t border-border py-2.5">
      <div className="flex min-h-9 flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-sm">
          <label htmlFor="usage-price-source">Price source</label>
          <HelpTip label="How price changes apply">
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 text-xs">
              <dt>New usage</dt>
              <dd>Uses the selected source.</dd>
              <dt>Priced history</dt>
              <dd>Stays frozen.</dd>
              <dt>Unpriced history</dt>
              <dd>Filled once a price is available, then frozen.</dd>
            </dl>
          </HelpTip>
        </div>
        {current?.can_edit ? (
          <div className="flex items-center gap-1.5">
            <select
              id="usage-price-source"
              className="h-9 max-w-[210px] rounded-md border border-input bg-background px-2 text-sm"
              disabled={saving}
              value={current.source}
              onChange={(event) => {
                void save({ source: event.target.value as PriceSelection["source"] });
              }}
            >
              {Object.entries(PRICE_SOURCE_NAMES)
                .filter(([key]) => key !== "custom" || current.source === "custom")
                .map(([key, name]) => (
                  <option key={key} value={key}>
                    {name}
                  </option>
                ))}
            </select>
            <Button
              variant="outline"
              size="sm"
              aria-label="Add price source URL"
              disabled={saving}
              onClick={() => {
                setUrl(current.url ?? "");
                setFormat(current.format ?? "litellm");
                setError("");
                setAdding(true);
              }}
            >
              +
            </Button>
          </div>
        ) : (
          <span className="text-sm text-muted-foreground">
            {current ? PRICE_SOURCE_NAMES[current.source] : "Reading…"}
          </span>
        )}
      </div>
      {current?.source === "custom" ? (
        <p className="mt-1 break-all text-xs text-muted-foreground">{current.url}</p>
      ) : null}
      {current?.fetched_at ? (
        <p className="mt-1 text-xs text-muted-foreground">
          {current.model_count.toLocaleString()} models · {formatShortDate(current.fetched_at)}
        </p>
      ) : null}
      {message ? (
        <p role="status" className="mt-1 text-xs text-success">
          {message}
        </p>
      ) : null}
      {saving ? (
        <p role="status" className="mt-1 text-xs text-muted-foreground">
          Reading prices…
        </p>
      ) : null}
      {(error && !adding) || current?.error || query.error ? (
        <p role="alert" className="mt-1 text-xs text-destructive">
          {error || current?.error || query.error?.message}
        </p>
      ) : null}
      <Dialog
        open={adding}
        onOpenChange={(open) => {
          if (!saving) setAdding(open);
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Add price source</DialogTitle>
            <DialogDescription>
              Choose a public price map. A valid first reading is required.
            </DialogDescription>
          </DialogHeader>
          <form
            className="grid gap-3"
            onSubmit={(event) => {
              event.preventDefault();
              void save({ source: "custom", format, url });
            }}
          >
            <label className="grid gap-1.5 text-sm">
              Format
              <select
                aria-label="Price map format"
                className="h-9 rounded-md border border-input bg-background px-2"
                value={format}
                onChange={(event) => setFormat(event.target.value as PriceFormat)}
                disabled={saving}
              >
                {(["litellm", "openrouter", "modelsdev"] as const).map((key) => (
                  <option key={key} value={key}>
                    {PRICE_SOURCE_NAMES[key]}
                  </option>
                ))}
              </select>
            </label>
            <label className="grid gap-1.5 text-sm">
              HTTPS URL
              <Input
                aria-label="Price map HTTPS URL"
                type="url"
                required
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                disabled={saving}
                placeholder="https://example.com/prices.json"
              />
            </label>
            {error ? (
              <p role="alert" className="text-xs text-destructive">
                {error}
              </p>
            ) : null}
            <Button type="submit" disabled={saving}>
              {saving ? "Reading prices…" : "Add source"}
            </Button>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
