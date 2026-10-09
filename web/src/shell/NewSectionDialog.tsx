// Create a sidebar section: choose a kind, then a name (and the row count for
// a recent section). The new section is inserted at the very top; the user can
// move it from its header menu.

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { SaveSidebarLayout } from "@/hooks/useSidebarLayout";
import {
  insertSection,
  kindsAvailableToCreate,
  newSectionId,
  type SectionKind,
  type SidebarLayout,
  type SidebarSectionDef,
} from "@/lib/sidebarLayout";

const KIND_LABELS: Record<SectionKind, string> = {
  projects: "Project section",
  favorites: "Favorites",
  recent: "Recent sessions",
  other_projects: "Projects (no section)",
  other_sessions: "Sessions (no project)",
};

const KIND_DEFAULT_NAMES: Record<SectionKind, string> = {
  projects: "New section",
  favorites: "Favorites",
  recent: "Recent",
  other_projects: "Projects",
  other_sessions: "Sessions",
};

const RECENT_COUNT_OPTIONS = Array.from({ length: 20 }, (_, index) => index + 1);

function sectionDefFor(kind: SectionKind, name: string, count: number): SidebarSectionDef {
  const def: SidebarSectionDef = {
    id: newSectionId(),
    kind,
    name,
    // System sections keep today's uncapped look; user sections get a cap.
    maxRows: kind === "other_projects" || kind === "other_sessions" ? null : 10,
  };
  if (kind === "projects") def.projectIds = [];
  if (kind === "favorites") def.items = [];
  if (kind === "recent") def.count = count;
  return def;
}

export function NewSectionDialog({
  open,
  onOpenChange,
  layout,
  saveLayout,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  layout: SidebarLayout;
  saveLayout: SaveSidebarLayout;
}) {
  const initialKind = kindsAvailableToCreate(layout)[0] ?? "projects";
  const [step, setStep] = useState<1 | 2>(1);
  const [kind, setKind] = useState<SectionKind>(initialKind);
  const [name, setName] = useState(KIND_DEFAULT_NAMES[initialKind]);
  const [count, setCount] = useState(5);

  const create = () => {
    const trimmed = name.trim();
    saveLayout((current) =>
      insertSection(
        current,
        sectionDefFor(kind, trimmed === "" ? KIND_DEFAULT_NAMES[kind] : trimmed, count),
        0,
      ),
    );
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="new-section-dialog">
        <DialogHeader>
          <DialogTitle>New section</DialogTitle>
        </DialogHeader>
        {step === 1 ? (
          <fieldset className="flex flex-col gap-1">
            <legend className="sr-only">Section kind</legend>
            {kindsAvailableToCreate(layout).map((option) => (
              <label
                key={option}
                className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-ui hover:bg-muted"
              >
                <input
                  type="radio"
                  name="new-section-kind"
                  value={option}
                  checked={kind === option}
                  data-testid={`new-section-kind-${option}`}
                  onChange={() => setKind(option)}
                />
                {KIND_LABELS[option]}
              </label>
            ))}
          </fieldset>
        ) : (
          <div className="flex flex-col gap-3">
            <label className="flex flex-col gap-1 text-ui">
              Name
              <Input
                data-testid="new-section-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                autoFocus
              />
            </label>
            {kind === "recent" && (
              <label className="flex flex-col gap-1 text-ui">
                Sessions shown
                <select
                  data-testid="new-section-count"
                  className="h-8 rounded-lg border border-input bg-transparent px-2 text-ui"
                  value={count}
                  onChange={(event) => setCount(Number(event.target.value))}
                >
                  {RECENT_COUNT_OPTIONS.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
        )}
        <DialogFooter className="border-t-0 bg-transparent">
          {step === 1 ? (
            <>
              <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button
                type="button"
                data-testid="new-section-continue"
                onClick={() => {
                  setName(KIND_DEFAULT_NAMES[kind]);
                  setStep(2);
                }}
              >
                Continue
              </Button>
            </>
          ) : (
            <>
              <Button type="button" variant="ghost" onClick={() => setStep(1)}>
                Back
              </Button>
              <Button type="button" data-testid="new-section-create" onClick={create}>
                Create
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
