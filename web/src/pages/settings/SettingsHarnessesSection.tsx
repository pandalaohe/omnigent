import { useEffect, useId, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2Icon,
  ChevronDownIcon,
  KeyRoundIcon,
  Plus,
  SettingsIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { Link, useNavigate, useSearchParams } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { AgentNameTag } from "@/components/AgentNameTag";
import { AgentSettingsEditor } from "@/components/AgentsSettings";
import { AgentPromptCard } from "@/components/AgentContextTable";
import { useAgentRegistry } from "@/hooks/useAgentRegistry";
import { useHarnessCapabilities } from "@/lib/agentLabels";
import type { HarnessCapabilityMap } from "@/lib/sessionDefaultModes";
import {
  agentPresentOnHost,
  agentSettingsHarnessLabel,
  agentSettingsPath,
  harnessSettingsBadgeId,
} from "@/lib/agentSettings";
import { Switch } from "@/components/ui/switch";
import { Segmented } from "@/components/ui/segmented";
import { callingDefaultHarnesses, callingDefaultParent } from "@/lib/callingDefaults";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { ComposerAgentIcon } from "@/shell/NewChatDialog";
import {
  useHarnessStartup,
  useHosts,
  useRefreshHarnessAccounts,
  type HarnessAccount,
  type Host,
} from "@/hooks/useHosts";
import { ApiError } from "@/lib/sessionsApi";
import { useCuratedOutHarnesses } from "@/hooks/useCuratedOutHarnesses";
import {
  HARNESS_CREDENTIAL_COPY,
  harnessInstallableOnHost,
  harnessWarningBadgeText,
  MIN_TMUX_VERSION,
  missingHarnessCredentialCopy,
  type HarnessCredentialKind,
} from "@/lib/harnessSetup";
import { NATIVE_CODING_AGENTS, nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";
import {
  readHideUnconfiguredHarnesses,
  writeHideUnconfiguredHarnesses,
} from "@/lib/harnessVisibilityPreferences";
import { canonicalNativeHarness, harnessAccountKey } from "@/lib/nativeHarnessModes";
import { BackButton, HarnessCatalog } from "./HarnessCatalog";
import { HarnessFixDialog } from "./HarnessFixDialog";
import { AgentSettingsFolder } from "./AgentSettingsFolder";
import { AgentModelList } from "./AgentModelList";
import {
  applyHarnessFixResult,
  harnessFixMode,
  harnessStatus,
  type HarnessFixMode,
  type HarnessFixResult,
} from "@/lib/harnessFix";
import { useSettingsRoute } from "../../shell/settingsNav";
import {
  HARNESS_CARD_ENTRIES,
  HARNESS_CREDENTIAL_ENTRIES,
  HARNESS_DESCRIPTIONS,
  HARNESS_DETAIL_ENTRIES,
  type HarnessCredentialActionName,
  type HarnessFixActionName,
  harnessInventoryPage,
  HARNESSES_GRID_ENTRY,
  type NativeHarnessId,
  type FixOpener,
} from "./search/catalog/harnesses";
import { SettingsEntryLabel, SettingsItem } from "./search/SettingsItem";
import { HIDE_UNCONFIGURED_HARNESSES_ENTRY } from "./search/catalog/appearance";
import {
  SettingsOverlayOwner,
  SettingsOwner,
  settingsOverlayProps,
  settingsRuntimeChoiceProps,
} from "./search/settingsOwnership";
import { type SettingsControlProps, settingsControlProps } from "./search/settingsControl";

interface HarnessEntry {
  /** Native or registered harness slug used by the host. */
  harness: string;
  name: string;
  description: string;
  agentName: string;
  agentId?: string;
}

// The harness catalog, derived from the shared native-agent registry so this
// list stays in step with the rest of the app; descriptions come from the Settings
// catalog. Sorted by the registry's own picker order.
const HARNESS_ENTRIES: HarnessEntry[] = [...NATIVE_CODING_AGENTS]
  .sort((a, b) => a.sortRank - b.sortRank)
  .map((spec) => ({
    harness: spec.harness,
    name: spec.displayName,
    agentName: spec.agentName,
    description: HARNESS_DESCRIPTIONS[spec.harness] ?? "",
  }));

function hostHarnessKey(hostId: string, harness: string): string {
  return `${hostId}:${harness}`;
}

function catalogHarness(harness: string): NativeHarnessId | undefined {
  return HARNESS_DETAIL_ENTRIES.members.find((member) => member === harness);
}

/**
 * Settings → Harnesses: the harness grid at /settings/harnesses, or one
 * harness's details at /settings/harnesses/<harness>. An unknown slug, or one
 * the deployment curated out, shows the grid. Host selection lives here so it
 * survives grid ↔ details navigation.
 */
export const SettingsHarnessesSection = () => {
  const { harness } = useSettingsRoute();
  const registry = useAgentRegistry();
  const capabilities = useHarnessCapabilities();
  const info = useServerInfo();
  const curatedOut = useCuratedOutHarnesses();
  const offered = useMemo(
    () => HARNESS_ENTRIES.filter((entry) => !curatedOut.has(entry.harness)),
    [curatedOut],
  );
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const requestedHostId = searchParams.get("host");
  const [selectedHostId, setSelectedHostId] = useState<string | null>(requestedHostId);
  // A session's link names its host, which may be a sandbox the picker otherwise hides.
  const [linkedHostId, setLinkedHostId] = useState<string | null>(requestedHostId);
  // In-section links drop `?host`, so only a new deep link replaces the pick.
  useEffect(() => {
    if (requestedHostId === null) return;
    setSelectedHostId(requestedHostId);
    setLinkedHostId(requestedHostId);
  }, [requestedHostId]);
  const { data: hosts, isPending: hostsLoading } = useHosts({
    refetchOnFocus: true,
    sourceHostId: linkedHostId ?? undefined,
  });
  const [credentialKinds, setCredentialKinds] = useState<Record<string, HarnessCredentialKind>>({});
  const [badgeEditing, setBadgeEditing] = useState<{
    id: string;
    name: string;
    harness: string;
  } | null>(null);
  const [readyNotice, setReadyNotice] = useState<{
    harness: string;
    hostId: string;
    version?: string;
    account?: HarnessAccount;
  } | null>(null);

  // Gate the "Set up" affordance on the install feature, matching New Chat: with
  // it off the setup dialog has no runnable install step, so we show status
  // only (no button that opens a dead-end dialog).
  const canSetup = isFeatureEnabled(info, "harness_install");

  // Online hosts first, then offline (disabled in the menu). Default the
  // selection to the first online host; the picker can override.
  const sortedHosts = useMemo(
    () =>
      [...(hosts ?? [])].sort(
        (a, b) => Number(b.status === "online") - Number(a.status === "online"),
      ),
    [hosts],
  );
  const onlineHosts = useMemo(
    () => sortedHosts.filter((h) => h.status === "online"),
    [sortedHosts],
  );
  const selectedHost =
    sortedHosts.find((h) => h.host_id === selectedHostId) ??
    onlineHosts[0] ??
    sortedHosts[0] ??
    null;
  useRefreshHarnessAccounts(
    selectedHost?.host_id ?? null,
    canSetup &&
      selectedHost?.status === "online" &&
      selectedHost.harness_accounts_supported === true,
  );

  // Capture the selected host when the fix opens so a later host change cannot
  // redirect an in-progress install or credential write to another machine.
  const [fixTarget, setFixTarget] = useState<{
    entry: HarnessEntry;
    host: Host;
    mode: HarnessFixMode;
    opener: FixOpener;
  } | null>(null);
  // The fix dialog's result: record the host's new state and the notice it leaves behind.
  const handleFixed = ({
    configuredHarnesses,
    credentialKind,
    version,
    gatewayInference,
    harnessAccounts,
  }: HarnessFixResult) => {
    if (!fixTarget) return;
    const { host: fixedHost, entry } = fixTarget;
    applyHarnessFixResult(queryClient, fixedHost.host_id, {
      configuredHarnesses,
      harnessAccounts,
      gatewayInference,
    });
    if (credentialKind) {
      setCredentialKinds((current) => ({
        ...current,
        [hostHarnessKey(fixedHost.host_id, entry.harness)]: credentialKind,
      }));
    }
    setReadyNotice({
      harness: entry.harness,
      hostId: fixedHost.host_id,
      version,
      account: harnessAccounts?.[harnessAccountKey(entry.harness)],
    });
  };
  const openFix = (
    entry: HarnessEntry,
    targetHost: Host,
    mode: HarnessFixMode,
    opener: FixOpener = "setUp",
  ) => setFixTarget({ entry, host: targetHost, mode, opener });
  const fixCatalogHarness = fixTarget ? catalogHarness(fixTarget.entry.harness) : undefined;

  const notSetUp = offered.filter(
    (entry) => !sortedHosts.some((host) => agentPresentOnHost(entry.harness, host)),
  );

  const registered = registry.items.find(
    (item) => item.key === `harness:${harness}` || item.aliasKeys?.includes(`harness:${harness}`),
  );
  const knownHarness =
    harness &&
    (registered ||
      Object.hasOwn(capabilities, harness) ||
      callingDefaultHarnesses(capabilities).includes(harness) ||
      sortedHosts.some((host) => Object.hasOwn(host.configured_harnesses ?? {}, harness)));
  const nativeHarness = harness && nativeCodingAgentForHarness(canonicalNativeHarness(harness));
  const isCuratedOut = !!nativeHarness && curatedOut.has(nativeHarness.harness);
  const detailEntry =
    offered.find((entry) => entry.harness === harness) ??
    (knownHarness && !isCuratedOut
      ? {
          harness,
          name: agentSettingsHarnessLabel(harness, capabilities, registered?.label),
          description: registered?.description ?? "",
          agentName: registered?.name ?? harness,
        }
      : undefined);
  const detail = detailEntry
    ? {
        ...detailEntry,
        agentId: harnessSettingsBadgeId(detailEntry.harness, registry.legacyProducts),
      }
    : undefined;

  return (
    <div className="@container">
      {detail ? (
        <>
          <div className="mb-4">
            {selectedHost && (
              <Segmented
                label="Host"
                options={sortedHosts.map((host) => ({
                  value: host.host_id,
                  label: `${host.name || host.host_id}${host.status === "offline" ? " (offline)" : ""}`,
                }))}
                value={selectedHost.host_id}
                onChange={setSelectedHostId}
                className="flex-wrap"
              />
            )}
          </div>
          <HarnessDetail
            key={detail.harness}
            entry={detail}
            capabilities={capabilities}
            onEditBadge={(id, name, badgeHarness) =>
              setBadgeEditing({ id, name, harness: badgeHarness })
            }
            host={selectedHost}
            hostsLoading={hostsLoading}
            canSetup={canSetup}
            installable={harnessInstallableOnHost(info, detail.harness, selectedHost)}
            credentialKind={
              selectedHost
                ? credentialKinds[hostHarnessKey(selectedHost.host_id, detail.harness)]
                : undefined
            }
            readyNotice={
              readyNotice !== null &&
              readyNotice.hostId === selectedHost?.host_id &&
              readyNotice.harness === detail.harness
                ? readyNotice
                : null
            }
            onFix={(mode, opener) => selectedHost && openFix(detail, selectedHost, mode, opener)}
          />
        </>
      ) : harness && !isCuratedOut ? (
        <>
          <BackButton label="Agents" to="/settings/agents" />
          <p
            role={registry.isLoading ? "status" : "alert"}
            className="text-sm text-muted-foreground"
          >
            {registry.isLoading ? "Loading agent…" : "Agent not found."}
          </p>
        </>
      ) : (
        <SettingsItem entry={HARNESSES_GRID_ENTRY}>
          {offered.length === 0 && (
            <p className="text-sm text-muted-foreground">
              This server offers no harnesses to set up.
            </p>
          )}
          {notSetUp.length > 0 && (
            <details className="border-t border-border pt-4" id="not-set-up">
              <summary
                {...settingsControlProps(HARNESSES_GRID_ENTRY, "setup")}
                className="cursor-pointer text-ui font-medium"
              >
                Not set up ({notSetUp.length})
              </summary>
              <div className="mt-4 flex flex-wrap items-center gap-3">
                <HostSelect
                  hosts={sortedHosts}
                  selected={selectedHost}
                  onSelect={setSelectedHostId}
                />
              </div>
              {!selectedHost && <NoHostNotice loading={hostsLoading} />}
              <div
                className={cn(
                  "mt-4 grid grid-cols-1 gap-3 @[520px]:grid-cols-2",
                  // No host to resolve status against — dim the catalog to read as inactive.
                  !selectedHost && "opacity-50",
                )}
              >
                {notSetUp.map((entry) => (
                  <HarnessCard
                    key={entry.harness}
                    entry={entry}
                    host={selectedHost}
                    canSetup={canSetup}
                    installable={harnessInstallableOnHost(info, entry.harness, selectedHost)}
                    credentialKind={
                      selectedHost
                        ? credentialKinds[hostHarnessKey(selectedHost.host_id, entry.harness)]
                        : undefined
                    }
                    onFix={(mode) => selectedHost && openFix(entry, selectedHost, mode)}
                  />
                ))}
              </div>
            </details>
          )}
          <section className="mt-8 flex flex-col gap-3">
            <h2 className="text-ui font-medium">Session picker</h2>
            <div className="rounded-xl border border-border bg-card p-4">
              <HideUnconfiguredHarnessesControl />
            </div>
          </section>
        </SettingsItem>
      )}
      {fixTarget?.opener === "setUp" && (
        <SettingsOverlayOwner
          entry={fixCatalogHarness ? HARNESS_CARD_ENTRIES.entry(fixCatalogHarness) : undefined}
          opener="setUp"
        >
          <HarnessFixDialog
            open
            onOpenChange={(open) => !open && setFixTarget(null)}
            mode={fixTarget.mode}
            host={fixTarget.host}
            harness={fixTarget.entry.harness}
            harnessLabel={fixTarget.entry.name}
            onFixed={handleFixed}
          />
        </SettingsOverlayOwner>
      )}
      {fixTarget?.opener === "fixAction" && (
        <SettingsOverlayOwner
          entry={fixCatalogHarness ? HARNESS_DETAIL_ENTRIES.entry(fixCatalogHarness) : undefined}
          opener="fixAction"
        >
          <HarnessFixDialog
            open
            onOpenChange={(open) => !open && setFixTarget(null)}
            mode={fixTarget.mode}
            host={fixTarget.host}
            harness={fixTarget.entry.harness}
            harnessLabel={fixTarget.entry.name}
            onFixed={handleFixed}
          />
        </SettingsOverlayOwner>
      )}
      {fixTarget?.opener === "credentialAction" && (
        <SettingsOverlayOwner
          entry={
            fixCatalogHarness ? HARNESS_CREDENTIAL_ENTRIES.entry(fixCatalogHarness) : undefined
          }
          opener="credentialAction"
        >
          <HarnessFixDialog
            open
            onOpenChange={(open) => !open && setFixTarget(null)}
            mode={fixTarget.mode}
            host={fixTarget.host}
            harness={fixTarget.entry.harness}
            harnessLabel={fixTarget.entry.name}
            onFixed={handleFixed}
          />
        </SettingsOverlayOwner>
      )}
      {badgeEditing && (
        <AgentSettingsEditor
          agent={{ ...badgeEditing, custom: false }}
          onClose={() => setBadgeEditing(null)}
          onSaved={async () => {}}
        />
      )}
    </div>
  );
};

function HideUnconfiguredHarnessesControl() {
  const [value, setValue] = useState(readHideUnconfiguredHarnesses);
  return (
    <SettingsItem
      entry={HIDE_UNCONFIGURED_HARNESSES_ENTRY}
      className="flex items-start justify-between gap-6"
    >
      <SettingsEntryLabel
        entry={HIDE_UNCONFIGURED_HARNESSES_ENTRY}
        description="Only show harnesses that are set up on the selected host in the new-chat picker. Harnesses needing a CLI install or sign-in are hidden instead of badged."
      />
      <Switch
        {...settingsControlProps(HIDE_UNCONFIGURED_HARNESSES_ENTRY)}
        checked={value}
        onCheckedChange={(next) => {
          setValue(next);
          writeHideUnconfiguredHarnesses(next);
        }}
        data-testid="hide-unconfigured-harnesses-toggle"
        className="mt-0.5 shrink-0"
        componentId="settings.harnesses.hide_unconfigured_harnesses"
      />
    </SettingsItem>
  );
}

function NoHostNotice({ loading }: { loading: boolean }) {
  if (loading) return <p className="text-ui text-muted-foreground">Loading hosts…</p>;
  return (
    <p className="text-ui text-muted-foreground" data-testid="harness-no-host">
      Connect an online host to see which harnesses are installed and to set them up.
    </p>
  );
}

/** Online/offline status dot, mirroring the composer host rows. */
function HostStatusDot({ online }: { online: boolean }) {
  return (
    <span
      aria-hidden
      className={cn(
        "size-2 shrink-0 rounded-full",
        online ? "bg-success" : "border-[1.5px] border-muted-foreground",
      )}
    />
  );
}

/**
 * Host picker for the Harnesses grid — a compact status pill that opens a menu
 * of hosts (online selectable, offline disabled). Mirrors the composer's host
 * rows (status dot + name) without its connect-new-host / sandbox affordances.
 */
function HostSelect({
  hosts,
  selected,
  onSelect,
}: {
  hosts: Host[];
  selected: Host | null;
  onSelect: (hostId: string) => void;
}) {
  if (hosts.length === 0) return null;
  return (
    <div className="flex items-center gap-2">
      <span className="text-ui text-muted-foreground">Machine</span>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="outline"
            size="sm"
            className="h-8 shrink-0 gap-1.5"
            data-testid="harness-host-select"
            componentId="settings.harnesses.host"
            {...settingsControlProps(HARNESSES_GRID_ENTRY, "machine", {
              runtime: selected?.name ?? "Select a host",
            })}
          >
            <HostStatusDot online={selected?.status === "online"} />
            <span className="max-w-40 truncate">{selected?.name ?? "Select a host"}</span>
            <ChevronDownIcon className="size-3.5 shrink-0 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          className="min-w-52"
          {...settingsOverlayProps(HARNESSES_GRID_ENTRY, "machine")}
        >
          {/* One host is the selected one, so its item is checked. */}
          <DropdownMenuRadioGroup value={selected?.host_id ?? ""}>
            {hosts.map((h) => {
              const online = h.status === "online";
              return (
                <DropdownMenuRadioItem
                  key={h.host_id}
                  value={h.host_id}
                  disabled={!online}
                  onSelect={() => online && onSelect(h.host_id)}
                  data-active={h.host_id === selected?.host_id ? "true" : undefined}
                  data-testid={`harness-host-${h.host_id}`}
                  {...settingsRuntimeChoiceProps(HARNESSES_GRID_ENTRY, "machine")}
                  className="gap-1.5 data-[active=true]:bg-muted dark:data-[active=true]:bg-muted/50"
                >
                  <HostStatusDot online={online} />
                  <span className="min-w-0 flex-1 truncate">{h.name}</span>
                  {!online && <span className="text-xs text-muted-foreground">Offline</span>}
                </DropdownMenuRadioItem>
              );
            })}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

function credentialPresentation(
  host: Host | null,
  harness: string,
  override?: HarnessCredentialKind,
) {
  if (override === "personal") {
    return HARNESS_CREDENTIAL_COPY.personal;
  }
  if (override === "isaac" || host?.gateway_inference?.[harness] === true) {
    return HARNESS_CREDENTIAL_COPY.isaac;
  }
  const account = host?.harness_accounts?.[harnessAccountKey(harness)];
  if (account) {
    if (account.kind === "providers") {
      return {
        label: "Providers",
        description: providerDetails(account),
      };
    }
    if (account.kind === "api_key") {
      return { label: "API key", description: "Uses an API key configured on this host" };
    }
    if (account.kind === "chatgpt" || account.kind === "subscription") {
      return {
        label: "Personal subscription",
        description: accountDetails(account),
      };
    }
  }
  return { label: "Signed in", description: "Uses the harness's own login on this host" };
}

function accountDetails(account: HarnessAccount): string {
  return [account.email, account.org, account.plan].filter(Boolean).join(" · ") || "Signed in";
}

function providerDetails(account: HarnessAccount): string {
  const providers = account.providers ?? [];
  if (providers.length === 0) return "No providers configured";
  return providers
    .map((provider) => {
      const authType = provider.auth_type.replaceAll("_", " ");
      return `${provider.label} (${authType === "api key" ? "API key" : authType})`;
    })
    .join(" · ");
}

function configuredAccountLabel(host: Host | null, harness: string): string | undefined {
  if (host?.gateway_inference?.[harness] === true) {
    return HARNESS_CREDENTIAL_COPY.isaac.label;
  }
  const account = host?.harness_accounts?.[harnessAccountKey(harness)];
  if (!account) return undefined;
  if (account.kind === "providers") {
    const labels = (account.providers ?? []).map((provider) => provider.label);
    return labels.length > 0 ? labels.join(", ") : undefined;
  }
  return [account.email, account.plan].filter(Boolean).join(" · ") || undefined;
}

function canSwitchHarnessAccount(host: Host | null, harness: string): boolean {
  if (host?.harness_login_supported !== true) return false;
  const accountKey = harnessAccountKey(harness);
  const kind = host.harness_accounts?.[accountKey]?.kind;
  return (
    (accountKey === "codex" && kind === "chatgpt") ||
    (accountKey === "claude" && kind === "subscription")
  );
}

function reconfigurationReason(reason: string | null) {
  if (reason === "needs-auth") return "Missing credentials";
  if (reason === "version-too-low") return "Out of date";
  if (reason === "binary-missing") return "Not installed";
  if (reason === "readiness-error") return "Readiness check failed";
  if (reason === "unconfigured") return "Not configured";
  const fallback = harnessWarningBadgeText(reason);
  return fallback.charAt(0).toUpperCase() + fallback.slice(1);
}

function HarnessStatusText({
  status,
  configuredCredential,
  enhanced,
}: {
  status: ReturnType<typeof harnessStatus>;
  configuredCredential?: string;
  enhanced: boolean;
}) {
  if (status.ready) {
    return (
      <span className="flex items-center gap-1.5 text-xs">
        <span className="text-green-600 dark:text-green-400">Configured</span>
        {configuredCredential && (
          <span className="text-muted-foreground">· {configuredCredential}</span>
        )}
      </span>
    );
  }
  if (status.reason === "cli-unresponsive") {
    return (
      <span className="flex w-fit items-center gap-1 text-xs text-amber-600 dark:text-amber-500">
        <TriangleAlertIcon className="size-3.5" aria-hidden />
        CLI did not respond
      </span>
    );
  }
  if (status.needsSetup && !enhanced) {
    const text = harnessWarningBadgeText(status.reason);
    return (
      <span className="text-xs text-amber-600 dark:text-amber-500">
        {text.charAt(0).toUpperCase() + text.slice(1)}
      </span>
    );
  }
  if (status.needsSetup) {
    if (status.reason === "binary-missing" || status.reason === "unconfigured") {
      return (
        <span className="flex w-fit items-center gap-1 text-xs text-amber-600 dark:text-amber-500">
          <TriangleAlertIcon className="size-3.5" aria-hidden />
          {reconfigurationReason(status.reason)}
        </span>
      );
    }
    const label = reconfigurationReason(status.reason);
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className="flex w-fit cursor-default items-center gap-1 text-xs text-amber-600 dark:text-amber-500"
            aria-label={`Needs reconfiguration: ${label}`}
          >
            <TriangleAlertIcon className="size-3.5" aria-hidden />
            Needs reconfiguration
          </span>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    );
  }
  return null;
}

function HarnessIcon({ entry }: { entry: HarnessEntry }) {
  return (
    <div className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border [&_img]:size-5 [&_svg]:size-5">
      <ComposerAgentIcon agent={{ name: entry.agentName, harness: entry.harness }} />
    </div>
  );
}

function SetupButton({ entry, onSetup }: { entry: HarnessEntry; onSetup: () => void }) {
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      className="shrink-0"
      {...settingsControlProps(
        HARNESS_CARD_ENTRIES.entry(entry.harness as NativeHarnessId),
        "setUp",
      )}
      data-testid={`harness-action-${entry.harness}`}
      componentId="settings.harnesses.setup"
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onSetup();
      }}
    >
      <Plus />
      {/* Set up */}
    </Button>
  );
}

/** Grid card. An installed harness's card opens its details page. */
function HarnessCard({
  entry,
  host,
  canSetup,
  installable,
  credentialKind,
  onFix,
}: {
  entry: HarnessEntry;
  host: Host | null;
  canSetup: boolean;
  installable: boolean;
  credentialKind?: HarnessCredentialKind;
  onFix: (mode: HarnessFixMode) => void;
}) {
  const status = harnessStatus(entry.harness, host);
  const mode = harnessFixMode(status.reason);
  const canFix = canSetup && host?.status === "online" && !host.sandbox_provider;
  const canInstall = canFix && installable && mode === "install";
  const needsAttention = status.needsSetup || status.reason === "cli-unresponsive";
  const opensDetail = status.ready || (needsAttention && canFix);
  const navigate = useNavigate();
  // This card is only rendered for the built-in harness catalog.
  const card = HARNESS_CARD_ENTRIES.entry(entry.harness as NativeHarnessId);
  // The link is named by the harness; its status and description are its description.
  const statusId = useId();
  const descriptionId = useId();
  const body = (
    <>
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-3">
          <HarnessIcon entry={entry} />
          <div className="flex min-w-0 flex-col">
            <span className="truncate text-ui font-medium text-foreground">{entry.name}</span>
            <div id={statusId}>
              <HarnessStatusText
                status={status}
                enhanced={canSetup}
                configuredCredential={
                  status.ready && canSetup
                    ? (configuredAccountLabel(host, entry.harness) ??
                      credentialPresentation(host, entry.harness, credentialKind).label)
                    : undefined
                }
              />
            </div>
          </div>
        </div>
        {(status.ready || (needsAttention && canFix && !canInstall)) && (
          <Button
            size="icon-sm"
            variant="ghost"
            {...settingsControlProps(card, "settings")}
            data-testid={`harness-settings-${entry.harness}`}
            componentId="settings.harnesses.open_settings"
            onClick={(e) => {
              // Inside the card's link: don't also follow it to the MCP servers tab.
              e.preventDefault();
              navigate(`${agentSettingsPath(entry.harness, host?.host_id)}&tab=settings`);
            }}
          >
            <SettingsIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
          </Button>
        )}
        {status.needsSetup && canInstall && (
          <SetupButton entry={entry} onSetup={() => onFix("install")} />
        )}
      </div>
      <p id={descriptionId} className="line-clamp-2 text-sm text-muted-foreground">
        {entry.description}
      </p>
    </>
  );
  const className =
    "flex flex-col gap-2 rounded-xl border border-border bg-card p-4 transition-colors";
  // The grid cell wraps the card so search can reveal it; the card still fills the cell's height.
  return (
    <SettingsItem entry={card} className="grid">
      {opensDetail ? (
        <Link
          {...settingsControlProps(card, "open")}
          aria-describedby={`${statusId} ${descriptionId}`}
          to={`${agentSettingsPath(entry.harness, host?.host_id)}${status.ready ? "" : "&tab=settings"}`}
          // Only cards that open a details page get the hover, so it reads as clickable.
          className={cn(
            className,
            "hover:border-foreground/20 hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none",
          )}
          data-testid={`harness-card-${entry.harness}`}
          componentId="settings.harnesses.open"
        >
          {body}
        </Link>
      ) : (
        <div className={className} data-testid={`harness-card-${entry.harness}`}>
          {body}
        </div>
      )}
    </SettingsItem>
  );
}

/** Details page for one harness on the selected host. */
function HarnessDetail({
  entry,
  host,
  hostsLoading,
  canSetup,
  installable,
  credentialKind,
  readyNotice,
  onFix,
  capabilities,
  onEditBadge,
}: {
  entry: HarnessEntry;
  host: Host | null;
  hostsLoading: boolean;
  canSetup: boolean;
  installable: boolean;
  credentialKind?: HarnessCredentialKind;
  readyNotice: {
    harness: string;
    hostId: string;
    version?: string;
    account?: HarnessAccount;
  } | null;
  onFix: (mode: HarnessFixMode, opener: FixOpener) => void;
  capabilities?: HarnessCapabilityMap;
  onEditBadge: (id: string, name: string, harness: string) => void;
}) {
  const status = harnessStatus(entry.harness, host);
  const credentialProblem = isCredentialProblem(status.reason);
  const mode = harnessFixMode(status.reason);
  const canFix = canSetup && host?.status === "online" && !host.sandbox_provider;
  const gateway = host?.gateway_inference?.[entry.harness] === true;
  const account = host?.harness_accounts?.[harnessAccountKey(entry.harness)];
  const presentedCredential = canSetup
    ? credentialPresentation(host, entry.harness, credentialKind)
    : gateway
      ? {
          label: "Unity Gateway",
          description: "Managed credential via the Databricks Unity Gateway",
        }
      : { label: "Signed in", description: "Uses the harness's own login on this host" };
  const credential =
    account?.expired && status.reason === "needs-auth"
      ? {
          label: "Sign-in expired",
          description: `${accountDetails(account)} — new sessions on ${host?.name ?? "this host"} can’t use ${entry.name} until you sign in again.`,
        }
      : presentedCredential;
  const nativeId = catalogHarness(entry.harness);
  const detail = nativeId ? HARNESS_DETAIL_ENTRIES.entry(nativeId) : undefined;
  const credentialEntry = nativeId ? HARNESS_CREDENTIAL_ENTRIES.entry(nativeId) : undefined;
  const credentialAction = (name?: HarnessCredentialActionName) => ({
    label:
      name === "switch"
        ? "Switch account"
        : name === "signInAgain"
          ? "Sign in again"
          : "Reconfigure",
    control: credentialEntry
      ? settingsControlProps(
          credentialEntry,
          "credentialAction",
          name === undefined ? undefined : { alternative: name },
        )
      : undefined,
  });
  const header = (
    <>
      {detail ? (
        <SettingsOwner entry={detail} part="back">
          <BackButton label="Agents" to="/settings/agents" />
        </SettingsOwner>
      ) : (
        <BackButton label="Agents" to="/settings/agents" />
      )}
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-center gap-3">
          <HarnessIcon entry={entry} />
          <div className="flex min-w-0 flex-col">
            <h1 className="settings-page-title text-2xl font-medium">
              <AgentNameTag
                agentId={entry.agentId ?? null}
                name={entry.name}
                harness={entry.harness}
              />
            </h1>
            <div className="flex flex-wrap gap-2">
              {Object.keys(capabilities ?? {})
                .filter(
                  (integration) =>
                    callingDefaultParent(integration, capabilities) === entry.harness &&
                    host &&
                    agentPresentOnHost(integration, host),
                )
                .map((integration) => (
                  <span key={integration} title={capabilities?.[integration]?.label ?? integration}>
                    <AgentNameTag
                      agentId={null}
                      name={integration}
                      harness={integration}
                      markOnly
                    />
                    <span className="sr-only">
                      {capabilities?.[integration]?.label ?? integration}
                    </span>
                  </span>
                ))}
            </div>
            <HarnessStatusText
              status={status}
              enhanced={canSetup}
              configuredCredential={
                status.ready && canSetup
                  ? (configuredAccountLabel(host, entry.harness) ?? credential.label)
                  : undefined
              }
            />
          </div>
        </div>
        {entry.agentId && (
          <Button
            variant="ghost"
            size="sm"
            aria-label={`Edit badge for ${entry.name}`}
            onClick={() => onEditBadge(entry.agentId!, entry.name, entry.harness)}
          >
            Edit badge
          </Button>
        )}
      </div>
    </>
  );
  const credentialCard = (
    <SettingsItem entry={credentialEntry}>
      <CredentialCard
        label={
          account?.expired && credentialProblem
            ? "Sign-in expired"
            : credentialProblem
              ? "Not signed in"
              : credential.label
        }
        description={
          account?.expired && credentialProblem
            ? credential.description
            : credentialProblem && host
              ? missingHarnessCredentialCopy(entry.harness, entry.name, host.name)
              : credential.description
        }
        ready={status.ready}
        warning={credentialProblem}
        showStatus={canSetup && host?.status === "online"}
        statusLabel={
          status.reason === "cli-unresponsive"
            ? "Not checked"
            : status.reason === "binary-missing" || status.reason === "unconfigured"
              ? reconfigurationReason(status.reason)
              : "Needs reconfiguration"
        }
        action={
          status.ready && canFix && canSwitchHarnessAccount(host, entry.harness)
            ? { ...credentialAction("switch"), onClick: () => onFix("switch", "credentialAction") }
            : status.reason === "needs-auth" && account?.expired && canFix
              ? {
                  ...credentialAction("signInAgain"),
                  onClick: () => onFix(mode, "credentialAction"),
                }
              : credentialProblem && canFix
                ? { ...credentialAction(), onClick: () => onFix(mode, "credentialAction") }
                : undefined
        }
      />
    </SettingsItem>
  );
  const settings =
    host && !status.ready && !canSetup ? (
      <p className="text-ui text-muted-foreground">
        Set up {entry.name} on {host.name} to manage its MCP servers, skills, and plugins.
      </p>
    ) : host ? (
      <div className="flex flex-col gap-6">
        {readyNotice && (
          <ReadyNotice
            entry={entry}
            host={host}
            version={readyNotice.version}
            account={readyNotice.account}
          />
        )}
        {(status.needsSetup || status.reason === "cli-unresponsive") && !credentialProblem && (
          <HarnessWarning
            entry={entry}
            host={host}
            mode={mode}
            reason={status.reason}
            installable={installable}
            canFix={canFix}
            onFix={(fixMode) => onFix(fixMode, "fixAction")}
          />
        )}
        {credentialCard}
        <AgentSettingsFolder host={host} harness={entry.harness} />
        <AgentModelList host={host} harness={entry.harness} />
        {agentPresentOnHost(entry.harness, host) && (
          <AgentPromptCard
            harness={entry.harness}
            host={host}
            capabilities={capabilities}
            agentId={entry.agentId}
            name={entry.name}
          />
        )}
      </div>
    ) : null;
  if (host && status.reason === "platform-unsupported") {
    return (
      <>
        {header}
        <p className="mt-6 text-sm text-muted-foreground">Not on Windows</p>
      </>
    );
  }
  // The host inventory only covers these families' MCP servers, skills, and plugins.
  const inventoryPage = nativeId ? harnessInventoryPage(nativeId) : undefined;
  if (host && inventoryPage && (status.ready || canFix)) {
    return (
      <SettingsItem entry={detail}>
        <HarnessCatalog
          header={
            <>
              {header}
              <div className="mt-6">{settings}</div>
            </>
          }
          footer={
            <div className="mt-6">
              {host.status === "online" && <StartupSettings host={host} harness={entry.harness} />}
            </div>
          }
          host={host}
          entries={inventoryPage}
        />
      </SettingsItem>
    );
  }
  return (
    <SettingsItem entry={detail}>
      {header}
      <div className="mt-6 flex flex-col gap-6">
        {!host ? (
          <NoHostNotice loading={hostsLoading} />
        ) : (
          <>
            {status.ready && (
              <p className="text-ui text-muted-foreground">
                MCP servers, skills, and plugins aren't listed for {entry.name} yet.
              </p>
            )}
            {settings}
            {host.status === "online" ? (
              <StartupSettings host={host} harness={entry.harness} />
            ) : (
              <p className="text-sm text-muted-foreground">
                Connect {host.name} to view startup settings.
              </p>
            )}
          </>
        )}
      </div>
    </SettingsItem>
  );
}

function isCredentialProblem(reason: string | null): boolean {
  return reason === "needs-auth";
}

function HarnessWarning({
  entry,
  host,
  mode,
  reason,
  installable,
  canFix,
  onFix,
}: {
  entry: HarnessEntry;
  host: Host;
  mode: HarnessFixMode;
  reason: string | null;
  installable: boolean;
  canFix: boolean;
  onFix: (mode: HarnessFixMode) => void;
}) {
  const copy =
    reason === "cli-unresponsive"
      ? `${entry.name}’s CLI on ${host.name} didn’t respond to a version check. New sessions still try to start it; run it on that machine and finish any setup prompt it shows.`
      : reason === "readiness-error"
        ? `${entry.name} isn’t working on ${host.name}. Run setup again to fix it.`
        : mode === "tmux"
          ? `${entry.name} needs tmux ${MIN_TMUX_VERSION} or newer on ${host.name}. Install or upgrade tmux so new sessions can use ${entry.name}.`
          : mode === "update"
            ? `${entry.name} is out of date on ${host.name}. Update it so new sessions can use ${entry.name}.`
            : `${entry.name} isn’t set up on ${host.name}, so new sessions can’t use ${entry.name}.`;
  const nativeId = catalogHarness(entry.harness);
  const detail = nativeId ? HARNESS_DETAIL_ENTRIES.entry(nativeId) : undefined;
  const fixKey = fixActionKey(mode, installable);
  const actionLabel =
    fixKey === undefined ? "Set up" : (detail?.nameAlternatives.fixAction[fixKey] ?? "Reconfigure");
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-ui">
      <div className="flex min-w-0 flex-1 items-start gap-3">
        <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-500" />
        <p>{copy}</p>
      </div>
      {canFix && reason !== "cli-unresponsive" && (
        <Button
          size="sm"
          {...(detail &&
            settingsControlProps(
              detail,
              "fixAction",
              fixKey === undefined ? undefined : { alternative: fixKey },
            ))}
          onClick={() => onFix(mode)}
        >
          {actionLabel}
        </Button>
      )}
    </div>
  );
}

/** The fix a setup warning's button names, by key; undefined is the plain "Set up". */
function fixActionKey(
  mode: HarnessFixMode,
  installable: boolean,
): HarnessFixActionName | undefined {
  if (mode === "tmux") return "tmux";
  if (!installable) return undefined;
  if (mode === "update") return "update";
  if (mode === "install") return "install";
  return "reconfigure";
}

function ReadyNotice({
  entry,
  host,
  version,
  account,
}: {
  entry: HarnessEntry;
  host: Host;
  version?: string;
  account?: HarnessAccount;
}) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-green-500/30 bg-green-500/10 p-4 text-ui text-green-700 dark:text-green-300">
      <CheckCircle2Icon className="size-4 shrink-0" aria-hidden />
      <p>
        {entry.name}
        {version ? ` ${version}` : ""} is ready on {host.name}.
        {account ? ` Signed in as ${accountDetails(account)}.` : " Checked just now."}
      </p>
    </div>
  );
}

function StartupSettings({ host, harness }: { host: Host; harness: string }) {
  const { data, error, isPending } = useHarnessStartup(host.host_id, harness);
  if (error instanceof ApiError && error.status === 404) return null;
  if (isPending) return <p className="text-ui text-muted-foreground">Loading launch settings…</p>;
  if (error || !data) {
    return (
      <p className="text-ui text-muted-foreground">
        {error instanceof ApiError && error.status === 501
          ? `Update ${host.name} to see launch settings.`
          : `Couldn't load launch settings from ${host.name}.`}
      </p>
    );
  }
  const source = {
    env: `From OMNIGENT_${harness.replace(/-native$/, "").toUpperCase()}_PATH on ${host.name}.`,
    config: `From harness.${harness} in ~/.omnigent/config.yaml on ${host.name}.`,
    default: `Default command on ${host.name}.`,
  }[data.command_source];
  return (
    <section className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
          Startup configuration
        </h2>
        <p className="text-xs text-muted-foreground">
          {source} Read-only host defaults, not a running session's full command or environment.
          Sessions and workspaces may override these.
        </p>
      </div>
      <div className="flex flex-col gap-2">
        <h3 className="text-ui font-medium">Command</h3>
        <code className="rounded-xl border border-border p-3 text-ui break-all">
          {data.resolved_path ?? data.command}
        </code>
        {!data.resolved_path && (
          <p className="text-xs text-muted-foreground">Executable not found.</p>
        )}
      </div>
      <div className="flex flex-col gap-2">
        <h3 className="text-ui font-medium">Environment</h3>
        {data.environment ? (
          <>
            {Object.keys(data.environment.variables).length > 0 ? (
              <dl className="flex flex-col gap-2 rounded-xl border border-border p-3 text-ui">
                {Object.entries(data.environment.variables).map(([name, value]) => (
                  <div key={name} className="flex flex-wrap items-baseline gap-x-3">
                    <dt className="font-mono break-all">{name}</dt>
                    <dd>
                      <code className="whitespace-pre-wrap break-all">{value || '""'}</code>
                    </dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="rounded-xl border border-border p-3 text-ui">None configured</p>
            )}
            <p className="text-xs text-muted-foreground">
              {data.environment.inherit
                ? "Inherited values are not listed."
                : "Inherited environment cleared."}
            </p>
            {data.environment.unset.length > 0 && (
              <p className="text-xs text-muted-foreground">
                Removed before applying overrides: <code>{data.environment.unset.join(", ")}</code>.
              </p>
            )}
          </>
        ) : (
          <p className="text-ui text-muted-foreground">
            {data.configured_args == null
              ? `Update ${host.name} to see environment settings.`
              : "Cannot separate environment values for this env wrapper. Command and arguments are shown unchanged."}
          </p>
        )}
      </div>
      <div className="flex flex-col gap-2">
        <h3 className="text-ui font-medium">Arguments</h3>
        {data.args == null ? (
          <p className="rounded-xl border border-border p-3 text-ui">
            {data.arg_count === 0
              ? "None configured"
              : `${data.arg_count} configured argument${data.arg_count === 1 ? "" : "s"}. Update ${host.name} to view values.`}
          </p>
        ) : (
          <StartupArguments args={data.args} />
        )}
      </div>
    </section>
  );
}

function StartupArguments({ args }: { args: string[] }) {
  if (args.length === 0)
    return <p className="rounded-xl border border-border p-3 text-ui">None configured</p>;
  return (
    <ol className="flex list-decimal flex-col gap-1 rounded-xl border border-border py-3 pr-3 pl-9 text-ui">
      {args.map((arg, index) => (
        // eslint-disable-next-line react/no-array-index-key
        <li key={index}>
          <code className="whitespace-pre-wrap break-all">{arg || '""'}</code>
        </li>
      ))}
    </ol>
  );
}

/** Where the harness's login comes from. */
function CredentialCard({
  label,
  description,
  ready,
  warning,
  showStatus,
  statusLabel,
  action,
}: {
  label: string;
  description: string;
  ready: boolean;
  warning: boolean;
  showStatus: boolean;
  statusLabel: string;
  action?: { label: string; onClick: () => void; control?: SettingsControlProps };
}) {
  const body = (
    <>
      <span
        className={cn(
          "flex size-8 shrink-0 items-center justify-center rounded-lg",
          warning ? "bg-amber-500/15" : "bg-muted",
        )}
      >
        <KeyRoundIcon
          className={cn(
            "size-4",
            warning ? "text-amber-600 dark:text-amber-500" : "text-muted-foreground",
          )}
        />
      </span>
      <span className="flex min-w-0 flex-1 flex-col text-left">
        <span
          className={cn("text-ui font-medium", warning && "text-amber-700 dark:text-amber-300")}
        >
          {warning && <TriangleAlertIcon className="mr-1 inline size-3.5" aria-hidden />}
          {label}
        </span>
        <span className="text-xs text-muted-foreground">{description}</span>
      </span>
      {action ? (
        <span className="inline-flex h-7 shrink-0 items-center justify-center rounded-[var(--radius-md)] bg-primary px-2.5 text-ui text-primary-foreground">
          {action.label}
        </span>
      ) : showStatus ? (
        <span
          className={cn(
            "text-xs",
            ready ? "text-green-600 dark:text-green-400" : "text-amber-600 dark:text-amber-500",
          )}
        >
          {ready ? "Configured" : statusLabel}
        </span>
      ) : null}
    </>
  );
  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Sign-in</h2>
      {action ? (
        <button
          type="button"
          {...action.control}
          className={cn(
            "flex w-full cursor-pointer items-center gap-3 rounded-xl border p-3 text-left transition-colors focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none",
            warning
              ? "border-amber-500/30 bg-amber-500/10 hover:bg-amber-500/15"
              : "border-border hover:bg-muted/50",
          )}
          onClick={action.onClick}
        >
          {body}
        </button>
      ) : (
        <div
          className={cn(
            "flex items-center gap-3 rounded-xl border p-3",
            warning ? "border-amber-500/30 bg-amber-500/10" : "border-border",
          )}
        >
          {body}
        </div>
      )}
    </section>
  );
}
