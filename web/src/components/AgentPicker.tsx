import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { PickerShell } from "@/components/PickerShell";
import { SelectionList, type PickerItem } from "@/components/SelectionList";
import {
  SECTION_GLOSSES,
  SECTION_LABELS,
  SECTION_ORDER,
  type RegistryItem,
  type RegistryKey,
} from "@/hooks/useAgentRegistry";

export function AgentPicker({
  items,
  taken,
  supports,
  onPick,
  trigger,
  isLoading = false,
  isError = false,
  onRetry,
  title = "Add exception",
  description = "Choose an agent or harness for a different setting.",
}: {
  items: RegistryItem[];
  taken: ReadonlySet<RegistryKey>;
  supports?: (item: RegistryItem) => string | null;
  onPick: (key: RegistryKey) => void;
  trigger: ReactNode;
  isLoading?: boolean;
  isError?: boolean;
  onRetry?: () => void;
  title?: string;
  description?: string;
}) {
  const [open, setOpen] = useState(false);
  const [showUndeployed, setShowUndeployed] = useState(false);
  const [query, setQuery] = useState("");
  const hidden = items.filter(
    (item) =>
      !item.deployed &&
      `${item.label} ${item.harness ?? ""} ${SECTION_LABELS[item.section]}`
        .toLowerCase()
        .includes(query.toLowerCase().trim()),
  );
  const visible = items.filter((item) => showUndeployed || item.deployed);
  const options: PickerItem[] = visible.map((item) => ({
    id: item.key,
    label: item.label,
    description: item.description ?? undefined,
    keywords: item.harness ?? undefined,
    section: SECTION_LABELS[item.section],
    unavailableReason: supports?.(item),
    disabled: taken.has(item.key),
    aside: (
      <>
        {!item.deployed && <span>not deployed </span>}
        {taken.has(item.key) ? "✓ already listed" : SECTION_LABELS[item.section]}
      </>
    ),
  }));
  return (
    <PickerShell
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) {
          setShowUndeployed(false);
          setQuery("");
        }
      }}
      title={title}
      description={description}
      trigger={trigger}
    >
      <SelectionList
        key={String(open)}
        mode="single"
        items={options}
        value={null}
        onChange={(key) => {
          if (key) onPick(key as RegistryKey);
          setOpen(false);
        }}
        searchLabel="Search agents"
        onQueryChange={setQuery}
        searchThreshold={0}
        listLabel="Agent choices"
        rowRole="button"
        unavailableLabel="Not supported here"
        sectionOrder={SECTION_ORDER.map((section) => SECTION_LABELS[section])}
        sectionGlosses={Object.fromEntries(
          SECTION_ORDER.map((section) => [SECTION_LABELS[section], SECTION_GLOSSES[section] ?? ""]),
        )}
        loading={
          isLoading && <p className="px-3 py-3 text-sm text-muted-foreground">Loading agents…</p>
        }
        error={
          isError && (
            <div className="flex items-center justify-between gap-3 px-3 py-3 text-sm">
              <span>Couldn't load agents</span>
              <Button type="button" variant="outline" size="sm" onClick={onRetry}>
                Retry
              </Button>
            </div>
          )
        }
        emptyLabel="No agents match your search."
      />
      {hidden.length > 0 && !showUndeployed && (
        <button
          type="button"
          className="min-h-11 w-full border-t px-4 text-left text-sm text-muted-foreground hover:text-foreground"
          onClick={() => setShowUndeployed(true)}
        >
          Show {hidden.length} not deployed
        </button>
      )}
    </PickerShell>
  );
}
