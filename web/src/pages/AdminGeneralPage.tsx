import { useState } from "react";
import { PageScroll } from "@/components/PageScroll";
import { ProactiveAgentAdminControls } from "@/components/ProactiveAgentAdminControls";
import { ProactiveAgentSettingControl } from "@/components/ProactiveAgentSettingControl";
import { SettingsGroup } from "@/components/SettingsGroup";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { useProactiveAgentSettings } from "@/hooks/useProactiveAgentSettings";
import { useSharing, useSetSharing } from "@/hooks/useSharing";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isSingleUserMode, type SiblingSessionDestinations } from "@/lib/capabilities";
import { PROACTIVE_AGENT_DISPLAY_NAME } from "@/lib/proactiveInbox";
import {
  ATTACHMENT_SIZE_LIMIT_ENTRY,
  NESTED_AGENT_DESTINATIONS_ENTRY,
  PROACTIVE_AGENT_WORKSPACE_ENTRY,
} from "@/pages/settings/search/catalog/admin";
import { settingsControlProps } from "@/pages/settings/search/settingsControl";
import { settingsOverlayProps } from "@/pages/settings/search/settingsOwnership";
import { SettingsEntryLabel, SettingsItem } from "@/pages/settings/search/SettingsItem";

const BYTES_PER_MB = 1024 * 1024;
const MAX_ATTACHMENT_LIMIT_MB = 102_400;

function formatLimitMb(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || bytes <= 0) return "0";
  // Never round a small positive byte cap down to "0" (which means unlimited).
  return String(Math.max(1, Math.round(bytes / BYTES_PER_MB)));
}

const AGENT_DESTINATIONS: {
  id: SiblingSessionDestinations;
  label: string;
  description: string;
}[] = [
  {
    id: "current_host",
    label: "Current host only",
    description: "New agents must run on the same host as the session they are added from.",
  },
  {
    id: "current_host_or_new_sandbox",
    label: "Current host or new sandbox",
    description: "New agents can use the current host or start in a fresh managed sandbox.",
  },
  {
    id: "any_host",
    label: "Any available host",
    description:
      "New agents can use the current host, another connected host, or a fresh managed sandbox.",
  },
];

export function AdminGeneralPage() {
  const info = useServerInfo();
  const isAdmin = useIsAdmin();
  const canManage = isSingleUserMode(info) || isAdmin;
  const nestedAgentsEnabled = info !== "loading" && info.sibling_sessions === true;
  const attachmentsConfigurable =
    info !== "loading" && info.attachment_oversize_to_filesystem === true;
  const { available: advisorAvailable, settings: advisorSettings } = useProactiveAgentSettings();
  const {
    data: sharing,
    error: sharingError,
    isLoading,
  } = useSharing({ enabled: canManage && (nestedAgentsEnabled || attachmentsConfigurable) });
  const setSharing = useSetSharing();
  const [destinationError, setDestinationError] = useState<string | null>(null);
  const [draftLimitMb, setDraftLimitMb] = useState<string | null>(null);
  const [limitError, setLimitError] = useState<string | null>(null);
  const [limitSaved, setLimitSaved] = useState(false);
  const destinations = sharing?.sibling_session_destinations ?? "any_host";
  const destination = AGENT_DESTINATIONS.find((option) => option.id === destinations);
  const serverLimitMb = formatLimitMb(
    sharing?.attachment_max_file_bytes !== undefined
      ? sharing.attachment_max_file_bytes
      : info !== "loading"
        ? info.attachment_limits?.file_bytes
        : undefined,
  );
  const limitValue = draftLimitMb ?? serverLimitMb;
  const isLimitDirty = draftLimitMb !== null && draftLimitMb.trim() !== serverLimitMb;
  const limitEditable = sharing?.attachment_max_file_bytes_editable === true;
  const showAttachmentLimit =
    attachmentsConfigurable &&
    sharing !== undefined &&
    sharing.attachment_max_file_bytes_editable !== undefined;

  function chooseDestinations(value: SiblingSessionDestinations) {
    if (
      value === destinations ||
      !sharing?.sibling_session_destinations_editable ||
      setSharing.isPending
    )
      return;
    setDestinationError(null);
    setSharing.mutate(
      { sibling_session_destinations: value },
      { onError: (error) => setDestinationError(error.message) },
    );
  }

  function saveAttachmentLimit() {
    if (!limitEditable || setSharing.isPending || !isLimitDirty) return;
    const trimmed = limitValue.trim();
    if (!/^\d+$/.test(trimmed)) {
      setLimitError("Enter a whole number of MB (0 for unlimited).");
      return;
    }
    const mb = Number.parseInt(trimmed, 10);
    if (!Number.isSafeInteger(mb) || mb < 0 || mb > MAX_ATTACHMENT_LIMIT_MB) {
      setLimitError(`Enter a limit between 0 and ${MAX_ATTACHMENT_LIMIT_MB} MB.`);
      return;
    }
    setLimitError(null);
    setLimitSaved(false);
    setSharing.mutate(
      { attachment_max_file_bytes: mb === 0 ? 0 : mb * BYTES_PER_MB },
      {
        onSuccess: () => {
          setDraftLimitMb(null);
          setLimitSaved(true);
        },
        onError: (error) => setLimitError(error.message),
      },
    );
  }

  if (!nestedAgentsEnabled && !advisorAvailable && !attachmentsConfigurable) return null;
  return (
    <PageScroll maxWidthClassName="max-w-4xl" contentClassName="px-4 md:px-8" extraBottom="2.5rem">
      <h1 className="text-2xl font-medium">Admin settings</h1>
      <p className="mt-1 mb-6 text-sm text-muted-foreground">Server-wide settings for all users.</p>
      {!canManage ? (
        <p className="text-ui text-muted-foreground">
          You don't have permission to manage server settings.
        </p>
      ) : (
        <>
          {isLoading && <p className="text-ui text-muted-foreground">Loading...</p>}
          {advisorAvailable && advisorSettings?.can_manage_workspace !== false && (
            <SettingsGroup
              title={PROACTIVE_AGENT_DISPLAY_NAME}
              testId="settings-group-admin-proactive-agent"
            >
              <ProactiveAgentSettingControl
                scope="workspace"
                entry={PROACTIVE_AGENT_WORKSPACE_ENTRY}
              >
                <div className="border-t border-border pt-4">
                  <ProactiveAgentAdminControls />
                </div>
              </ProactiveAgentSettingControl>
            </SettingsGroup>
          )}
          {nestedAgentsEnabled && sharing && (
            <SettingsGroup title="Nested agents" testId="settings-group-agent-destinations">
              <SettingsItem
                entry={NESTED_AGENT_DESTINATIONS_ENTRY}
                className="flex flex-col gap-3 md:flex-row-reverse md:items-start md:gap-6"
              >
                <Select
                  value={destinations}
                  onValueChange={(value) => chooseDestinations(value as SiblingSessionDestinations)}
                  disabled={!sharing.sibling_session_destinations_editable || setSharing.isPending}
                  componentId="settings.admin.agent_destinations"
                  valueHasNoPii
                >
                  <SelectTrigger
                    {...settingsControlProps(NESTED_AGENT_DESTINATIONS_ENTRY)}
                    className="w-full shrink-0 md:w-64"
                  >
                    <SelectValue>{destination?.label ?? destinations}</SelectValue>
                  </SelectTrigger>
                  <SelectContent
                    {...settingsOverlayProps(NESTED_AGENT_DESTINATIONS_ENTRY)}
                    position="popper"
                    align="end"
                    className="w-[30rem] max-w-[calc(100vw-2rem)]"
                  >
                    {AGENT_DESTINATIONS.filter(
                      (option) =>
                        sharing.sibling_session_destinations_options?.includes(option.id) ?? true,
                    ).map((option) => (
                      <SelectItem key={option.id} value={option.id} textValue={option.label}>
                        <span className="flex flex-col gap-1">
                          <span className="font-medium">{option.label}</span>
                          <span className="text-sm leading-normal whitespace-normal text-muted-foreground">
                            {option.description}
                          </span>
                        </span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <div className="min-w-0 flex-1 md:pt-1">
                  <SettingsEntryLabel
                    entry={NESTED_AGENT_DESTINATIONS_ENTRY}
                    description={destination?.description}
                  />
                </div>
              </SettingsItem>
            </SettingsGroup>
          )}
          {showAttachmentLimit && (
            <SettingsGroup title="Attachments" testId="settings-group-admin-attachments">
              <SettingsItem
                entry={ATTACHMENT_SIZE_LIMIT_ENTRY}
                className="flex flex-col gap-3 md:flex-row-reverse md:items-start md:gap-6"
              >
                <div className="flex shrink-0 items-center gap-2">
                  <Input
                    {...settingsControlProps(ATTACHMENT_SIZE_LIMIT_ENTRY)}
                    type="number"
                    min={0}
                    max={MAX_ATTACHMENT_LIMIT_MB}
                    step={1}
                    inputMode="numeric"
                    value={limitValue}
                    onChange={(event) => {
                      setDraftLimitMb(event.target.value);
                      setLimitError(null);
                      setLimitSaved(false);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        saveAttachmentLimit();
                      }
                    }}
                    disabled={!limitEditable || setSharing.isPending}
                    className="w-28"
                  />
                  <span className="text-sm text-muted-foreground">MB</span>
                  <Button
                    {...settingsControlProps(ATTACHMENT_SIZE_LIMIT_ENTRY, "save")}
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={saveAttachmentLimit}
                    disabled={!limitEditable || setSharing.isPending || !isLimitDirty}
                  >
                    Save
                  </Button>
                  {limitSaved && (
                    <span role="status" className="text-sm text-muted-foreground">
                      Saved
                    </span>
                  )}
                </div>
                <div className="min-w-0 flex-1 md:pt-1">
                  <SettingsEntryLabel entry={ATTACHMENT_SIZE_LIMIT_ENTRY} />
                </div>
              </SettingsItem>
            </SettingsGroup>
          )}
          {(limitError || destinationError || sharingError) && (
            <div
              role="alert"
              className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
            >
              {limitError ?? destinationError ?? sharingError?.message}
            </div>
          )}
        </>
      )}
    </PageScroll>
  );
}
