import type { ProjectConfig } from "@/lib/projectsApi";
import { useSessionChoiceDocument } from "./sessionChoiceData";
import { CHOICE_SELECT } from "./ChoiceLinkEditor";

export type ProjectChoiceOverride = NonNullable<ProjectConfig["orchestration"]>["session_choice"];

export function ProjectSessionChoiceField({
  value,
  onChange,
}: {
  value: ProjectChoiceOverride;
  onChange: (value: ProjectChoiceOverride) => void;
}) {
  const document = useSessionChoiceDocument();
  const presets = document.data?.settings?.presets ?? [];
  const pin = value && typeof value === "object" ? value.pin : null;
  return (
    <label className="flex flex-wrap items-center gap-2 text-sm">
      Session choice
      <select
        aria-label="Project session choice"
        className={CHOICE_SELECT}
        value={pin ? `pin:${pin}` : value === "off" ? "off" : "inherit"}
        onChange={(event) =>
          onChange(
            event.target.value === "inherit"
              ? undefined
              : event.target.value === "off"
                ? "off"
                : { pin: event.target.value.slice(4) },
          )
        }
      >
        <option value="inherit">Use account strategy</option>
        <option value="off">Off for this project</option>
        {pin && !presets.some((p) => p.id === pin) && (
          <option value={`pin:${pin}`}>{pin} (preset unavailable)</option>
        )}
        {presets.map((p) => (
          <option key={p.id} value={`pin:${p.id}`}>
            Pin {p.name}
          </option>
        ))}
      </select>
      {document.isError && (
        <span className="text-xs text-muted-foreground">Presets unavailable</span>
      )}
    </label>
  );
}
