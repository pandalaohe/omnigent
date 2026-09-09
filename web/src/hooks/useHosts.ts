import { useMutation, useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type { NativeModelOption } from "@/lib/types";

export interface Host {
  host_id: string;
  name: string;
  owner: string;
  status: "online" | "offline";
  /**
   * Sandbox provider backing a server-managed host (e.g. "modal");
   * null for user-connected hosts. Optional because older servers
   * omit the field entirely.
   */
  sandbox_provider?: string | null;
  /**
   * Per-harness readiness reported by the host's last connect, e.g.
   * `{"claude-sdk": true, "codex": "needs-auth"}`. `null`/absent means the
   * host has never reported it (older host build) — unknown, never
   * "nothing configured".
   */
  configured_harnesses?: Record<string, boolean | string> | null;
  /**
   * Whether each harness family's launch on this host resolves an
   * AI-Gateway-backed inference config, e.g. `{"claude-native": true,
   * "codex": false}`. Smart Routing's apply layer only works on gateway-backed
   * inference. `null`/absent (or a missing key) means unknown — an older host
   * or server — and must not gate anything away; only an explicit `false` does.
   */
  gateway_inference?: Record<string, boolean> | null;
  /** Host-native directory opened first for new sessions on this machine. */
  default_workspace?: string | null;
  /** Whether the connected Host can enumerate platform filesystem roots. */
  filesystem_roots?: boolean;
}

export interface CodexRateLimitWindow {
  kind: "primary" | "secondary";
  used_percent: number;
  window_duration_mins: number;
  resets_at?: number;
}

export interface CodexRateLimitBucket {
  limit_id: string;
  limit_name?: string;
  windows: CodexRateLimitWindow[];
}

export interface CodexRateLimitsSnapshot {
  captured_at: number;
  limits: CodexRateLimitBucket[];
}

const MAX_CODEX_RATE_LIMIT_BUCKETS = 16;
const MAX_CODEX_RATE_LIMIT_WINDOWS = 2;
const MAX_CODEX_RATE_LIMIT_TEXT_LENGTH = 128;
const MAX_CODEX_RATE_LIMIT_WINDOW_MINS = 5 * 525_600;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSafePositiveInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) > 0;
}

/** Rebuild a bounded snapshot from untrusted REST JSON, or hide it. */
export interface CliRetentionPolicy {
  version: 1;
  idle_threshold_minutes: number;
  max_idle_clis: number | null;
  close_on_archive: boolean;
}

export type CliRetentionApplicationStatus =
  | "legacy"
  | "host_offline"
  | "other_replica"
  | "pending"
  | "unknown"
  | "partial"
  | "unsupported"
  | "applied";

export interface CliRetentionApplication {
  status: CliRetentionApplicationStatus;
  policy_revision: number;
  observed_at: number | null;
  bound?: number | null;
  supported?: number | null;
  absent?: number | null;
  unsupported?: number | null;
  unknown?: number | null;
}

export interface CliRetentionFamilyStats {
  idle: number;
  active: number;
  below_threshold: number;
  total: number;
}

export interface CliRetentionRuntime {
  configured: boolean;
  owner?: boolean | null;
  policy_revision?: number | null;
  observed_at?: number | null;
  application?: CliRetentionApplication | null;
  families: Record<string, CliRetentionFamilyStats>;
  scheduled?: string[] | null;
  released?: string[] | null;
  reset?: string[] | null;
  unavailable?: string[] | null;
  lease?: "busy" | null;
}

export interface HostCliRetentionResponse {
  contract_version: 1;
  configured: boolean;
  revision: number;
  policy: CliRetentionPolicy;
  runtime: CliRetentionRuntime | null;
  application: CliRetentionApplication;
}

export type CliRetentionRequestFailure =
  "conflict" | "busy" | "host_offline" | "other_replica" | "unknown";

export class CliRetentionRequestError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly failure: CliRetentionRequestFailure;

  constructor({ status, code, message }: { status: number; code: string | null; message: string }) {
    super(message);
    this.name = "CliRetentionRequestError";
    this.status = status;
    this.code = code;
    const normalized = message.toLowerCase();
    this.failure =
      code === "wrong_replica" || normalized.includes("another replica")
        ? "other_replica"
        : normalized.includes("host is offline") || normalized.includes("host offline")
          ? "host_offline"
          : normalized.includes("currently reconciling")
            ? "busy"
            : status === 409
              ? "conflict"
              : "unknown";
  }
}

export function parseCodexRateLimitsSnapshot(value: unknown): CodexRateLimitsSnapshot | null {
  if (!isRecord(value) || !isSafePositiveInteger(value.captured_at)) return null;
  if (
    !Array.isArray(value.limits) ||
    value.limits.length < 1 ||
    value.limits.length > MAX_CODEX_RATE_LIMIT_BUCKETS
  ) {
    return null;
  }

  const limits: CodexRateLimitBucket[] = [];
  for (const rawBucket of value.limits) {
    if (!isRecord(rawBucket)) return null;
    const { limit_id: limitId, limit_name: limitName } = rawBucket;
    if (
      typeof limitId !== "string" ||
      limitId.length < 1 ||
      limitId.length > MAX_CODEX_RATE_LIMIT_TEXT_LENGTH ||
      limitId.trim() !== limitId
    ) {
      return null;
    }
    if (
      limitName !== undefined &&
      (typeof limitName !== "string" ||
        limitName.length < 1 ||
        limitName.length > MAX_CODEX_RATE_LIMIT_TEXT_LENGTH ||
        limitName.trim() !== limitName)
    ) {
      return null;
    }
    if (
      !Array.isArray(rawBucket.windows) ||
      rawBucket.windows.length < 1 ||
      rawBucket.windows.length > MAX_CODEX_RATE_LIMIT_WINDOWS
    ) {
      return null;
    }

    const windows: CodexRateLimitWindow[] = [];
    for (const rawWindow of rawBucket.windows) {
      if (!isRecord(rawWindow)) return null;
      const { kind, used_percent: usedPercent, window_duration_mins: duration } = rawWindow;
      if (kind !== "primary" && kind !== "secondary") return null;
      if (
        typeof usedPercent !== "number" ||
        !Number.isFinite(usedPercent) ||
        usedPercent < 0 ||
        usedPercent > 100
      ) {
        return null;
      }
      if (
        !Number.isInteger(duration) ||
        (duration as number) < 1 ||
        (duration as number) > MAX_CODEX_RATE_LIMIT_WINDOW_MINS
      ) {
        return null;
      }
      const resetsAt = rawWindow.resets_at;
      if (resetsAt !== undefined && !isSafePositiveInteger(resetsAt)) return null;
      windows.push({
        kind,
        used_percent: usedPercent,
        window_duration_mins: duration as number,
        ...(resetsAt !== undefined ? { resets_at: resetsAt } : {}),
      });
    }
    limits.push({
      limit_id: limitId,
      ...(limitName !== undefined ? { limit_name: limitName } : {}),
      windows,
    });
  }
  return { captured_at: value.captured_at, limits };
}

interface HostsResponse {
  hosts: Host[];
}

export async function fetchHosts(includeSandbox: boolean): Promise<Host[]> {
  const res = await authenticatedFetch("/v1/hosts");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as HostsResponse;
  // Hide server-managed sandbox hosts from every host picker: they
  // are launch targets the server creates on demand (and relaunches
  // at will), not user-connectable machines, so offering them as
  // manual targets is misleading. Hosts from older servers lack the
  // field and are kept. `includeSandbox` opts a caller (the chat-header
  // HostBadge) back into seeing them so it can label sandbox sessions.
  if (includeSandbox) return body.hosts;
  return body.hosts.filter((h) => !h.sandbox_provider);
}

interface UseHostsOptions {
  enabled?: boolean;
  includeSandbox?: boolean;
  /** Refetch on every window refocus (paired with `staleTime: 0`) so returning
   *  to the tab is a guaranteed readiness recovery. Only the setup flow needs
   *  this; other consumers keep the 30 s stale window to avoid an app-wide bump
   *  in `/v1/hosts` volume on refocus. */
  refetchOnFocus?: boolean;
}

export function useHosts(options: UseHostsOptions = {}) {
  const enabled = options.enabled ?? true;
  const includeSandbox = options.includeSandbox ?? false;
  const refetchOnFocus = options.refetchOnFocus ?? false;
  return useQuery({
    // Distinct cache key per filtering mode so the picker's filtered
    // list and the header's unfiltered list don't overwrite each other.
    // A bare ["hosts"] invalidation still prefix-matches both.
    queryKey: ["hosts", { includeSandbox }],
    queryFn: () => fetchHosts(includeSandbox),
    enabled,
    // Readiness is pushed live via WS (hosts_changed → invalidate in
    // SessionUpdatesProvider), so the badge normally clears within seconds of
    // `omni setup` finishing. The refocus recovery settles the case that push
    // misses: a user typically runs setup in a terminal with the tab
    // backgrounded, which pauses the interval poll AND is when a reconnect gap
    // can drop the frame. Refetching on refocus makes returning to the tab a
    // guaranteed recovery — paired with staleTime 0 so refocus always refires
    // rather than serving a stale "needs setup" from cache. Scoped to the setup
    // flow via `refetchOnFocus` so the other ~8 consumers don't pay it. The 60 s
    // interval remains the in-tab fallback.
    staleTime: refetchOnFocus ? 0 : 30_000,
    refetchOnWindowFocus: refetchOnFocus,
    refetchInterval: enabled ? 60_000 : false,
  });
}

export const hostCliRetentionQueryKey = (hostId: string | null) =>
  ["host-cli-retention", hostId] as const;

async function cliRetentionError(res: Response): Promise<CliRetentionRequestError> {
  let message = `${res.status} ${res.statusText}`.trim();
  let code: string | null = null;
  try {
    const body = (await res.json()) as {
      detail?: string;
      error?: { code?: string; message?: string };
    };
    if (typeof body.detail === "string" && body.detail) message = body.detail;
    if (typeof body.error?.message === "string" && body.error.message) {
      message = body.error.message;
    }
    if (typeof body.error?.code === "string" && body.error.code) code = body.error.code;
  } catch {
    // Keep the HTTP status line when an intermediary returns a non-JSON body.
  }
  return new CliRetentionRequestError({ status: res.status, code, message });
}

async function fetchHostCliRetention(hostId: string): Promise<HostCliRetentionResponse> {
  const res = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}/cli-retention`);
  if (!res.ok) throw await cliRetentionError(res);
  return (await res.json()) as HostCliRetentionResponse;
}

/** Effective idle-CLI policy plus the latest Runner application snapshot for one Host. */
export function useHostCliRetention(hostId: string | null, enabled = true) {
  return useQuery({
    queryKey: hostCliRetentionQueryKey(hostId),
    queryFn: () => fetchHostCliRetention(hostId as string),
    enabled: enabled && hostId !== null,
    staleTime: 5_000,
    refetchInterval: enabled && hostId !== null ? 5_000 : false,
    refetchOnWindowFocus: true,
    retry: false,
  });
}

interface ReplaceHostCliRetentionInput {
  expected_revision: number;
  policy: CliRetentionPolicy;
}

/** CAS-replace one Host's policy and refresh its runtime projection. */
export function useReplaceHostCliRetention(hostId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: ReplaceHostCliRetentionInput): Promise<HostCliRetentionResponse> => {
      if (hostId === null) throw new Error("Select a Host before saving.");
      const res = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId)}/cli-retention`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(input),
        },
      );
      if (!res.ok) throw await cliRetentionError(res);
      return (await res.json()) as HostCliRetentionResponse;
    },
    onSuccess: (response) => {
      queryClient.setQueryData(hostCliRetentionQueryKey(hostId), response);
      void queryClient.invalidateQueries({ queryKey: hostCliRetentionQueryKey(hostId) });
    },
  });
}

/** CAS-delete one Host's override so its existing layered lifecycle rules resume. */
export function useResetHostCliRetention(hostId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (expectedRevision: number): Promise<HostCliRetentionResponse> => {
      if (hostId === null) throw new Error("Select a Host before restoring legacy rules.");
      const res = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId)}/cli-retention`,
        {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ expected_revision: expectedRevision }),
        },
      );
      if (!res.ok) throw await cliRetentionError(res);
      return (await res.json()) as HostCliRetentionResponse;
    },
    onSuccess: (response) => {
      queryClient.setQueryData(hostCliRetentionQueryKey(hostId), response);
      void queryClient.invalidateQueries({ queryKey: hostCliRetentionQueryKey(hostId) });
    },
  });
}

async function fetchCodexRateLimits(hostId: string): Promise<CodexRateLimitsSnapshot | null> {
  const res = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}/codex-rate-limits`);
  // Older servers and removed/offline Hosts have no usable snapshot. This is
  // an optional status indicator, so degrade by hiding it instead of surfacing
  // an unrelated chat-page error.
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  try {
    const body: unknown = await res.json();
    return isRecord(body) ? parseCodexRateLimitsSnapshot(body.rate_limits) : null;
  } catch {
    return null;
  }
}

/** Sanitized Codex subscription quota windows reported by one connected Host. */
export function useCodexRateLimits(hostId: string | null, enabled = true) {
  return useQuery({
    queryKey: ["host-codex-rate-limits", hostId],
    queryFn: () => fetchCodexRateLimits(hostId as string),
    enabled: enabled && hostId !== null,
    staleTime: 30_000,
    refetchInterval: enabled && hostId !== null ? 60_000 : false,
    refetchOnWindowFocus: true,
    retry: false,
  });
}

/** Persist or clear the default starting workspace for one physical host. */
export async function setHostDefaultWorkspace(
  hostId: string,
  defaultWorkspace: string | null,
): Promise<void> {
  const res = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ default_workspace: defaultWorkspace }),
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `Couldn't save the default folder (HTTP ${res.status}).`);
  }
}

async function fetchHostModelOptions(
  hostId: string,
  harness: string,
): Promise<NativeModelOption[]> {
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/harnesses/${encodeURIComponent(harness)}/model-options`,
  );
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string" && body.detail) detail = body.detail;
    } catch {
      // Non-JSON error body — keep the status-line detail.
    }
    throw new Error(detail);
  }
  const body = (await res.json()) as { models?: NativeModelOption[]; error?: string };
  const models = body.models ?? [];
  // Backward compatibility with servers that encoded probe failure in a 200.
  if (models.length === 0 && body.error) throw new Error(body.error);
  return models;
}

/** Model choices available before launch, resolved on the selected host. */
export function useHostModelOptions(hostId: string | null, harness: string, enabled = true) {
  return useQuery({
    queryKey: ["host-model-options", hostId, harness],
    queryFn: () => fetchHostModelOptions(hostId as string, harness),
    enabled: enabled && hostId !== null,
    // The host's provider can change underneath an open picker (`omni setup`
    // re-pointing the Claude default): poll while mounted so the list follows
    // the host's current catalog, which it re-resolves on every request.
    staleTime: 15_000,
    refetchInterval: enabled && hostId !== null ? 15_000 : false,
    // A request racing the host's boot probe gets a structured failure;
    // the probe itself completes shortly after
    // (single-flight in the host's catalog store). Retry with backoff so a
    // picker opened during that warm-up window fills in instead of pinning
    // the transient error until reopen. A genuinely failing probe still
    // surfaces its error once the retries exhaust (~22 s).
    retry: 6,
    retryDelay: (attempt) => Math.min(5_000, 1_000 * 2 ** attempt),
  });
}

interface InstallHarnessResult {
  object: "harness_install";
  harness: string;
  configured_harnesses: Record<string, boolean | string>;
}

/**
 * Install a missing harness onto a connected host from the UI.
 *
 * POSTs to the flag-gated install endpoint; the server drives the same
 * installer `omni setup` uses and returns the host's refreshed readiness.
 * On success we write that map straight into every cached host list so the
 * "needs setup" badge flips to ready without waiting for the 60 s poll or a
 * reconnect. The caller passes the harness id (e.g. `"codex"`); only ids in the
 * server's `installable_harnesses` set should be offered (see
 * `harnessInstallableOnHost`).
 *
 * Concurrent installs of different harnesses are supported: each `mutate()`
 * call runs independently, and callers track per-harness in-flight state via
 * the call's own `onSettled` (see `HarnessSetupDialog`) rather than the shared
 * observer's `isPending`, which only reflects the latest call.
 */
/** Stable mutation key for harness-install mutations on a host. Lets the setup
 *  dialog read per-harness in-flight state via {@link useInstallingHarnesses}
 *  regardless of which install fired last (a shared observer only remembers the
 *  latest call's callbacks — see the comment in {@link useInstallHarness}). */
export function installHarnessMutationKey(hostId: string): readonly unknown[] {
  return ["install-harness", hostId];
}

export function useInstallHarness(hostId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: installHarnessMutationKey(hostId),
    mutationFn: async (harness: string): Promise<InstallHarnessResult> => {
      const res = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId)}/harnesses/${encodeURIComponent(harness)}/install`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
      );
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const err = (await res.json()) as { detail?: string };
          if (typeof err.detail === "string" && err.detail) detail = err.detail;
        } catch {
          // Non-JSON error body — keep the status-line detail.
        }
        throw new Error(detail);
      }
      return (await res.json()) as InstallHarnessResult;
    },
    onSuccess: (result) => {
      // Patch the refreshed readiness into every ["hosts", …] cache entry
      // (filtered + unfiltered) so the badge updates immediately. This lives at
      // config level (not the per-call mutate() options) on purpose: config
      // callbacks fire per-mutation from the Mutation object, so a concurrent
      // second install can't orphan the first one's cache patch — unlike the
      // observer's per-call callbacks, which the later mutate() overwrites.
      queryClient.setQueriesData<Host[]>({ queryKey: ["hosts"] }, (hosts) =>
        hosts?.map((h) =>
          h.host_id === hostId ? { ...h, configured_harnesses: result.configured_harnesses } : h,
        ),
      );
    },
  });
}

/**
 * The set of harness ids with an install currently in flight on *hostId*.
 *
 * Reads React Query's global mutation state (filtered to this host's install
 * mutations), so it reflects EVERY pending install regardless of which one
 * fired last. The setup dialog is a single persistent instance sharing one
 * install mutation observer; that observer only remembers the latest call's
 * per-call callbacks, so tracking in-flight installs via a local set fed by
 * mutate()'s onSettled loses any earlier install when a second one starts —
 * leaving the first harness stuck showing "Installing…" forever. Deriving the
 * set from mutation state instead is observer-independent and self-heals.
 */
export function useInstallingHarnesses(hostId: string): ReadonlySet<string> {
  const pending = useMutationState({
    filters: { mutationKey: installHarnessMutationKey(hostId), status: "pending" },
    select: (mutation) => mutation.state.variables as string | undefined,
  });
  return new Set(pending.filter((h): h is string => typeof h === "string"));
}

/** Payload for {@link useStoreCredential}: an API key, a gateway, or adopt. */
export interface StoreCredentialInput {
  harness: string;
  kind: "key" | "gateway" | "adopt";
  /** The API key / gateway token for `key` / `gateway`; omitted for `adopt`. */
  secret?: string;
  /** Gateway base URL (required for `kind: "gateway"`). */
  base_url?: string;
  /** Family default model id to pin. Accepted by the backend but not yet
   *  surfaced by the v1 form — reserved for a follow-up. */
  default_model?: string;
  /** OpenAI wire protocol (`"chat"` / `"responses"`), gateway/key openai only.
   *  Reserved for a follow-up like {@link default_model}. */
  wire_api?: string;
  /** For `kind: "adopt"`, the host env var to reference. */
  env_var?: string;
}

interface StoreCredentialResult {
  object: "harness_credential";
  harness: string;
  configured_harnesses: Record<string, boolean | string>;
}

/**
 * Write a harness provider credential onto a connected host from the UI.
 *
 * POSTs to the flag-gated credential endpoint; the server forwards the secret
 * to the host daemon, which writes it (keychain + a `providers:` reference) and
 * returns the host's refreshed readiness. On success we patch that map into
 * every cached host list so the harness badge flips (yellow → green) without a
 * reconnect. The secret rides in the request body and is never held server-side.
 */
export function useStoreCredential(hostId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: StoreCredentialInput): Promise<StoreCredentialResult> => {
      const { harness, ...body } = input;
      const res = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId)}/harnesses/${encodeURIComponent(harness)}/credential`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const err = (await res.json()) as { detail?: string };
          if (typeof err.detail === "string" && err.detail) detail = err.detail;
        } catch {
          // Non-JSON error body — keep the status-line detail.
        }
        throw new Error(detail);
      }
      return (await res.json()) as StoreCredentialResult;
    },
    // This mutation-level onSuccess patches the ["hosts"] cache (badge flip) and
    // invalidates the detect query so a just-adopted credential stops showing.
    // Callers may ALSO pass a call-level onSuccess (toast + close the form);
    // react-query fires both — don't consolidate them, or the cache patch here
    // is lost.
    onSuccess: (result) => {
      queryClient.setQueriesData<Host[]>({ queryKey: ["hosts"] }, (hosts) =>
        hosts?.map((h) =>
          h.host_id === hostId ? { ...h, configured_harnesses: result.configured_harnesses } : h,
        ),
      );
      // A written/adopted credential changes what's adoptable — refetch it.
      void queryClient.invalidateQueries({ queryKey: ["detected-credentials", hostId] });
    },
  });
}

/** A credential already on the host, offered for one-click adopt (non-secret). */
export interface DetectedCredential {
  family: string;
  source: string;
  env_var: string | null;
}

/**
 * Fetch the credentials already present on a host, for the adopt affordance.
 *
 * Hits the flag-gated detect endpoint; the server asks the host daemon for
 * NON-secret descriptors (family + source label + env var name) of adoptable
 * credentials. Enabled only when a host id is given and `enabled` is set (the
 * dialog turns it on only for a harness whose credential the UI can write), so
 * we don't probe hosts for closed dialogs.
 */
export function useDetectedCredentials(hostId: string | null | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["detected-credentials", hostId],
    queryFn: async (): Promise<DetectedCredential[]> => {
      const res = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId ?? "")}/credentials/detected`,
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const body = (await res.json()) as { credentials?: DetectedCredential[] };
      return Array.isArray(body.credentials) ? body.credentials : [];
    },
    enabled: enabled && !!hostId,
    staleTime: 30_000,
  });
}
