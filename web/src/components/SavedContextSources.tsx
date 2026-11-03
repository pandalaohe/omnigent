import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { SavedSourcesManager } from "@/components/SavedSourcesManager";
import type { Host } from "@/hooks/useHosts";
import type { RegistryItem } from "@/hooks/useAgentRegistry";
import type { UsageContextPreferences, UsageContextSource } from "@/lib/usageContextPreferences";

const NO_REGISTRY_ITEMS: readonly RegistryItem[] = [];

/** Every saved Context value in one list, for deleting and merging what the table cannot reach. */
export function SavedContextSources({
  preferences,
  currentSourceKey,
  hosts,
  registryItems = NO_REGISTRY_ITEMS,
  initialSource,
  reportedContextWindow = null,
  reportedAutoCompactTokenLimit = null,
}: {
  preferences: UsageContextPreferences;
  currentSourceKey: string;
  hosts: Host[];
  registryItems?: readonly RegistryItem[];
  initialSource: UsageContextSource | null;
  reportedContextWindow?: number | null;
  reportedAutoCompactTokenLimit?: number | null;
}) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <Button
        size="sm"
        variant="outline"
        className="min-h-11 md:min-h-7"
        disabled={initialSource === null}
        onClick={() => setOpen(true)}
      >
        List all saved values
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          data-pop=""
          className="max-h-[90vh] overflow-y-auto sm:max-w-4xl"
          onEscapeKeyDown={(event) => {
            if (
              event.target instanceof Element &&
              event.target.closest("[data-usage-context-editor]")
            )
              event.preventDefault();
          }}
        >
          <DialogHeader>
            <DialogTitle>All saved values</DialogTitle>
            <DialogDescription>
              One model on this computer beats one model everywhere, then the cell, then the agent,
              then the computer, then all agents. Empty fields inherit.
            </DialogDescription>
          </DialogHeader>
          {initialSource ? (
            <SavedSourcesManager
              preferences={preferences}
              currentSourceKey={currentSourceKey}
              hosts={hosts}
              registryItems={registryItems}
              initialSource={initialSource}
              reportedContextWindow={reportedContextWindow}
              reportedAutoCompactTokenLimit={reportedAutoCompactTokenLimit}
            />
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
