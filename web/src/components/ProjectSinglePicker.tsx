import { useState } from "react";
import { PickerShell } from "@/components/PickerShell";
import { SelectionList } from "@/components/SelectionList";
import { Button } from "@/components/ui/button";

export function ProjectSinglePicker({
  projects,
  value,
  onChange,
  label = "Project",
  noneLabel = "No project",
  disabled = false,
  testId,
}: {
  projects: readonly { id: string; name: string; archived_at?: number | null }[];
  value: string | null;
  onChange: (id: string | null) => void;
  label?: string;
  noneLabel?: string;
  disabled?: boolean;
  testId?: string;
}) {
  const [open, setOpen] = useState(false);
  const selected = projects.find((project) => project.id === value);
  return (
    <PickerShell
      title={`Choose ${label.toLowerCase()}`}
      open={open}
      onOpenChange={setOpen}
      trigger={
        <Button
          type="button"
          variant="outline"
          disabled={disabled}
          data-testid={testId}
          aria-label={label}
          className="w-full min-w-0 justify-between font-normal"
        >
          <span className="truncate">
            {selected ? `${selected.name}${selected.archived_at ? " (archived)" : ""}` : noneLabel}
          </span>
          <span aria-hidden="true">▾</span>
        </Button>
      }
    >
      <SelectionList
        mode="single"
        items={projects
          .filter((project) => !project.archived_at)
          .map((project) => ({ id: project.id, label: project.name }))}
        value={value}
        onChange={(id) => {
          onChange(id);
          setOpen(false);
        }}
        noneLabel={noneLabel}
        searchLabel="Search projects"
      />
    </PickerShell>
  );
}
