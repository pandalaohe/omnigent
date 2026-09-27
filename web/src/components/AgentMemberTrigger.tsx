import { useEffect, useMemo, useRef, useState } from "react";

import { ComposerAgentIcon } from "@/components/composer/ComposerAgentIcon";
import { ComposerConfigSections } from "@/components/composer/ComposerConfigSections";
import { HarnessPicker, HarnessPickerConfigRow } from "@/components/composer/HarnessPicker";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { useHostModelOptions, type Host } from "@/hooks/useHosts";
import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import { normalizeEffortLabel } from "@/lib/composerModelLabel";
import { effortLevelsFor, reconcileEffortOnModelChange } from "@/lib/modelEffortOptions";

/** Menu key for the "Default" (null) row in the Model and Effort submenus. */
const DEFAULT_ROW_KEY = "__default__";

/** The host / harness / model / effort quadruple a member trigger reports. */
export interface AgentMemberValue {
  harness: string;
  model: string | null;
  effort: string | null;
  /** The member's saved host; null = the session's selected host. */
  hostId: string | null;
}

type Section = "host" | "harness" | "model" | "effort";

/**
 * Compact per-member runtime trigger: harness icon, model, and effort in one
 * button whose menu holds Host › / Harness › / Model › / Effort › submenus,
 * the rows the New Chat flyout and the composer configure sessions with. The
 * Agent editor mounts one per member; the create dialog mounts it for the lead
 * (without the Host section: an unsaved bundle has nowhere to keep a host).
 */
export function AgentMemberTrigger({
  harness,
  model,
  effort,
  harnessOptions,
  hostId,
  sessionHostId,
  hosts,
  onChange,
  disabled = false,
}: {
  harness: string;
  model: string | null;
  effort: string | null;
  /** `{id, label}` rows for the Harness submenu, in menu order. */
  harnessOptions: readonly { id: string; label: string }[];
  /** The member's saved host; null = the session host (the catalog fallback). */
  hostId: string | null;
  /** The New Chat host, used as the catalog source when no member host is set. */
  sessionHostId: string | null;
  /** The user's hosts; when provided the Host › section lists them. */
  hosts?: readonly Host[];
  onChange: (next: AgentMemberValue) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [section, setSection] = useState<Section | null>(null);
  const [modelDraft, setModelDraft] = useState("");
  const [manualModelEntry, setManualModelEntry] = useState(false);
  const modelInputRef = useRef<HTMLInputElement>(null);
  // The manual entry starts as the "Other model…" menu item — Radix's arrow
  // navigation skips a bare input, so entering the input happens by hand.
  useEffect(() => {
    if (manualModelEntry && section === "model") modelInputRef.current?.focus();
  }, [manualModelEntry, section]);
  // Closing the Model submenu restores its item, so keyboard arrows can reach
  // the input again on the next visit.
  useEffect(() => {
    if (section !== "model") setManualModelEntry(false);
  }, [section]);
  // A member's own host decides its catalog; without one the New Chat host
  // does, exactly as before the per-member host existed.
  const catalogHostId = hostId ?? sessionHostId;
  const { data: hostModelOptions } = useHostModelOptions(
    catalogHostId,
    harness,
    catalogHostId !== null,
  );
  const rows = useMemo(() => hostModelOptions ?? [], [hostModelOptions]);
  // Same option sources as the scheduled-task fields: the host catalog, else
  // the static Claude aliases for a Claude harness. The current value is
  // always listed so a saved pick the catalog no longer carries stays visible.
  const modelOptions = useMemo(() => {
    const options =
      rows.length > 0
        ? rows.map((option) => ({ id: option.id, label: option.displayName ?? option.id }))
        : harness.startsWith("claude")
          ? CLAUDE_NATIVE_MODELS.map((option) => ({ id: option.id, label: option.label }))
          : [];
    if (model !== null && !options.some((option) => option.id === model)) {
      options.push({ id: model, label: model });
    }
    return options;
  }, [rows, harness, model]);
  const effortLevels = effortLevelsFor(harness, rows, model);
  const harnessLabel = harnessOptions.find((option) => option.id === harness)?.label ?? harness;
  const modelLabel =
    model === null
      ? "Default"
      : (modelOptions.find((option) => option.id === model)?.label ?? model);
  const effortLabel =
    effort !== null && effortLevels !== null ? normalizeEffortLabel(effort) : undefined;
  const memberHost = hosts?.find((host) => host.host_id === hostId);
  const hostLabel =
    hostId === null
      ? "Session host"
      : (memberHost?.name ?? hostId) + (memberHost?.status === "offline" ? " (offline)" : "");

  const selectHarness = (id: string) => {
    if (id !== harness) onChange({ harness: id, model: null, effort: null, hostId });
  };
  const selectModel = (nextModel: string | null) =>
    onChange({
      harness,
      model: nextModel,
      effort: reconcileEffortOnModelChange(harness, rows, nextModel, effort),
      hostId,
    });
  const selectEffort = (nextEffort: string | null) =>
    onChange({ harness, model, effort: nextEffort, hostId });
  const selectHost = (nextHostId: string | null) => {
    if (nextHostId !== hostId) onChange({ harness, model, effort, hostId: nextHostId });
  };
  const commitModelDraft = () => {
    const trimmed = modelDraft.trim();
    if (trimmed === "") return;
    selectModel(trimmed);
    setModelDraft("");
  };
  const submenuProps = (name: Section) => ({
    open: section === name,
    onOpenChange: (next: boolean) =>
      setSection((current) => (next ? name : current === name ? null : current)),
    isMobile: false,
    disabled,
  });

  return (
    <HarnessPicker
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) {
          setSection(null);
          setModelDraft("");
        }
      }}
      configOpen={section !== null}
      trigger={{
        label: [harnessLabel, modelLabel, effortLabel].filter(Boolean).join(" · "),
        model: modelLabel,
        effort: effortLabel,
        icon: <ComposerAgentIcon agent={{ name: "", harness }} sdkMarkClassName="size-[15px]" />,
        testIdPrefix: "agent-member",
        disabled,
        "data-testid": "agent-member-trigger",
      }}
      contentAlign="start"
      contentSide="bottom"
      testId="agent-member-menu"
    >
      {hosts !== undefined && (
        <HarnessPickerConfigRow
          label="Host"
          value={hostLabel}
          testId="agent-member-host"
          configTestId="agent-member-host-menu"
          {...submenuProps("host")}
        >
          <ComposerConfigSections
            extra={[
              {
                testId: "agent-member-hosts",
                header: "Host",
                choices: [
                  {
                    key: DEFAULT_ROW_KEY,
                    label: "Session host",
                    checked: hostId === null,
                    onSelect: () => selectHost(null),
                    testId: "agent-member-host-session",
                  },
                  ...hosts.map((host) => ({
                    key: host.host_id,
                    label: host.status === "online" ? host.name : `${host.name} · offline`,
                    checked: host.host_id === hostId,
                    onSelect: () => selectHost(host.host_id),
                    testId: `agent-member-host-${host.host_id}`,
                  })),
                ],
              },
            ]}
          />
        </HarnessPickerConfigRow>
      )}
      <HarnessPickerConfigRow
        label="Harness"
        value={harnessLabel}
        testId="agent-member-harness"
        configTestId="agent-member-harness-menu"
        {...submenuProps("harness")}
      >
        <ComposerConfigSections
          extra={[
            {
              testId: "agent-member-harness-options",
              header: "Harness",
              choices: harnessOptions.map((option) => ({
                key: option.id,
                label: option.label,
                checked: option.id === harness,
                onSelect: () => selectHarness(option.id),
                testId: `agent-member-harness-${option.id}`,
              })),
            },
          ]}
        />
      </HarnessPickerConfigRow>
      <HarnessPickerConfigRow
        label="Model"
        value={modelLabel}
        testId="agent-member-model"
        configTestId="agent-member-model-menu"
        {...submenuProps("model")}
      >
        <ComposerConfigSections
          models={{
            testId: "agent-member-models",
            header: "Models",
            choices: [
              {
                key: DEFAULT_ROW_KEY,
                label: "Default",
                checked: model === null,
                onSelect: () => selectModel(null),
                testId: "agent-member-model-default",
              },
              ...modelOptions.map((option) => ({
                key: option.id,
                label: option.label,
                checked: option.id === model,
                onSelect: () => selectModel(option.id),
                testId: `agent-member-model-${option.id}`,
                data: { "data-model-id": option.id },
              })),
            ],
          }}
        />
        {/* A harness the host can't probe for models (Cursor) or no reachable
            host at all leaves only "Default", but Create requires a model id.
            The manual entry is a menu item: arrow keys skip a bare input. */}
        {manualModelEntry ? (
          <Input
            ref={modelInputRef}
            aria-label="Model id"
            placeholder="Model id…"
            value={modelDraft}
            onChange={(event) => setModelDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                event.stopPropagation();
                commitModelDraft();
                return;
              }
              // Keeps menu typeahead and arrow navigation off the field.
              event.stopPropagation();
            }}
            data-testid="agent-member-model-input"
          />
        ) : (
          <DropdownMenuItem
            data-testid="agent-member-model-other"
            onSelect={(event) => {
              event.preventDefault();
              setManualModelEntry(true);
            }}
          >
            Other model…
          </DropdownMenuItem>
        )}
      </HarnessPickerConfigRow>
      {effortLevels !== null && (
        <HarnessPickerConfigRow
          label="Effort"
          value={effort === null ? "Default" : normalizeEffortLabel(effort)}
          testId="agent-member-effort"
          configTestId="agent-member-effort-menu"
          {...submenuProps("effort")}
        >
          <ComposerConfigSections
            efforts={{
              testId: "agent-member-efforts",
              header: "Effort",
              choices: [
                {
                  key: DEFAULT_ROW_KEY,
                  label: "Default",
                  checked: effort === null,
                  onSelect: () => selectEffort(null),
                  testId: "agent-member-effort-default",
                },
                ...effortLevels.map((level) => ({
                  key: level,
                  label: normalizeEffortLabel(level),
                  checked: level === effort,
                  onSelect: () => selectEffort(level),
                  testId: `agent-member-effort-${level}`,
                  data: { "data-effort-level": level },
                })),
              ],
            }}
          />
        </HarnessPickerConfigRow>
      )}
    </HarnessPicker>
  );
}
