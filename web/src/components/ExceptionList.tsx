import { useState, type ReactNode } from "react";

import { AgentPicker } from "@/components/AgentPicker";
import { Button } from "@/components/ui/button";
import { SECTION_LABELS, type RegistryItem, type RegistryKey } from "@/hooks/useAgentRegistry";

const EMPTY_LABELS: Record<string, string> = {};

export function ExceptionList<T>({
  rows,
  items,
  legacyLabels = EMPTY_LABELS,
  summary,
  editor,
  onAdd,
  onPatch,
  onRemove,
  supports,
  isLoading,
  isError,
  onRetry,
}: {
  rows: Record<RegistryKey, T>;
  items: RegistryItem[];
  legacyLabels?: Record<string, string>;
  summary: (row: T) => string;
  editor: (row: T, patch: (next: Partial<T>) => void, key: RegistryKey) => ReactNode;
  onAdd: (key: RegistryKey) => void;
  onPatch: (key: RegistryKey, next: Partial<T>) => void;
  onRemove: (key: RegistryKey) => void;
  supports?: (item: RegistryItem) => string | null;
  isLoading?: boolean;
  isError?: boolean;
  onRetry?: () => void;
}) {
  const [openKey, setOpenKey] = useState<string | null>(null);
  const byKey = new Map(items.map((item) => [item.key, item]));
  return (
    <div className="space-y-2" data-testid="exception-list">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-medium">Exceptions</span>
        <AgentPicker
          items={items}
          taken={new Set(Object.keys(rows))}
          supports={supports}
          onPick={(key) => {
            onAdd(key);
            setOpenKey(key);
          }}
          trigger={
            <Button type="button" size="sm" variant="outline">
              Add exception
            </Button>
          }
          isLoading={isLoading}
          isError={isError}
          onRetry={onRetry}
        />
      </div>
      {Object.keys(rows).length === 0 && (
        <p className="text-xs text-muted-foreground">All agents use the default.</p>
      )}
      {Object.entries(rows).map(([key, value]) => {
        const item = byKey.get(key);
        const isLegacyProduct = Object.hasOwn(legacyLabels, key);
        const label = isLegacyProduct
          ? `${legacyLabels[key]} · direct sessions only`
          : (item?.label ?? key);
        return (
          <div key={key} className="rounded-lg border border-border px-3 py-2">
            <div className="flex min-w-0 flex-wrap items-center justify-between gap-2 text-sm">
              <div className="min-w-0">
                <span className="font-medium break-all">{label}</span>
                <span className="ml-2 text-xs text-muted-foreground">
                  {item ? SECTION_LABELS[item.section] : "Saved agent"}
                </span>
                {item && !item.deployed && (
                  <span className="ml-2 text-xs text-muted-foreground">not deployed</span>
                )}
                <span className="ml-2 text-xs text-muted-foreground">{summary(value)}</span>
              </div>
              <div className="flex shrink-0 gap-1">
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  aria-label={`Edit ${label}`}
                  onClick={() => setOpenKey(openKey === key ? null : key)}
                >
                  {openKey === key ? "Done" : "Edit"}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove ${label}`}
                  onClick={() => {
                    onRemove(key);
                    if (openKey === key) setOpenKey(null);
                  }}
                >
                  Remove
                </Button>
              </div>
            </div>
            {openKey === key && (
              <div className="mt-3 border-t border-border pt-3">
                {editor(value, (next) => onPatch(key, next), key)}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
