import { PlusIcon, XIcon } from "lucide-react";
import { useState } from "react";
import { HelpTip } from "@/components/HelpTip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useSessionCollabPreferences } from "@/hooks/useSessionCollabPreferences";
import type { ProjectConfig } from "@/lib/projectsApi";
import {
  ProjectSessionChoiceField,
  type ProjectChoiceOverride,
} from "@/pages/settings/sessionChoice/ProjectSessionChoiceField";
import { useSessionChoiceDocument } from "@/pages/settings/sessionChoice/sessionChoiceData";

export interface ProjectOrchestrationDraft {
  child_cap?: number | "none";
  session_choice?: ProjectChoiceOverride;
  standing_approval?: { remove?: string[]; exceptions?: string[] };
}

const KNOWN_APPROVAL_KEYS = ["install", "merge", "release", "answer-child-approvals"] as const;
const MAX_REMOVED_KINDS = 32;
const MAX_EXCEPTIONS = 5;
const MAX_EXCEPTION_LEN = 120;

export function seedOrchestrationDraft(
  orchestration: ProjectConfig["orchestration"],
): ProjectOrchestrationDraft | undefined {
  if (!orchestration) return undefined;
  const standing = orchestration.standing_approval ?? orchestration.standing_pass;
  const remove = standing?.remove?.filter((item) => item.trim() !== "");
  const exceptions = standing?.exceptions?.filter((item) => item.trim() !== "");
  return normalizeOrchestrationDraft({
    child_cap: orchestration.child_cap,
    session_choice: orchestration.session_choice,
    standing_approval:
      remove?.length || exceptions?.length
        ? {
            ...(remove?.length ? { remove } : {}),
            ...(exceptions?.length ? { exceptions } : {}),
          }
        : undefined,
  });
}

export function normalizeOrchestrationDraft(
  draft: ProjectOrchestrationDraft | undefined,
): ProjectOrchestrationDraft | undefined {
  if (!draft) return undefined;
  const next: ProjectOrchestrationDraft = {};
  if (
    draft.child_cap === "none" ||
    (typeof draft.child_cap === "number" &&
      Number.isInteger(draft.child_cap) &&
      draft.child_cap > 0)
  ) {
    next.child_cap = draft.child_cap;
  }
  if (draft.session_choice !== undefined) {
    next.session_choice = draft.session_choice;
  }
  const remove = draft.standing_approval?.remove?.filter((item) => item.trim() !== "") ?? [];
  const exceptions =
    draft.standing_approval?.exceptions?.filter((item) => item.trim() !== "") ?? [];
  if (remove.length > 0 || exceptions.length > 0) {
    next.standing_approval = {
      ...(remove.length > 0 ? { remove } : {}),
      ...(exceptions.length > 0 ? { exceptions } : {}),
    };
  }
  return Object.keys(next).length > 0 ? next : undefined;
}

export function isOrchestrationOverridden(draft: ProjectOrchestrationDraft | undefined): boolean {
  return normalizeOrchestrationDraft(draft) !== undefined;
}

function ApprovalKindLabel({ kind }: { kind: string }) {
  if (kind === "install") return <>Install</>;
  if (kind === "merge") return <>Merge</>;
  if (kind === "release") return <>Release</>;
  if (kind === "answer-child-approvals") return <>Answer child approvals</>;
  return kind;
}

export function ProjectOrchestrationView({
  value,
  onChange,
  disabled = false,
}: {
  value: ProjectOrchestrationDraft | undefined;
  onChange: (next: ProjectOrchestrationDraft | undefined) => void;
  disabled?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [customCapText, setCustomCapText] = useState(() =>
    typeof value?.child_cap === "number" ? String(value.child_cap) : "",
  );
  const [exceptionInput, setExceptionInput] = useState("");
  const [exceptionError, setExceptionError] = useState<"length" | "limit" | null>(null);
  const collabPreferences = useSessionCollabPreferences();
  const choiceDoc = useSessionChoiceDocument();

  const normalized = normalizeOrchestrationDraft(value);
  const overridden = normalized !== undefined;
  const removedKinds = normalized?.standing_approval?.remove ?? [];
  const exceptions = normalized?.standing_approval?.exceptions ?? [];
  const presets = choiceDoc.data?.settings?.presets ?? [];

  const capMode: "inherit" | "none" | "custom" =
    normalized?.child_cap === "none"
      ? "none"
      : typeof normalized?.child_cap === "number"
        ? "custom"
        : "inherit";

  const availableKinds = [
    ...KNOWN_APPROVAL_KEYS,
    ...collabPreferences.standingPassDefault.filter(
      (kind) => !KNOWN_APPROVAL_KEYS.includes(kind as (typeof KNOWN_APPROVAL_KEYS)[number]),
    ),
    ...removedKinds.filter(
      (kind) =>
        !KNOWN_APPROVAL_KEYS.includes(kind as (typeof KNOWN_APPROVAL_KEYS)[number]) &&
        !collabPreferences.standingPassDefault.includes(kind),
    ),
  ];

  const updateDraft = (patch: Partial<ProjectOrchestrationDraft>) => {
    onChange(normalizeOrchestrationDraft({ ...(normalized ?? {}), ...patch }));
  };

  const setCapMode = (mode: "inherit" | "none" | "custom") => {
    if (mode === "inherit") {
      setCustomCapText("");
      updateDraft({ child_cap: undefined });
      return;
    }
    if (mode === "none") {
      setCustomCapText("");
      updateDraft({ child_cap: "none" });
      return;
    }
    const existing = typeof normalized?.child_cap === "number" ? normalized.child_cap : 4;
    setCustomCapText(String(existing));
    updateDraft({ child_cap: existing });
  };

  const toggleRemovedKind = (kind: string) => {
    const exists = removedKinds.includes(kind);
    if (!exists && removedKinds.length >= MAX_REMOVED_KINDS) return;
    const nextRemove = exists
      ? removedKinds.filter((item) => item !== kind)
      : [...removedKinds, kind];
    updateDraft({
      standing_approval: {
        ...(normalized?.standing_approval ?? {}),
        remove: nextRemove,
      },
    });
  };

  const addException = () => {
    const trimmed = exceptionInput.trim();
    if (!trimmed || [...trimmed].length > MAX_EXCEPTION_LEN) {
      setExceptionError("length");
      return;
    }
    if (exceptions.length >= MAX_EXCEPTIONS) {
      setExceptionError("limit");
      return;
    }
    if (exceptions.includes(trimmed)) {
      setExceptionInput("");
      setExceptionError(null);
      return;
    }
    setExceptionInput("");
    setExceptionError(null);
    updateDraft({
      standing_approval: {
        ...(normalized?.standing_approval ?? {}),
        exceptions: [...exceptions, trimmed],
      },
    });
  };

  const removeException = (index: number) => {
    updateDraft({
      standing_approval: {
        ...(normalized?.standing_approval ?? {}),
        exceptions: exceptions.filter((_, i) => i !== index),
      },
    });
  };

  const pinId =
    normalized?.session_choice && typeof normalized.session_choice === "object"
      ? normalized.session_choice.pin
      : null;
  const pinLabel = pinId ? (presets.find((preset) => preset.id === pinId)?.name ?? pinId) : null;

  return (
    <section
      aria-labelledby="project-orchestration-heading"
      className="space-y-3 border-t border-border pt-4"
      data-testid="project-orchestration-section"
    >
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h3 id="project-orchestration-heading" className="text-ui font-medium">
            Session orchestration
          </h3>
          {!overridden ? (
            <span
              className="text-sm text-muted-foreground"
              data-testid="project-orchestration-summary-default"
            >
              Use account settings
            </span>
          ) : (
            <div
              className="flex min-w-0 flex-wrap items-center gap-1.5"
              data-testid="project-orchestration-summary-chips"
            >
              {normalized.child_cap === "none" && (
                <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                  Child limit: Unlimited
                </span>
              )}
              {typeof normalized.child_cap === "number" && (
                <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                  Child limit: {normalized.child_cap}
                </span>
              )}
              {normalized.session_choice === "off" && (
                <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                  Session choice: Off
                </span>
              )}
              {pinLabel !== null && (
                <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                  Session choice: {pinLabel}
                </span>
              )}
              {removedKinds.length > 0 && (
                <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                  Removed approvals: {removedKinds.length}
                </span>
              )}
              {exceptions.length > 0 && (
                <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                  Approval exceptions: {exceptions.length}
                </span>
              )}
            </div>
          )}
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled}
          onClick={() => {
            if (!editing && typeof normalized?.child_cap === "number") {
              setCustomCapText(String(normalized.child_cap));
            }
            setEditing((open) => !open);
          }}
          data-testid="project-orchestration-toggle"
        >
          {editing ? "Collapse" : overridden ? "Edit" : "Customize"}
        </Button>
      </div>

      {editing && (
        <div className="space-y-4 rounded-md border p-3" data-testid="project-orchestration-editor">
          <div className="space-y-2">
            <div className="text-ui font-medium">Child session limit</div>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                size="sm"
                variant={capMode === "inherit" ? "secondary" : "outline"}
                aria-pressed={capMode === "inherit"}
                disabled={disabled}
                onClick={() => setCapMode("inherit")}
                data-testid="project-orchestration-cap-inherit"
              >
                Use account settings
              </Button>
              <Button
                type="button"
                size="sm"
                variant={capMode === "none" ? "secondary" : "outline"}
                aria-pressed={capMode === "none"}
                disabled={disabled}
                onClick={() => setCapMode("none")}
                data-testid="project-orchestration-cap-none"
              >
                Unlimited
              </Button>
              <Button
                type="button"
                size="sm"
                variant={capMode === "custom" ? "secondary" : "outline"}
                aria-pressed={capMode === "custom"}
                disabled={disabled}
                onClick={() => setCapMode("custom")}
                data-testid="project-orchestration-cap-custom"
              >
                Custom
              </Button>
              {capMode === "custom" && (
                <Input
                  type="number"
                  min={1}
                  step={1}
                  inputMode="numeric"
                  aria-label="Custom child session limit"
                  data-testid="project-orchestration-cap-input"
                  className="h-8 w-24"
                  disabled={disabled}
                  value={customCapText}
                  onChange={(event) => {
                    const raw = event.target.value;
                    setCustomCapText(raw);
                    const parsed = Number.parseInt(raw, 10);
                    if (Number.isInteger(parsed) && parsed > 0 && String(parsed) === raw.trim()) {
                      updateDraft({ child_cap: parsed });
                    }
                  }}
                />
              )}
            </div>
          </div>

          <div className="space-y-1.5">
            <ProjectSessionChoiceField
              value={normalized?.session_choice}
              onChange={(nextChoice) => updateDraft({ session_choice: nextChoice })}
              disabled={disabled}
            />
          </div>

          <div className="space-y-2">
            <div className="flex items-center gap-1.5 text-ui font-medium">
              <span>Removed approval kinds</span>
              <HelpTip label="About removed approval kinds">
                Applies only to new parent sessions opened after saving.
              </HelpTip>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {availableKinds.map((kind) => {
                const selected = removedKinds.includes(kind);
                return (
                  <Button
                    key={kind}
                    type="button"
                    size="sm"
                    variant={selected ? "secondary" : "outline"}
                    aria-pressed={selected}
                    disabled={disabled || (!selected && removedKinds.length >= MAX_REMOVED_KINDS)}
                    onClick={() => toggleRemovedKind(kind)}
                    data-testid={`project-orchestration-remove-kind-${kind}`}
                  >
                    <ApprovalKindLabel kind={kind} />
                  </Button>
                );
              })}
            </div>
          </div>

          <div className="space-y-2">
            <div className="flex items-center gap-1.5 text-ui font-medium">
              <span>Approval exceptions</span>
              <HelpTip label="About approval exceptions">
                Applies only to new parent sessions opened after saving.
              </HelpTip>
            </div>
            {exceptions.length > 0 && (
              <div className="space-y-1.5">
                {exceptions.map((item, index) => (
                  <div
                    key={item}
                    className="flex min-w-0 items-center justify-between gap-2 rounded-md bg-muted px-2.5 py-1.5 text-sm"
                    data-testid={`project-orchestration-exception-${index}`}
                  >
                    <span className="min-w-0 break-words">{item}</span>
                    <button
                      type="button"
                      className="inline-flex size-5 shrink-0 items-center justify-center rounded text-muted-foreground hover:text-foreground disabled:opacity-50"
                      aria-label="Remove approval exception"
                      disabled={disabled}
                      onClick={() => removeException(index)}
                      data-testid={`project-orchestration-exception-remove-${index}`}
                    >
                      <XIcon className="size-3.5" aria-hidden="true" />
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div className="flex min-w-0 items-center gap-2">
              <Input
                value={exceptionInput}
                maxLength={MAX_EXCEPTION_LEN}
                disabled={disabled || exceptions.length >= MAX_EXCEPTIONS}
                aria-label="Approval exception"
                placeholder="Add an exception (1–120 characters)"
                data-testid="project-orchestration-exception-input"
                onChange={(event) => {
                  setExceptionInput(event.target.value);
                  setExceptionError(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    addException();
                  }
                }}
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={disabled || exceptions.length >= MAX_EXCEPTIONS}
                onClick={addException}
                data-testid="project-orchestration-exception-add"
              >
                <PlusIcon className="size-3.5" aria-hidden="true" />
                Add
              </Button>
            </div>
            {exceptionError && (
              <p role="alert" className="text-xs text-destructive">
                {exceptionError === "length"
                  ? "Enter an exception with 1–120 characters."
                  : "At most 5 exceptions are allowed."}
              </p>
            )}
          </div>

          {overridden && (
            <div className="flex justify-end border-t pt-2">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={disabled}
                onClick={() => {
                  setCustomCapText("");
                  setExceptionInput("");
                  setExceptionError(null);
                  onChange(undefined);
                }}
                data-testid="project-orchestration-restore-all"
              >
                Restore all to account settings
              </Button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
