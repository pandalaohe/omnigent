// Auto-generated Omnigent bridge extension for native Pi sessions.
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const permissionToolSources = require("./permission_tool_registry.cjs");
let permissionToolRegistry = null;

// Tuning for the TOOL_CALL policy long-poll (see evalNativePolicyHttp).
//
// The server's POST /policies/evaluate parks an ASK verdict server-side
// (URL-based elicitation) and holds the connection open until a human
// resolves it via the web UI, then returns a hard ALLOW/DENY — the same
// contract the Claude Code / Codex / Cursor native hooks rely on
// (omnigent.native_policy_hook.post_evaluate_with_retry, ~1-day client
// read budget). Node's global fetch (undici) caps a connection that
// receives no response headers at headersTimeout (~5 min by default), so
// a long human wait would sever the park mid-flight. We therefore bound
// each attempt with an AbortController well under that cap and, on the
// resulting abort (or a transient 5xx / connect error), re-POST the SAME
// _omnigent_elicitation_id so the server RE-ATTACHES to the existing
// parked elicitation instead of publishing a second approval card. This
// mirrors the re-attach idiom in post_evaluate_with_retry while staying
// resilient to undici's header timeout.
//
// _PARK_ATTEMPT_TIMEOUT_MS — per-request abort budget; kept under undici's
//   ~300s headersTimeout default so a parked request is retried (re-attach)
//   rather than failing as UND_ERR_HEADERS_TIMEOUT.
// _PARK_TOTAL_BUDGET_MS — overall ceiling on the park loop. The human-wait
//   itself is capped server-side by the deciding policy's ask_timeout, so
//   this is a long backstop that keeps a wedged client from waiting forever.
const _PARK_ATTEMPT_TIMEOUT_MS = 240_000;
const _PARK_TOTAL_BUDGET_MS = 86_400_000;
// Budget for retrying genuinely transient transport errors (connect
// refused / reset / 5xx) before failing CLOSED. Distinct from the long park
// budget: a genuine outage ends here, but the window matches the Python
// TOOL_CALL_POLICY_RETRY_BUDGET_S / _TOOL_CALL_POLICY_RETRY_MAX_BACKOFF_S in
// omnigent/native/native_policy_hook.py — this extension only ever evaluates
// PHASE_TOOL_CALL, so it gets the long tool-call budget (a few minutes rides
// out a DNS/server blip rather than failing an in-flight call closed).
const _TRANSIENT_RETRY_BUDGET_MS = 300_000;
const _TRANSIENT_RETRY_INITIAL_BACKOFF_MS = 1_000;
const _TRANSIENT_RETRY_MAX_BACKOFF_MS = 20_000;
// A genuine connect error (refused / reset) throws fast — well under the
// per-attempt park timeout. A legitimate long-poll abort only throws once
// our own _PARK_ATTEMPT_TIMEOUT_MS timer fires (the server held the
// connection open the whole time). We use the attempt's elapsed wall-time
// to disambiguate the two even when controller.signal.aborted has already
// flipped true (the abort-timer race): only an attempt that
// survived to ~the per-attempt timeout is treated as a re-attachable park;
// anything that failed materially sooner is a genuine transport error and
// is charged against the transient budget (→ eventually fail CLOSED).
const _PARK_REATTACH_MIN_ELAPSED_MS = _PARK_ATTEMPT_TIMEOUT_MS - 5_000;
// Cap on consecutive raw POLICY_ACTION_ASK rounds that never collapse to a
// hard verdict. A writable session's ASK is resolved server-side (the gate
// parks and returns ALLOW/DENY); a *raw* ASK that keeps coming back means the
// gate cannot be satisfied here (e.g. a read-only caller). Mirror the Python
// native hook, which fails a stray ASK CLOSED rather than deferring — we cap
// the rounds and then deny, instead of riding the 24h park ceiling to a
// fail-open. The brief inter-round sleep means this bounds wall-time too.
const _MAX_RAW_ASK_ROUNDS = 50;
// Reason surfaced when the tool-call gate cannot obtain a usable verdict and
// fails CLOSED. PHASE_TOOL_CALL is the sole enforcement point for a native
// connector tool, so an unevaluable policy must block, not proceed — matching
// omnigent.policies.types.FAIL_CLOSED_PHASES and the Python native hook's
// fail_closed_hook_output(PreToolUse) → deny.
const _FAIL_CLOSED_REASON =
  "blocked: Omnigent policy server unreachable — failing closed (PHASE_TOOL_CALL)";

function failClosedVerdict() {
  return { block: true, reason: _FAIL_CLOSED_REASON };
}

function mintEvaluateElicitationId() {
  // Namespaced + 32 lowercase hex chars to satisfy the server's
  // _EVALUATE_HOOK_ELICITATION_ID_RE (^elicit_evaluate_[0-9a-f]{32}$).
  return `elicit_evaluate_${crypto.randomBytes(16).toString("hex")}`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function readConfig() {
  const configPath = process.env.OMNIGENT_PI_NATIVE_CONFIG;
  if (!configPath) return null;
  try {
    return JSON.parse(fs.readFileSync(configPath, "utf8"));
  } catch (_err) {
    return null;
  }
}

function writeConfig(config) {
  const configPath = process.env.OMNIGENT_PI_NATIVE_CONFIG;
  if (!configPath) throw new Error("Pi bridge config is unavailable");
  const current = readConfig() || {};
  const temporary = `${configPath}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify({
    ...current, ...config, authHeaders: current.authHeaders || config.authHeaders,
  }), { mode: 0o600 });
  fs.renameSync(temporary, configPath);
}

async function rotationRequest(config, method, resource, body) {
  const response = await fetch(`${config.serverUrl}${resource}`, {
    method,
    headers: headers(config),
    signal: AbortSignal.timeout(30000),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response || response.status < 200 || response.status >= 300)
    throw new Error(`Pi session rotation ${method} ${resource} returned ${response && response.status}`);
  return response.status === 204 ? null : response.json();
}

async function rotateNativeSession(config) {
  const oldId = config.pendingRotationSource || config.sessionId;
  config.pendingRotationSource = oldId;
  writeConfig(config);
  const oldPath = `/v1/sessions/${encodeURIComponent(oldId)}`;
  const old = await rotationRequest(config, "GET", oldPath);
  if (old && old.parent_session_id) {
    config.pendingRotationSource = null;
    config.pendingSuccessorId = null;
    writeConfig(config);
    return false;
  }
  if (!old || typeof old.runner_id !== "string" || !old.runner_id)
    throw new Error("Pi session rotation source has no runner");
  const created = await rotationRequest(config, "POST", `${oldPath}/successor`);
  const successorId = created && created.id;
  if (typeof successorId !== "string" || !successorId)
    throw new Error("Pi successor preparation returned no session id");
  config.pendingSuccessorId = successorId;
  writeConfig(config);
  const successorPath = `/v1/sessions/${encodeURIComponent(successorId)}`;
  await rotationRequest(config, "PATCH", successorPath, { runner_id: old.runner_id });
  await rotationRequest(config, "POST", `${successorPath}/resources/terminals`, {
    terminal: "pi", session_key: "main", ensure_native_terminal: true,
  });
  const succession = await rotationRequest(config, "POST", `${oldPath}/succession`, {
    target_session_id: successorId,
    allow_empty: true,
  });
  if (!succession || succession.status !== "done")
    throw new Error("Pi session succession has not finished");
  // The successor opens in its own bridge and terminal. Retire the old pane
  // only after its opening was accepted, so no live pane keeps an archived ID.
  config.pendingRotationSource = null;
  config.pendingSuccessorId = null;
  writeConfig(config);
  await rotationRequest(config, "DELETE", `${oldPath}/resources/terminals/terminal_pi_main`);
  return successorId;
}

const PERMISSION_TIERS = new Set(["ask", "plan", "accept-edits", "auto", "full"]);

function readPermissionState(config) {
  const configPath = process.env.OMNIGENT_PI_NATIVE_CONFIG;
  if (!config || typeof configPath !== "string" || !configPath ||
      typeof config.sessionId !== "string" || !config.sessionId) return null;
  const statePath = path.join(path.dirname(configPath), "permission_state.json");
  try {
    if (fs.lstatSync(statePath).isSymbolicLink()) return null;
    const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
    if (!state || state.session_id !== config.sessionId ||
        typeof state.host_id !== "string" || !state.host_id ||
        typeof state.workspace !== "string" || !state.workspace ||
        !PERMISSION_TIERS.has(state.tier) ||
        !["ready", "switching", "blocked"].includes(state.status) ||
        !Number.isInteger(state.revision) || state.revision < 1) return null;
    return state;
  } catch (_err) {
    return null;
  }
}

function writePermissionControlFile(config, name, payload) {
  const configPath = process.env.OMNIGENT_PI_NATIVE_CONFIG;
  if (!config || !configPath) throw new Error("Pi permission config missing");
  const target = path.join(path.dirname(configPath), name);
  const temporary = `${target}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(payload), { mode: 0o600 });
  fs.renameSync(temporary, target);
}

function permissionExtensionOrderProven() {
  if (process.env.OMNIGENT_PI_PERMISSION_INLINE) {
    try {
      return fs.realpathSync(process.env.OMNIGENT_PI_PERMISSION_INLINE) ===
        fs.realpathSync(__filename);
    } catch (_err) { return false; }
  }
  const argv = process.argv;
  if (!argv.includes("--no-extensions") && !argv.includes("-ne")) return false;
  let lastExtension = null;
  for (let index = 0; index < argv.length - 1; index += 1) {
    if (argv[index] === "--extension" || argv[index] === "-e") {
      lastExtension = argv[index + 1];
    }
  }
  try {
    return !!lastExtension && fs.realpathSync(path.resolve(lastExtension)) ===
      fs.realpathSync(__filename);
  } catch (_err) {
    return false;
  }
}

function activeToolsForTier(pi, config, baselineTools, tier) {
  if (typeof pi.setActiveTools !== "function" ||
      typeof pi.getActiveTools !== "function" || !Array.isArray(baselineTools)) return null;
  const planTools = Array.isArray(config.planVisibleTools) ? config.planVisibleTools : null;
  if (!planTools || !planTools.every((name) => typeof name === "string")) return null;
  const desired = tier === "plan"
    ? baselineTools.filter((name) => planTools.includes(name))
    : baselineTools;
  try {
    pi.setActiveTools(desired);
    const actual = pi.getActiveTools();
    return Array.isArray(actual) && actual.length === desired.length &&
      actual.every((name, index) => name === desired[index]) ? actual : null;
  } catch (_err) {
    return null;
  }
}

function markPermissionInstallation(pi, config, state, activeTools) {
  if (!config || !state || !activeTools ||
      (state.tier !== "full" && !permissionExtensionOrderProven()) ||
      typeof config.permissionInstallationId !== "string" ||
      !config.permissionInstallationId) return false;
  try {
    const extensionSha256 = crypto.createHash("sha256")
      .update(fs.readFileSync(__filename)).digest("hex");
    if (state.tier !== "full") {
      permissionToolRegistry = permissionToolSources.capture(pi, {
        version: 1, installation_id: config.permissionInstallationId,
        session_id: state.session_id, harness: "pi-native", extension_sha256: extensionSha256,
      });
      writePermissionControlFile(config, "permission_tools.json", permissionToolRegistry);
    }
    writePermissionControlFile(config, "permission_installation.json", {
      version: 1,
      installation_id: config.permissionInstallationId,
      session_id: state.session_id,
      harness: "pi-native",
      extension_sha256: extensionSha256,
      tier: state.tier,
      revision: state.revision,
      tools_verified: true,
      active_tools: activeTools,
    });
    return true;
  } catch (_err) {
    return false;
  }
}

async function finishPermissionLease(config, callId, lease, result) {
  if (!config || typeof config.serverUrl !== "string" || !config.serverUrl ||
      typeof config.sessionId !== "string" || !config.sessionId ||
      typeof callId !== "string" || !callId ||
      !lease || !Number.isInteger(lease.revision) || lease.revision < 1 ||
      typeof fetch !== "function") return false;
  try {
    const relay = relayCredentials();
    const url = relay
      ? `${relay.url}/policies/evaluate`
      : `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}/policies/evaluate`;
    const response = await fetch(
      url,
      {
        method: "POST",
        headers: relay
          ? { "content-type": "application/json", authorization: `Bearer ${relay.token}` }
          : { "content-type": "application/json", "X-Omnigent-Permission-Tiers": "1",
              ...freshAuthHeaders(config.authHeaders) },
        body: JSON.stringify({ event: { type: "PHASE_TOOL_RESULT", target: lease.name,
          data: { name: lease.name, arguments: lease.args,
            result: { isError: !!(result && result.isError) }, call_id: callId,
            permission_tier_revision: lease.revision }, context: {} } }),
      },
    );
    if (!response || !response.ok) return false;
    const verdict = await response.json();
    const acknowledgement = verdict && verdict.permission_tier_finish;
    return !!acknowledgement && typeof acknowledgement === "object" &&
      acknowledgement.call_id === callId &&
      acknowledgement.revision === lease.revision &&
      acknowledgement.finished === true;
  } catch (_err) {
    return false;
  }
}

function nativeAuthFailure(message, ctx) {
  const authDir = process.env.OMNIGENT_PI_NATIVE_AUTH_DIR;
  const error = message && message.errorMessage;
  const provider =
    (message && message.provider) || (ctx && ctx.model && ctx.model.provider);
  if (!authDir || typeof provider !== "string" || typeof error !== "string")
    return null;
  // Match wrap_auth_command's provider key and Pi's resolver error wording.
  const id = crypto
    .createHash("sha256")
    .update(provider)
    .digest("hex")
    .slice(0, 16);
  const file = `auth-${id}.json`;
  if (
    !error.includes("Failed to resolve API key for provider") ||
    !error.includes(file)
  )
    return null;
  try {
    const resultPath = path.join(authDir, `${file}.result.json`);
    const result = JSON.parse(fs.readFileSync(resultPath, "utf8"));
    const age = Date.now() - result.at;
    if (
      result.piPid !== process.pid ||
      !Number.isFinite(age) ||
      age < 0 ||
      age > 30_000
    )
      return null;
    const reasons = {
      timeout: "timed out before Pi's ten-second deadline",
      failed_exit: "exited unsuccessfully",
      empty: "returned an empty token",
      invalid_output: "returned invalid UTF-8",
      output_limit: "exceeded the output limit",
      start_failed: "could not start",
      invalid_command: "has no command configured",
      cancelled: "was cancelled",
    };
    const reason = reasons[result.kind];
    if (!reason) return null;
    fs.unlinkSync(resultPath);
    return {
      code: `pi_auth_${result.kind}`,
      message: `Pi authentication helper ${reason}. Check the configured token command, then send another message to retry. Your session is preserved.`,
    };
  } catch (_) {
    return null;
  }
}

// Re-read authHeaders from config.json on each outbound request. The bearer
// baked at launch dies with the ~1h Databricks OAuth lifetime; the runner
// re-mints it into config.json each turn (PiNativeExecutor), so re-reading
// here keeps the policy/MCP/event POSTs authenticated instead of failing
// closed mid-session. Falls back to the closed-over headers when the re-read
// fails (a torn config) so a transient read can't drop auth.
function freshAuthHeaders(fallback) {
  const latest = readConfig();
  if (latest && latest.authHeaders && typeof latest.authHeaders === "object") {
    return latest.authHeaders;
  }
  return fallback || {};
}

// Return the relay URL and token from config.json when available. The runner
// writes relayUrl + relayToken into config.json after the tool relay starts,
// so policy POSTs can use the relay's non-expiring local token instead of a
// baked server bearer.
function relayCredentials() {
  const cfg = readConfig();
  if (
    cfg &&
    typeof cfg.relayUrl === "string" &&
    typeof cfg.relayToken === "string"
  ) {
    return { url: cfg.relayUrl, token: cfg.relayToken };
  }
  return null;
}

/**
 * Evaluate a TOOL_CALL policy for a native Pi tool via the Omnigent server's
 * session-level HTTP endpoint (POST /v1/sessions/{sessionId}/policies/evaluate).
 *
 * This is the same endpoint used by the Claude Code, Codex, and Cursor native
 * hooks. It does NOT require an active Omnigent turn context on the harness
 * side — the endpoint evaluates against the session's full policy set directly.
 *
 * Verdict handling (parity with the native hooks):
 *   - POLICY_ACTION_DENY  → block the Pi tool call with the policy reason.
 *   - POLICY_ACTION_ALLOW / UNSPECIFIED (the engine default when no policy
 *       matches) → proceed.
 *   - POLICY_ACTION_ASK   → the server resolves ASK by PARKING this request
 *       (URL-based elicitation: it publishes an approval card to the web UI
 *       and holds the connection until a human resolves it), then returns a
 *       hard ALLOW/DENY — so a writable session never observes a raw ASK here.
 *       The park is realized by a generous client read budget plus re-attach
 *       retries (see the _PARK_* tuning above): if undici severs a long park
 *       (headersTimeout) we re-POST the SAME _omnigent_elicitation_id so the
 *       server re-attaches to the existing elicitation instead of opening a
 *       second approval card. A LEGITIMATE long-poll re-attach (the server is
 *       reachable and still holding the connection) is the only case that may
 *       loop toward the long park ceiling. If a raw ASK ever does come back
 *       (e.g. a read-only caller that cannot park), we re-evaluate up to
 *       _MAX_RAW_ASK_ROUNDS and then fail CLOSED.
 *
 * Fail CLOSED (a block verdict) whenever a usable verdict cannot be obtained:
 * the transient-retry budget for a genuine transport/5xx error is exhausted,
 * a raw ASK never collapses within _MAX_RAW_ASK_ROUNDS, or the long park
 * ceiling is reached. PHASE_TOOL_CALL is the SOLE enforcement point for a
 * native connector tool — the call is never re-checked server-side — so an
 * unevaluable policy MUST block, not proceed. This matches
 * omnigent.policies.types.FAIL_CLOSED_PHASES and the Python native hook's
 * fail_closed_hook_output(PreToolUse) → deny. (pi-native is itself a sole
 * gate, exactly like the Claude / Codex native hooks; an eventually-allowing
 * human-approval gate would defeat its own purpose, so the earlier fail-open
 * posture was wrong here.) A reachable server long-polling a parked ASK is
 * the deliberate exception: that is the human approval window, not an outage,
 * and we keep waiting (re-attaching) rather than blocking.
 *
 * @returns {Promise<{block: boolean, reason: string}>} A verdict; missing
 *   configuration, transport failure and unknown actions block.
 */
async function evalNativePolicyHttp(config, toolName, args, callId) {
  if (
    !config ||
    typeof config.serverUrl !== "string" ||
    !config.serverUrl ||
    typeof config.sessionId !== "string" ||
    !config.sessionId ||
    typeof fetch !== "function"
  )
    return failClosedVerdict();
  if (
    typeof toolName !== "string" ||
    !toolName ||
    !args ||
    typeof args !== "object" ||
    Array.isArray(args) ||
    typeof callId !== "string" ||
    !callId
  ) {
    return failClosedVerdict();
  }
  const permissionState = readPermissionState(config);
  if (!permissionState || permissionState.status !== "ready") return failClosedVerdict();
  // Prefer relay (non-expiring local token); fall back to direct server call.
  const relay = relayCredentials();
  const url = relay
    ? `${relay.url}/policies/evaluate`
    : `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}/policies/evaluate`;
  // Mint one stable re-attach id for this tool call. Every (re)POST carries
  // it so a re-park lands on the SAME elicitation — no duplicate approval
  // card. Kept for the whole call, across both the park loop and any
  // transient-error retries.
  const elicitationId = mintEvaluateElicitationId();
  let body;
  try {
    body = JSON.stringify({
      event: {
        type: "PHASE_TOOL_CALL",
        target: "",
        data: { name: toolName, arguments: args, call_id: callId,
          expected_permission_tier_revision: permissionState.revision },
        context: {},
      },
      _omnigent_elicitation_id: elicitationId,
    });
  } catch (_err) {
    return failClosedVerdict();
  }
  const reqHeaders = relay
    ? {
        "content-type": "application/json",
        authorization: `Bearer ${relay.token}`,
      }
    : {
        "content-type": "application/json",
        "X-Omnigent-Permission-Tiers": "1",
        ...freshAuthHeaders(config.authHeaders),
      };

  const parkDeadline = Date.now() + _PARK_TOTAL_BUDGET_MS;
  // Independent transient-error budget so a server that is actually down
  // resolves quickly (fail CLOSED) instead of riding the long park ceiling.
  let transientDeadline = Date.now() + _TRANSIENT_RETRY_BUDGET_MS;
  let transientBackoff = _TRANSIENT_RETRY_INITIAL_BACKOFF_MS;
  // Bound on consecutive raw ASK rounds (see _MAX_RAW_ASK_ROUNDS).
  let rawAskRounds = 0;

  while (true) {
    const current = readPermissionState(config);
    if (!current || current.status !== "ready" ||
        current.revision !== permissionState.revision ||
        current.tier !== permissionState.tier) return failClosedVerdict();
    if (Date.now() >= parkDeadline) {
      // Park ceiling reached (well past any sane ask_timeout): the gate is
      // wedged, not resolving. Fail CLOSED — PHASE_TOOL_CALL is the sole
      // enforcement point, so a never-resolving gate must block, not proceed.
      return failClosedVerdict();
    }
    // AbortController bounds each attempt under undici's headersTimeout so a
    // long park is retried (re-attach) rather than thrown as a header
    // timeout. AbortSignal.timeout would be terser but is newer; this form
    // works on every Node that ships global fetch.
    const controller =
      typeof AbortController === "function" ? new AbortController() : null;
    const timer = controller
      ? setTimeout(() => controller.abort(), _PARK_ATTEMPT_TIMEOUT_MS)
      : null;
    const attemptStart = Date.now();
    let resp;
    try {
      resp = await fetch(url, {
        method: "POST",
        headers: reqHeaders,
        body,
        ...(controller ? { signal: controller.signal } : {}),
      });
    } catch (_err) {
      if (timer) clearTimeout(timer);
      // Distinguish a LEGITIMATE long-poll re-attach from a GENUINE transport
      // error. controller.signal.aborted alone is unreliable: once our
      // per-attempt timer has fired it reads true even if a real connect
      // reset raced the timer. A genuine connect error throws
      // fast — well under _PARK_ATTEMPT_TIMEOUT_MS — whereas a real long-poll
      // only aborts once the timer fires after holding the connection open
      // the whole attempt. So require BOTH aborted AND that the attempt
      // survived ~to the per-attempt timeout before treating it as a re-park.
      const aborted = !!(controller && controller.signal.aborted);
      const elapsed = Date.now() - attemptStart;
      const isLongPollReattach =
        aborted && elapsed >= _PARK_REATTACH_MIN_ELAPSED_MS;
      if (isLongPollReattach) {
        // Re-park: the server held the connection the whole time, so re-POST
        // the SAME elicitation id immediately (no backoff). This is the human
        // approval window — keep waiting (bounded only by parkDeadline).
        // A completed long-poll is a healthy round-trip, so refresh the
        // transient budget the same way the ASK branch does. Without this the
        // entry budget expires during the first park, and a genuine transport
        // blip while the human is still deciding would fail closed with zero
        // retries instead of getting its full transient window.
        transientDeadline = Date.now() + _TRANSIENT_RETRY_BUDGET_MS;
        transientBackoff = _TRANSIENT_RETRY_INITIAL_BACKOFF_MS;
        continue;
      }
      // Genuine transport error (connect refused / reset, or an abort that
      // fired too early to be a real park) → charge it against the short
      // transient budget. When that budget is exhausted, fail CLOSED.
      if (Date.now() + transientBackoff >= transientDeadline) {
        return failClosedVerdict();
      }
      await sleep(transientBackoff);
      transientBackoff = Math.min(
        transientBackoff * 2,
        _TRANSIENT_RETRY_MAX_BACKOFF_MS,
      );
      continue;
    } finally {
      if (timer) clearTimeout(timer);
    }

    if (!resp || resp.ok !== true) {
      // 5xx is transient (retry within budget, re-attaching); 4xx is final
      // (a bad request won't succeed on retry). Either way, an unevaluable
      // PHASE_TOOL_CALL fails CLOSED.
      if (resp && resp.status >= 500) {
        if (Date.now() + transientBackoff >= transientDeadline) {
          return failClosedVerdict();
        }
        await sleep(transientBackoff);
        transientBackoff = Math.min(
          transientBackoff * 2,
          _TRANSIENT_RETRY_MAX_BACKOFF_MS,
        );
        continue;
      }
      return failClosedVerdict();
    }

    let json;
    try {
      json = await resp.json();
    } catch (_err) {
      // Malformed body — not retryable, and we have no verdict. Fail CLOSED.
      return failClosedVerdict();
    }

    const result = json && json.result;
    if (result === "POLICY_ACTION_DENY") {
      return {
        block: true,
        reason:
          typeof json.reason === "string" && json.reason
            ? json.reason
            : "blocked by Omnigent policy",
      };
    }
    if (result === "POLICY_ACTION_ASK") {
      // The gate did not park server-side (e.g. a read-only caller that cannot
      // open an elicitation) yet still wants approval. Re-evaluate
      // (re-attaching) so a gate that is about to collapse to a hard verdict
      // gets the chance — but bound it: a raw ASK that NEVER collapses must
      // not ride the 24h park ceiling and then proceed. After
      // _MAX_RAW_ASK_ROUNDS we fail CLOSED, mirroring the Python native hook
      // which denies a stray ASK rather than deferring (an unresolvable
      // approval gate that eventually allows would defeat its purpose).
      rawAskRounds += 1;
      if (rawAskRounds >= _MAX_RAW_ASK_ROUNDS) {
        return failClosedVerdict();
      }
      // Reset the transient budget since this is a healthy round-trip, not a
      // failure, and give it a brief beat so we do not hot-loop a server that
      // keeps returning ASK.
      transientDeadline = Date.now() + _TRANSIENT_RETRY_BUDGET_MS;
      transientBackoff = _TRANSIENT_RETRY_INITIAL_BACKOFF_MS;
      await sleep(_TRANSIENT_RETRY_INITIAL_BACKOFF_MS);
      continue;
    }
    if (
      result === "POLICY_ACTION_ALLOW" ||
      result === "POLICY_ACTION_UNSPECIFIED"
    ) {
      const confirmed = readPermissionState(config);
      if (!confirmed || confirmed.status !== "ready" ||
          confirmed.revision !== permissionState.revision ||
          confirmed.tier !== permissionState.tier) return failClosedVerdict();
      return { block: false, reason: "" };
    }
    return failClosedVerdict();
  }
}

/**
 * Build a Pi tool-result object from an MCP ``tools/call`` JSON-RPC response.
 *
 * Pi expects ``{ content: [{ type: "text", text }], isError }``. The Omnigent
 * MCP proxy returns a JSON-RPC envelope whose ``result`` carries an MCP
 * content array (``[{ type: "text", text }]``) on success, or a JSON-RPC
 * ``error`` object (with the MCP convention code -32000 for tool denials /
 * tool errors) on failure. Map both into a single text block so the Pi agent
 * can read the output — denials surface as a readable error rather than
 * wedging the loop.
 */
function piResultFromMcpResponse(json) {
  if (json && typeof json === "object" && json.error) {
    const msg =
      (json.error && json.error.message) || "Omnigent tool call failed";
    return { content: [{ type: "text", text: String(msg) }], isError: true };
  }
  const result = json && typeof json === "object" ? json.result : undefined;
  // An ``input_required`` envelope must never reach this mapper: it carries no
  // ``content`` array, so it would otherwise fall to the "unexpected shape"
  // branch and be returned as ``isError: false`` — a confusing elicitation blob
  // masquerading as a successful tool result. callOmnigentTool detects and
  // resolves the ASK round-trip BEFORE calling this; treat a stray one as a
  // fail-closed error so an unresolved approval never reports success.
  if (
    result &&
    typeof result === "object" &&
    result.resultType === "input_required"
  ) {
    return {
      content: [
        {
          type: "text",
          text: "Omnigent tool call requires approval that was not resolved",
        },
      ],
      isError: true,
    };
  }
  if (result && Array.isArray(result.content)) {
    const parts = [];
    for (const block of result.content) {
      if (
        block &&
        typeof block === "object" &&
        typeof block.text === "string"
      ) {
        parts.push(block.text);
      }
    }
    const text = parts.join("\n");
    return {
      content: [{ type: "text", text: text || safeJsonStringify(result) }],
      isError: result.isError === true,
    };
  }
  // Unexpected shape: hand the raw result back so nothing is silently dropped.
  return {
    content: [{ type: "text", text: safeJsonStringify(result ?? json ?? {}) }],
    isError: false,
  };
}

/**
 * Extract the elicitation id + opaque requestState from an MCP
 * ``input_required`` (MRTR) envelope, or ``null`` when the response is not an
 * ``input_required`` result.
 *
 * The Omnigent server keys ``inputRequests`` by the server-minted elicitation
 * id (the MRTR spec lets the client read those keys; only ``requestState`` is
 * opaque), so the first key is the id the retry must echo back inside
 * ``inputResponses``.
 */
function mcpInputRequired(json) {
  const result = json && typeof json === "object" ? json.result : undefined;
  if (
    !result ||
    typeof result !== "object" ||
    result.resultType !== "input_required"
  ) {
    return null;
  }
  const inputRequests =
    result.inputRequests && typeof result.inputRequests === "object"
      ? result.inputRequests
      : {};
  const elicitationId = Object.keys(inputRequests)[0] || "";
  const requestState =
    typeof result.requestState === "string" ? result.requestState : "";
  return { elicitationId, requestState };
}

/**
 * Bounded classification for a 2xx ``/mcp`` reply that is not an MCP JSON body.
 *
 * ``resp.ok`` is not proof of a JSON answer: an authenticating edge (reverse
 * proxy, IdP) in front of the server can answer any route with a sign-in
 * document and status 200. The text names the classification only — never the
 * body, headers, or URL — because a sign-in document routinely carries tokens
 * and tenant identifiers.
 */
function nonJsonMcpPiResult() {
  return {
    content: [
      {
        type: "text",
        text:
          "Omnigent tool call failed: the server answered 2xx with a non-JSON " +
          "body, which usually means an authenticating proxy or sign-in page " +
          "answered instead of Omnigent. Re-authenticate and retry.",
      },
    ],
    isError: true,
  };
}

/**
 * False when ``resp`` declares a content type that is not JSON.
 *
 * A declared ``text/html`` (the sign-in case) is rejected. An absent or
 * unreadable content type is not treated as a signal, so the parse attempt
 * stays the decider there.
 */
function mcpContentTypeIsJson(resp) {
  let declared = null;
  try {
    // Both the property read and the lookup are inside the try: a `headers`
    // getter can throw just as `get()` can.
    const headers = resp && resp.headers;
    if (headers && typeof headers.get === "function") {
      declared = headers.get("content-type");
    }
  } catch (_headerErr) {
    // An unreadable header is not a signal, so fall through to the parse.
    // Swallow it here: letting it reach the outer catch would put its message —
    // which can quote header or body text — into the tool result.
    return true;
  }
  if (typeof declared !== "string" || declared.trim() === "") return true;
  return /^application\/(?:[\w.+-]+\+)?json\s*(?:;|$)/i.test(declared.trim());
}

/**
 * POST a single JSON-RPC ``tools/call`` to the server's per-session MCP proxy
 * and return the parsed response (or a fail-closed Pi tool-result on a
 * transport/HTTP error). ``extraParams`` carries the MRTR retry fields
 * (``requestState`` + ``inputResponses``) on the approval retry; it is omitted
 * on the initial call.
 *
 * @returns {Promise<{json: object} | {piResult: object}>} ``json`` on a parsed
 *   200 response; ``piResult`` is a terminal fail-closed tool result the caller
 *   returns as-is.
 */
async function postMcpToolsCall(config, toolName, args, rpcId, callId, extraParams) {
  const url = `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}/mcp`;
  const body = JSON.stringify({
    jsonrpc: "2.0",
    id: rpcId,
    method: "tools/call",
    params: { name: toolName, arguments: args || {},
      _omnigent_permission_call_id: callId, ...(extraParams || {}) },
  });
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "X-Omnigent-Permission-Tiers": "1",
        ...freshAuthHeaders(config.authHeaders),
      },
      body,
    });
    if (!resp.ok) {
      return {
        piResult: {
          content: [
            {
              type: "text",
              text: `Omnigent tool call failed: HTTP ${resp.status}`,
            },
          ],
          isError: true,
        },
      };
    }
    // Status, content type, and parseability must all agree before this body is
    // treated as an MCP response. A parse failure is classified here rather
    // than by the outer catch, whose message would echo the offending body back
    // through ``err.message``.
    if (!mcpContentTypeIsJson(resp)) return { piResult: nonJsonMcpPiResult() };
    try {
      return { json: await resp.json() };
    } catch (_parseErr) {
      return { piResult: nonJsonMcpPiResult() };
    }
  } catch (err) {
    return {
      piResult: {
        content: [
          {
            type: "text",
            text: `Omnigent tool call failed: ${err && err.message ? err.message : String(err)}`,
          },
        ],
        isError: true,
      },
    };
  }
}

/**
 * Execute an Omnigent tool by POSTing a JSON-RPC ``tools/call`` request to the
 * server's per-session MCP proxy endpoint
 * (``POST /v1/sessions/{sessionId}/mcp``).
 *
 * This is the SAME endpoint the runner's ``ProxyMcpManager`` uses: the Omnigent
 * server evaluates TOOL_CALL / TOOL_RESULT policy and then forwards execution
 * to the runner's ``/mcp/execute`` (which dispatches the real ``sys_*`` tool on
 * the correct machine with the session's terminal/workspace). The extension
 * already carries ``serverUrl`` + ``sessionId`` + ``authHeaders`` in its config,
 * so no extra relay process is needed.
 *
 * **ASK policy / elicitation round-trip (MRTR).** When the tool is gated by an
 * ASK policy the proxy returns HTTP 200 with an ``input_required`` result
 * (``{resultType, inputRequests, requestState}``) instead of executing. We must
 * NOT hand that envelope to the model as a result — it neither prompts nor runs
 * the tool. Mirroring ``ProxyMcpManager.dispatch()``, we resolve the human
 * verdict and retry once with the decision in ``inputResponses``:
 *   1. Long-poll ``POST /policies/evaluate`` via ``evalNativePolicyHttp`` — the
 *      SAME server-side ASK park the non-bridged hook uses. It holds the
 *      connection until a human resolves the approval card and collapses to a
 *      hard ALLOW / DENY (the extension has no in-process approval Future like
 *      the runner, so the long-poll IS its park/resolve mechanism).
 *   2. Retry the ``tools/call`` ONCE with ``requestState`` + ``inputResponses:
 *      {elicitationId: {action: "accept" | "decline"}}``. The server re-evaluates
 *      TOOL_CALL policy on the retry (it does not trust the client's claim
 *      blindly), so a still-denied tool stays denied.
 *   3. Cap at one retry; fail CLOSED (``isError: true``, readable message) if the
 *      approval cannot be resolved or the proxy still asks after the retry.
 *
 * (Trade-off: the proxy's ASK already published one approval card, and the
 * evaluate long-poll publishes a second one — the extension cannot re-attach to
 * the proxy-minted elicitation, whose id is in a different namespace. The human
 * resolves the evaluate card to drive the verdict; the proxy card is orphaned.
 * This is a UX wrinkle, not a security gap: the tool only runs on a genuine
 * human accept, and the server re-checks policy on the retry.)
 *
 * Fail-safe: any transport/parse error resolves to a readable tool-result error
 * (``isError: true``) rather than throwing, so a server hiccup never wedges Pi's
 * agent loop.
 */
async function callOmnigentTool(config, toolName, args, callId) {
  if (
    !config ||
    !config.serverUrl ||
    !config.sessionId ||
    typeof fetch !== "function" ||
    typeof callId !== "string" || !callId ||
    !args || typeof args !== "object" || Array.isArray(args)
  ) {
    return {
      content: [
        { type: "text", text: "Omnigent tool bridge is not configured" },
      ],
      isError: true,
    };
  }

  const permissionState = readPermissionState(config);
  if (!permissionState || permissionState.status !== "ready") {
    return { content: [{ type: "text", text: "Permission state needs recovery; tools are paused" }], isError: true };
  }

  const first = await postMcpToolsCall(config, toolName, args, 1, callId);
  if (first.piResult) return first.piResult;
  const initial = mcpInputRequired(first.json);
  if (initial === null) {
    // ALLOW / DENY / executed — map directly to a Pi tool result.
    return piResultFromMcpResponse(first.json);
  }

  // ── ASK: resolve the human verdict, then retry once (MRTR) ──────────
  if (!initial.elicitationId || !initial.requestState) {
    // Malformed input_required — cannot retry. Fail CLOSED.
    return {
      content: [
        {
          type: "text",
          text: "Omnigent tool call requires approval but the server sent no resolvable elicitation",
        },
      ],
      isError: true,
    };
  }

  const verdict = await evalNativePolicyHttp(
    config,
    toolName,
    args || {},
    callId,
  );
  const action = verdict.block ? "decline" : "accept";

  const current = readPermissionState(config);
  if (!current || current.status !== "ready" ||
      current.revision !== permissionState.revision || current.tier !== permissionState.tier) {
    return { content: [{ type: "text", text: "Permission state changed; the tool was not executed" }], isError: true };
  }
  const retry = await postMcpToolsCall(config, toolName, args, 2, callId, {
    requestState: initial.requestState,
    inputResponses: { [initial.elicitationId]: { action } },
  });
  if (retry.piResult) return retry.piResult;
  if (mcpInputRequired(retry.json) !== null) {
    // The proxy asked again after one approval round — do NOT loop. Fail CLOSED.
    return {
      content: [
        {
          type: "text",
          text: "Omnigent tool call still requires approval after one round — not retrying",
        },
      ],
      isError: true,
    };
  }
  return piResultFromMcpResponse(retry.json);
}

function textFromContent(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts = [];
  for (const block of content) {
    if (!block || typeof block !== "object") continue;
    // OpenAI o-series / gpt-oss models return content as an array with typed
    // blocks: {type:"text",text:"..."} and {type:"reasoning",summary:[...]}.
    // Only collect text blocks; skip reasoning/summary blocks.
    if (block.type === "reasoning") continue;
    const text =
      block.text || block.input_text || block.output_text || block.content;
    if (typeof text === "string") parts.push(text);
  }
  return parts.join("");
}

function textFromMessage(message) {
  if (!message || typeof message !== "object") return "";
  return textFromContent(
    message.content || message.parts || message.message || "",
  );
}

function safeJsonStringify(value) {
  try {
    return JSON.stringify(value ?? {});
  } catch (_err) {
    return String(value);
  }
}

function textFromToolResult(event) {
  if (!event || typeof event !== "object") return "";
  const text = textFromContent(event.content);
  if (text) return text;
  if ("result" in event) {
    const result = event.result;
    if (typeof result === "string") return result;
    if (result && typeof result === "object") {
      const resultText = textFromContent(result.content);
      if (resultText) return resultText;
    }
    return safeJsonStringify(result);
  }
  if ("details" in event) return safeJsonStringify(event.details);
  return "";
}

function contentBlocks(message) {
  if (
    !message ||
    typeof message !== "object" ||
    !Array.isArray(message.content)
  )
    return [];
  return message.content;
}

function fingerprint(text) {
  let hash = 5381;
  for (let i = 0; i < text.length; i += 1) {
    hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
  }
  return `${text.length}-${hash.toString(36)}`;
}

function messageRole(message) {
  if (!message || typeof message !== "object") return "";
  return String(message.role || message.type || "");
}

function toInt(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.trunc(n) : 0;
}

/**
 * Lift token usage out of one Pi assistant message.
 *
 * Mirrors the non-native executor's ``_extract_pi_turn_usage``
 * (omnigent/inner/pi_executor.py): Pi (``@earendil-works/pi-coding-agent``)
 * carries a per-message ``usage`` object with ``input`` / ``output`` /
 * ``cacheRead`` / ``cacheWrite`` / ``totalTokens`` counts, and the message
 * carries the resolved ``model``. Pi's ``input`` is the NON-cached input
 * (Anthropic semantics) — ``cacheRead`` / ``cacheWrite`` are separate, so the
 * full input a turn sent is ``input + cacheRead + cacheWrite``.
 *
 * @returns {{input:number,output:number,cacheRead:number,cacheWrite:number,
 *   total:number,model:(string|null),provider:(string|null)}|null} the per-message counts, or
 *   ``null`` when ``message`` is not an assistant message carrying usage.
 */
function extractPiUsage(message) {
  if (!message || typeof message !== "object") return null;
  if (message.role !== "assistant") return null;
  const usage = message.usage;
  if (!usage || typeof usage !== "object") return null;
  const input = toInt(usage.input);
  const output = toInt(usage.output);
  const cacheRead = toInt(usage.cacheRead);
  const cacheWrite = toInt(usage.cacheWrite);
  // No countable tokens means Pi emitted an empty usage object — treat as "no
  // usage" so the server leaves the session unpriced rather than recording a
  // $0.00 turn (matches _aggregate_pi_turn_usage's empty-usage guard).
  if (!(input || output || cacheRead || cacheWrite)) return null;
  const rawModel = message.model;
  const model = typeof rawModel === "string" && rawModel ? rawModel : null;
  const rawProvider = message.provider;
  const provider = typeof rawProvider === "string" && rawProvider ? rawProvider : null;
  return {
    input,
    output,
    cacheRead,
    cacheWrite,
    total: toInt(usage.totalTokens),
    model,
    provider,
  };
}

function headers(config) {
  return {
    "content-type": "application/json",
    ...freshAuthHeaders(config.authHeaders),
    "X-Omnigent-Permission-Tiers": "1",
  };
}

const _ASSISTANT_ITEM_MAX_ATTEMPTS = 3;
const _ASSISTANT_ITEM_INITIAL_BACKOFF_MS = 50;
const _ASSISTANT_ITEM_MAX_BACKOFF_MS = 250;
const _ASSISTANT_ITEM_POST_TIMEOUT_MS = 5_000;
const _PREVIEW_POST_TIMEOUT_MS = 1_000;
const _PREVIEW_FLUSH_TIMEOUT_MS = 300;
const _PREVIEW_MAX_QUEUED_EVENTS = 32;
const _PREVIEW_MAX_QUEUED_CHARS = 32_768;
const _PREVIEW_MAX_DELTA_CHARS = 8_192;

function boundedSourceId(sourceId) {
  const value = typeof sourceId === "string" ? sourceId.trim() : "";
  if (!value) throw new Error("assistant conversation item requires source_id");
  if (value.length <= 256) return value;
  return `pi:${crypto.createHash("sha256").update(value).digest("hex")}`;
}

function eventPostError(response) {
  const status = response && response.status;
  if (typeof status === "number" && Number.isFinite(status)) {
    const error = new Error(`Omnigent event POST failed with HTTP ${status}`);
    error.status = status;
    return error;
  }
  return new Error("Omnigent event POST returned an invalid response");
}

async function postEventChecked(config, body, signal) {
  if (
    !config ||
    !config.serverUrl ||
    !config.sessionId ||
    typeof fetch !== "function"
  )
    return;
  const url = `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}/events`;
  const request = {
    method: "POST",
    headers: headers(config),
    body: JSON.stringify(body),
  };
  if (signal) request.signal = signal;
  const response = await fetch(url, request);
  const status = response && response.status;
  if (
    !response ||
    (typeof status === "number" &&
      (!Number.isFinite(status) || status < 200 || status >= 300)) ||
    (typeof status !== "number" && response.ok !== true)
  ) {
    throw eventPostError(response);
  }
  return response;
}

async function postEvent(config, body) {
  try {
    await postEventChecked(config, body);
  } catch (_err) {
    // Keep Pi responsive even if Omnigent is temporarily unavailable.
  }
}

function isRetryableAssistantError(error) {
  const status = error && error.status;
  if (typeof status !== "number") return true;
  return status === 408 || status === 429 || (status >= 500 && status <= 599);
}

function assistantItemPostTimeoutMs(config) {
  const configured = config && config.assistantItemPostTimeoutMs;
  return typeof configured === "number" &&
    Number.isFinite(configured) &&
    configured > 0
    ? configured
    : _ASSISTANT_ITEM_POST_TIMEOUT_MS;
}

// Final assistant messages and errors survive transient transport failures.
async function postConversationItem(config, data, sourceId) {
  const body = {
    type: "external_conversation_item",
    data: { ...data, source_id: boundedSourceId(sourceId) },
  };
  let backoff = _ASSISTANT_ITEM_INITIAL_BACKOFF_MS;
  let lastError;
  for (let attempt = 1; attempt <= _ASSISTANT_ITEM_MAX_ATTEMPTS; attempt += 1) {
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      assistantItemPostTimeoutMs(config),
    );
    try {
      return await postEventChecked(config, body, controller.signal);
    } catch (err) {
      lastError = err;
    } finally {
      clearTimeout(timeout);
    }
    if (
      attempt === _ASSISTANT_ITEM_MAX_ATTEMPTS ||
      !isRetryableAssistantError(lastError)
    )
      break;
    await sleep(backoff);
    backoff = Math.min(backoff * 2, _ASSISTANT_ITEM_MAX_BACKOFF_MS);
  }
  throw lastError || new Error("assistant conversation item POST failed");
}

function assistantMessageSourceId(message, responseId, text) {
  const response =
    message && typeof message.responseId === "string" && message.responseId
      ? `response:${message.responseId}`
      : "";
  const timestamp =
    message && typeof message.timestamp === "number"
      ? `timestamp:${message.timestamp}`
      : "";
  const identity = response || timestamp || `turn:${responseId}:${fingerprint(text)}`;
  return boundedSourceId(`pi:assistant:${identity}`);
}

async function patchExternalSessionId(config, nativeSessionId) {
  if (
    !nativeSessionId ||
    !config ||
    !config.serverUrl ||
    typeof fetch !== "function"
  )
    return;
  const url = `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}`;
  try {
    await fetch(url, {
      method: "PATCH",
      headers: headers(config),
      body: JSON.stringify({ external_session_id: nativeSessionId }),
    });
  } catch (_err) {}
}

function setOmnigentStatus(config, ctx, state) {
  if (!ctx || !ctx.ui || !config) return;
  const urlLabel = config.conversationUrl
    ? `Omnigent: ${config.conversationUrl}`
    : "Omnigent";
  const label = state ? `${urlLabel} · ${state}` : urlLabel;
  try {
    ctx.ui.setTitle(`Omnigent: ${config.sessionId}`);
    ctx.ui.setStatus("omnigent", label);
    ctx.ui.setStatus("omnigent_state", undefined);
  } catch (_err) {}
}

function interruptActiveContext(ctx) {
  if (!ctx || typeof ctx.abort !== "function") return false;
  try {
    ctx.abort();
    return true;
  } catch (_err) {
    return false;
  }
}

/** Run Pi compaction through its callback API, publishing ordered progress events. */
async function triggerCompaction(config, ctx, customInstructions) {
  if (!ctx || typeof ctx.compact !== "function") {
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: `pi-compact-unavailable-${Date.now()}`,
        item_type: "error",
        item_data: {
          source: "execution",
          code: "pi_compact_unavailable",
          message:
            "Omnigent: /compact is unavailable for this Pi session. The " +
            "resident Pi context exposes no compaction API, so the model or " +
            "Pi version may not support it.",
        },
      },
    });
    return false;
  }
  try {
    await postEvent(config, {
      type: "external_compaction_status",
      data: { status: "in_progress" },
    });
    await new Promise((resolve, reject) => {
      ctx.compact({
        onComplete: resolve,
        onError: reject,
        ...(typeof customInstructions === "string" && customInstructions.trim()
          ? { customInstructions }
          : {}),
      });
    });
    await postEvent(config, {
      type: "external_compaction_status",
      data: { status: "completed" },
    });
    return true;
  } catch (_err) {
    await postEvent(config, {
      type: "external_compaction_status",
      data: { status: "failed" },
    });
    return false;
  }
}

/**
 * Apply a web-picked model switch to the resident Pi process.
 *
 * Pi owns the active model inside this TUI process, so a model picked in the
 * Omnigent web UI must be applied here (the ``--model`` launch arg is baked in
 * at spawn). Resolves *modelId* against the session's ``modelRegistry`` (the
 * same catalog Pi's own ``/model`` picker uses, sourced from the generated
 * models.json) and calls Pi's ``setModel`` — immediate, no ``/reload``.
 *
 * Outcomes (mirrors triggerCompaction's visible-error contract so a web pick
 * never silently vanishes — the runner already returned 204, so there is no
 * server-side fallback):
 *   - No resident context / registry: post an error item, return false.
 *   - Model id not in the registry: post an error item, return false.
 *   - setModel returned false (no API key for the model): post an error item,
 *     return false.
 *   - Applied: return true. The paired ``model_select`` handler mirrors the
 *     resulting model back to Omnigent, so the web pill reflects the switch.
 */
const inferenceProviderIds = new Set([
  "omnigent",
  "omnigent-openai",
  "omnigent-completions",
]);

function hasInferenceBinding() {
  return process.env.OMNIGENT_PI_INFERENCE_BOUND === "1";
}

async function applyModelChange(pi, config, ctx, modelId) {
  const id = typeof modelId === "string" ? modelId.trim() : "";
  if (!id) return false;
  const registry = ctx ? ctx.modelRegistry : undefined;
  // Resolve against whichever listing method exists. Accept EITHER getAll or
  // getAvailable so the resolve path can never be narrower than the picker's:
  // postModelOptions lists from getAvailable(), so gating apply on getAll alone
  // would fail every switch on a hypothetical Pi build exposing only
  // getAvailable. getAll (the full catalog) is a superset of getAvailable, so
  // prefer it to resolve; fall back to getAvailable when getAll is absent.
  const listModels =
    registry && typeof registry.getAll === "function"
      ? () => registry.getAll()
      : registry && typeof registry.getAvailable === "function"
        ? () => registry.getAvailable()
        : null;
  if (!pi || typeof pi.setModel !== "function" || !listModels) {
    await postModelChangeError(
      config,
      `Omnigent: could not switch to model "${id}" — this Pi session exposes ` +
        "no model-switch API (the model or Pi version may not support it).",
    );
    return false;
  }
  let model;
  try {
    const models = listModels().filter(
      (candidate) =>
        !hasInferenceBinding() ||
        (candidate && inferenceProviderIds.has(candidate.provider)),
    );
    const separator = id.indexOf("/");
    if (hasInferenceBinding()) {
      // Profile IDs are literal, including slashes that resemble Pi providers.
      model = models.find((candidate) => candidate && candidate.id === id);
      if (!model && separator > 0 && inferenceProviderIds.has(id.slice(0, separator))) {
        model = models.find(
          (candidate) =>
            candidate &&
            candidate.provider === id.slice(0, separator) &&
            candidate.id === id.slice(separator + 1),
        );
      }
    } else if (separator > 0) {
      const provider = id.slice(0, separator);
      const bareId = id.slice(separator + 1);
      model = models.find(
        (candidate) =>
          candidate && candidate.id === bareId && candidate.provider === provider,
      );
      if (!model) model = models.find((candidate) => candidate && candidate.id === id);
      if (!model && registry && typeof registry.find === "function") {
        model = registry.find(provider, bareId);
      }
      if (!model) {
        model = models.find(
          (candidate) => candidate && candidate.id === bareId && !candidate.provider,
        );
      }
    } else {
      model = models.find((candidate) => candidate && candidate.id === id);
    }
  } catch (_err) {
    model = undefined;
  }
  if (!model) {
    await postModelChangeError(
      config,
      `Omnigent: model "${id}" is not available in this Pi session.`,
    );
    return false;
  }
  try {
    const applied = await pi.setModel(model);
    if (applied === false) {
      await postModelChangeError(
        config,
        `Omnigent: could not switch to model "${id}" — no API key is ` +
          "configured for it.",
      );
      return false;
    }
    return true;
  } catch (_err) {
    await postModelChangeError(
      config,
      `Omnigent: switching to model "${id}" failed inside Pi.`,
    );
    return false;
  }
}

async function postModelChangeError(config, message) {
  await postEvent(config, {
    type: "external_conversation_item",
    data: {
      response_id: `pi-model-change-error-${Date.now()}`,
      item_type: "error",
      item_data: {
        source: "execution",
        code: "pi_model_change_failed",
        message,
      },
    },
  });
}

function modelReference(model) {
  const modelId = model && typeof model.id === "string" ? model.id : "";
  if (!modelId) return "";
  const provider = model && typeof model.provider === "string" ? model.provider : "";
  if (hasInferenceBinding() && inferenceProviderIds.has(provider)) return modelId;
  return provider ? `${provider}/${modelId}` : modelId;
}

/**
 * Report Pi's live model catalog to Omnigent for the Web UI model picker.
 *
 * Sourced from Pi's model registry — the models Pi actually loaded for THIS
 * session, whatever their origin: an Omnigent-configured provider's generated
 * models.json, or Pi's own ``/login`` / ``~/.pi`` config. Posting the live
 * registry (rather than the server reading a launch-written file) means the
 * picker populates in every auth path, including ``/login`` where no Omnigent
 * models.json exists.
 *
 * Prefers ``getAvailable()`` — only models with configured auth — over
 * ``getAll()`` (Pi's entire built-in catalog spanning every vendor, most with
 * no credentials). This scopes the picker to models the user can actually
 * switch to, and naturally to whichever provider(s) they are logged into.
 * Falls back to ``getAll()`` only when ``getAvailable()`` is unavailable
 * (older Pi). Best-effort and fire-and-forget: an empty or unavailable
 * registry posts nothing, leaving the picker hidden.
 */
async function postModelOptions(config, ctx) {
  const registry = ctx ? ctx.modelRegistry : undefined;
  if (!registry) return;
  let models;
  try {
    if (typeof registry.getAvailable === "function") {
      models = registry.getAvailable();
    } else if (typeof registry.getAll === "function") {
      models = registry.getAll();
    } else {
      return;
    }
  } catch (_err) {
    return;
  }
  if (!Array.isArray(models) || models.length === 0) return;
  const options = [];
  const seen = new Set();
  for (const model of models) {
    if (hasInferenceBinding() && (!model || !inferenceProviderIds.has(model.provider))) continue;
    const modelId = model && typeof model.id === "string" ? model.id : "";
    const id = modelReference(model);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const name =
      model && typeof model.name === "string" && model.name ? model.name : modelId;
    options.push({ id, model: id, displayName: name });
  }
  if (options.length === 0) return;
  await postEvent(config, {
    type: "external_model_options",
    data: { models: options },
  });
}

/**
 * Tell the runner's terminal watcher that Pi now accepts input: the inbox
 * poller is armed, so queued web messages will be delivered. The runner
 * clears the marker before each launch and logs ``native_input_ready`` when
 * it appears. Best-effort — readiness logging must never break the session.
 */
function markInputReady(config) {
  if (!config || !config.bridgeDir) return;
  try {
    fs.writeFileSync(path.join(config.bridgeDir, "input_ready"), "");
  } catch (_err) {
    // Diagnostics only.
  }
}

function startInboxPoller(
  pi,
  config,
  handleInterrupt,
  handleCompact,
  handleModelChange,
  handleThinkingLevelChange,
  isTurnActive,
  isCompacting,
  handlePermissionTierChange,
) {
  if (!config || !config.inboxDir || pi.__omnigentInboxPoller) return;
  // Bound the dedup set (FIFO eviction) — delivered files are unlinked, so a
  // long-lived TUI mustn't grow it unboundedly.
  const seen = new Set();
  const SEEN_CAP = 4096;
  const rememberSeen = (id) => {
    seen.add(id);
    while (seen.size > SEEN_CAP) seen.delete(seen.values().next().value);
  };
  // Cap send attempts so a persistently-failing sendUserMessage can't
  // re-read+re-throw the same file forever (the turn is already reported done).
  const deliverAttempts = new Map();
  const MAX_DELIVER_ATTEMPTS = 5;
  pi.__omnigentInboxPoller = setInterval(() => {
    let files = [];
    try {
      files = fs
        .readdirSync(config.inboxDir)
        .filter((name) => name.endsWith(".json"))
        .sort();
    } catch (_err) {
      return;
    }
    for (const file of files) {
      const fullPath = path.join(config.inboxDir, file);
      let payload;
      try {
        payload = JSON.parse(fs.readFileSync(fullPath, "utf8"));
      } catch (_err) {
        continue;
      }
      // Dedup only on a real string id; seen.has(undefined) would drop every
      // later id-less payload.
      const id = typeof payload?.id === "string" ? payload.id : null;
      if (!payload || (id !== null && seen.has(id))) {
        try {
          fs.unlinkSync(fullPath);
        } catch (_err) {}
        continue;
      }
      // Pi's extension API cannot accept prompts during manual compaction.
      // Leave them on disk, in order, until its completion callback fires.
      if (
        (payload.type === "user_message" || payload.type === "compact") &&
        isCompacting()
      ) {
        continue;
      }
      if (
        payload.type === "user_message" &&
        typeof payload.content === "string"
      ) {
        // Mid-turn messages must STEER into the active turn: the Pi SDK
        // holds deliverAs "followUp" until the whole agent loop finishes, so
        // a web "Send now" delivered as a follow-up stays visibly queued in
        // the Pi CLI even though the web UI already reported success. When
        // the agent is idle, keep "followUp" — it starts the next turn
        // immediately, preserving initiating-message behavior. A turn ending
        // between this check and the send is benign: the message still
        // reaches Pi's queue, and a throw leaves the file for the next tick,
        // which recomputes the mode.
        const deliverAs =
          typeof isTurnActive === "function" && isTurnActive()
            ? "steer"
            : "followUp";
        try {
          pi.sendUserMessage(payload.content, { deliverAs });
        } catch (_err) {
          // Leave the file to retry next tick, capped by attempt count.
          const key = id ?? fullPath;
          const attempts = (deliverAttempts.get(key) ?? 0) + 1;
          if (attempts < MAX_DELIVER_ATTEMPTS) {
            deliverAttempts.set(key, attempts);
            continue;
          }
          // Cap reached: surface the dropped follow-up without faking a turn
          // failure. The runner treats external_session_status:failed as
          // terminal for native sub-agents, so use a non-content conversation
          // error item and consume the file to stop the spin. Include the
          // message id and a short content preview so an operator can identify
          // what was lost (data loss; the file is unlinked below).
          deliverAttempts.delete(key);
          const droppedId = id ?? "(no id)";
          const preview =
            typeof payload.content === "string"
              ? payload.content.length > 80
                ? `${payload.content.slice(0, 80)}…`
                : payload.content
              : "";
          postEvent(config, {
            type: "external_conversation_item",
            data: {
              response_id: `pi-deliver-dropped-${Date.now()}`,
              item_type: "error",
              item_data: {
                source: "execution",
                code: "pi_followup_delivery_dropped",
                message:
                  `Omnigent: a queued follow-up message (id ${droppedId}) could ` +
                  `not be delivered to Pi after ${MAX_DELIVER_ATTEMPTS} attempts ` +
                  `and was dropped. Content preview: ${JSON.stringify(preview)}`,
              },
            },
          });
          try {
            fs.unlinkSync(fullPath);
          } catch (_err) {}
          continue;
        }
        deliverAttempts.delete(id ?? fullPath);
      }
      if (payload.type === "interrupt") {
        // An interrupt is point-in-time: make one delivery attempt, then
        // always consume the file (below). If there is no live turn to abort
        // right now, the interrupt is simply dropped — leaving the file would
        // re-read it every tick forever and, once a later turn creates an
        // abortable context, abort that unrelated turn. requestInterrupt only
        // arms the pendingInterrupt window when it catches a genuinely running
        // turn (idle interrupts are dropped, not armed — see F18), so a turn
        // already in flight still gets aborted via replay without poisoning the
        // next freshly-started turn.
        if (typeof handleInterrupt === "function") handleInterrupt();
      }
      if (payload.type === "compact") {
        // Point-in-time like an interrupt: one delivery attempt against the
        // resident context, then always consume the file (below) — leaving it
        // would re-trigger compaction every tick. handleCompact owns every
        // status edge and the unavailable-context error item, so its returned
        // promise is intentionally discarded: there is nothing for the poller
        // to retry or clean up.
        if (typeof handleCompact === "function") {
          handleCompact(
            typeof payload.custom_instructions === "string"
              ? payload.custom_instructions
              : undefined,
          );
        }
      }
      if (payload.type === "model_change") {
        // Point-in-time like compact/interrupt: one delivery attempt against
        // the resident context, then always consume the file (below).
        // handleModelChange owns its visible-error item and posts nothing on
        // success — the paired model_select handler mirrors the applied model
        // back — so the returned promise is intentionally discarded.
        handleModelChange(
          typeof payload.model === "string" ? payload.model : undefined,
        );
      }
      if (payload.type === "thinking_level_change") {
        // Point-in-time: one delivery attempt, then always consume the file.
        // The callback reads back Pi's effective level after its synchronous setter.
        if (
          typeof handleThinkingLevelChange === "function" &&
          typeof payload.thinkingLevel === "string"
        ) {
          handleThinkingLevelChange(payload.thinkingLevel);
        }
      }
      if (payload.type === "permission_tier_change" &&
          typeof handlePermissionTierChange === "function") {
        handlePermissionTierChange(payload);
      }
      if (id !== null) rememberSeen(id);
      try {
        fs.unlinkSync(fullPath);
      } catch (_err) {}
    }
  }, 250);
}

module.exports = function (pi) {
  process.env.OMNIGENT_PI_NATIVE_PID = String(process.pid);
  const config = readConfig();
  let sequence = 0;
  let turnOrdinal = 0;
  let activeResponseId = null;
  // Response id shared across a turn's ``running`` → ``idle`` status pair.
  // Kept separate from activeResponseId, which turn_start overwrites.
  let turnStatusResponseId = null;
  // Dedicated loop-state flag, set on agent_start / cleared on agent_end. Used
  // as the no-isIdle() fallback for requestInterrupt instead of
  // !activeResponseId: agent_start resets activeResponseId to null and only
  // turn_start assigns it, so an interrupt landing in that gap (after
  // agent_start, before turn_start) would look idle by activeResponseId yet the
  // loop is genuinely running — agentRunning arms it correctly. See F18.
  let agentRunning = false;
  let compacting = false;
  let latestContext = null;
  let rotationInFlight = false;
  let rotationPending = false;
  let settingThinkingLevel = false;
  let lastReportedThinkingLevel;
  let thinkingLevelReport = Promise.resolve();
  function reportThinkingLevel(force = false) {
    // Serialize posts and read state when sending, so concurrent native events
    // cannot deliver an older snapshot after a newer selection.
    thinkingLevelReport = thinkingLevelReport.then(async () => {
      try {
        if (typeof pi.getThinkingLevel !== "function") return;
        const level = pi.getThinkingLevel();
        if (!["off", "minimal", "low", "medium", "high", "xhigh", "max"].includes(level)) return;
        if (!force && level === lastReportedThinkingLevel) return;
        await postEventChecked(config, {
          type: "external_reasoning_effort_change",
          data: { reasoning_effort: level === "off" ? "none" : level },
        });
        lastReportedThinkingLevel = level;
      } catch (_err) {
        lastReportedThinkingLevel = undefined;
        // Legacy APIs and a temporarily unavailable server must not break Pi.
      }
    });
    return thinkingLevelReport;
  }
  let pendingInterruptUntil = 0;
  const postedToolCalls = new Set();
  const postedToolResults = new Set();
  const postedReasoning = new Set();
  const postedAssistantMessages = new Set();
  const streamedReasoningBlocks = new Set();
  const toolCallsById = new Map();
  const pendingInterruptMs = 30_000;
  // Live streaming state for assistant text deltas. Pi emits
  // message_update events carrying an assistantMessageEvent of type
  // "text_delta" (token chunk) / "text_end" (block complete) during a
  // turn — see @earendil-works/pi-ai AssistantMessageEvent. We forward
  // adjacent tokens as transient external_output_text_delta events so the
  // web UI paints a live preview before the final message lands.
  //
  // The preview is keyed by ASSISTANT MESSAGE, not per text block.
  // message_end carries the same id on its authoritative item, retiring late
  // previews, and posts ONE combined external_conversation_item
  // per assistant message (textFromMessage joins all text blocks). So a
  // 1:1 message-scoped id keeps exactly one preview per item; a per-block
  // id would orphan extra previews when a message has multiple text
  // blocks (e.g. text → tool call → more text). All text blocks of a
  // message share its id with a single monotonic chunk index, so the
  // preview reads as one growing message — matching claude-native.
  //
  // Deltas are best-effort live preview: postEvent fails open, and the
  // authoritative text still arrives via message_end regardless.
  //
  // streamingMessageOrdinal: bumped at each assistant message_end so the
  // NEXT message of the turn gets a fresh, stable id distinct from earlier
  // ones — see the message_end handler for why it advances there (not on
  // message_start) so deltas and the finalize agree on the id.
  let streamingMessageOrdinal = 0;
  // The sender serializes text and reasoning previews. Slow preview HTTP
  // cannot hold Pi's token producer or the authoritative message indefinitely.
  const previewQueue = [];
  let previewQueuedChars = 0;
  let previewTask = null;
  let previewController = null;
  let previewDisabled = false;
  function disablePreviews() {
    previewDisabled = true;
    previewQueue.length = 0;
    previewQueuedChars = 0;
    if (previewController) previewController.abort();
  }
  function queuePreview(type, data, key) {
    if (previewDisabled) return;
    const delta = data.delta;
    const last = previewQueue[previewQueue.length - 1];
    if (last && key && last.key === key &&
        last.data.delta.length + delta.length <= _PREVIEW_MAX_DELTA_CHARS) {
      last.data.delta += delta;
      previewQueuedChars += delta.length;
    } else if (previewQueue.length < _PREVIEW_MAX_QUEUED_EVENTS &&
               delta.length <= _PREVIEW_MAX_DELTA_CHARS) {
      previewQueue.push({ type, data, key });
      previewQueuedChars += delta.length;
    } else {
      previewDisabled = true;
    }
    if (previewQueuedChars > _PREVIEW_MAX_QUEUED_CHARS || previewDisabled) {
      previewQueue.length = 0;
      previewQueuedChars = 0;
      return;
    }
    queuePreviewPump();
  }

  function queuePreviewPump() {
    if (previewTask || !previewQueue.length) return;
    previewTask = drainPreviews().finally(() => {
      previewTask = null;
      if (previewQueue.length) queuePreviewPump();
    });
  }

  async function drainPreviews() {
    while (previewQueue.length) {
      const next = previewQueue.shift();
      previewQueuedChars -= next.data.delta.length;
      if (next.type === "external_output_text_delta") {
        const messageId = next.data.message_id;
        const index = streamedTextIndex.get(messageId) || 0;
        next.data.index = index;
        streamedTextIndex.set(messageId, index + 1);
      }
      const controller = new AbortController();
      previewController = controller;
      let timeout;
      try {
        const deadline = new Promise((_, reject) => {
          controller.signal.addEventListener("abort", () => {
            reject(new Error("preview POST aborted"));
          }, { once: true });
          timeout = setTimeout(() => {
            controller.abort();
          }, _PREVIEW_POST_TIMEOUT_MS);
        });
        await Promise.race([
          postEventChecked(config, { type: next.type, data: next.data }, controller.signal),
          deadline,
        ]);
      } catch (_err) {
        // A partial preview is repaired by the authoritative item.
        disablePreviews();
      } finally {
        clearTimeout(timeout);
        if (previewController === controller) previewController = null;
      }
    }
  }

  async function flushPreviews() {
    const deadline = Date.now() + _PREVIEW_FLUSH_TIMEOUT_MS;
    while (previewTask) {
      const remaining = deadline - Date.now();
      if (remaining <= 0) {
        disablePreviews();
        await previewTask;
        return;
      }
      let timeout;
      await Promise.race([
        previewTask,
        new Promise((resolve) => { timeout = setTimeout(resolve, remaining); }),
      ]);
      clearTimeout(timeout);
    }
  }

  // streamedTextIndex: message_id -> next 0-based chunk index.
  const streamedTextIndex = new Map();
  // finalizedTextBlocks: message_ids whose final delta was already posted,
  // so a duplicate text_end (or a stray text_delta after end) can't reopen
  // or double-finalize the preview.
  const finalizedTextBlocks = new Set();

  // Names of the Omnigent tools registered via pi.registerTool below. Bridged
  // tools are policy-evaluated server-side inside the /mcp proxy (TOOL_CALL +
  // TOOL_RESULT), so the tool_call hook must NOT also call
  // evalNativePolicyHttp for them — that would double-evaluate and, for ASK
  // policies, double-prompt. Pi's OWN built-in tools (read/shell/etc) are not
  // in this set and stay gated by the hook below.
  const bridgedTools = new Set();
  const piNativeTools = new Set(["read", "bash", "write", "edit"]);
  const permittedNativeCalls = new Map();
  async function finishNativePermissionCall(callId, event) {
    if (typeof callId !== "string") return;
    const lease = permittedNativeCalls.get(callId);
    if (!lease || lease.finishing) return;
    lease.finishing = true;
    try {
      if (await finishPermissionLease(config, callId, lease, event) &&
          permittedNativeCalls.get(callId) === lease) {
        permittedNativeCalls.delete(callId);
      }
    } finally {
      lease.finishing = false;
    }
  }
  let baselineActiveTools = null;
  if (config && Array.isArray(config.tools)) {
    for (const tool of config.tools) {
      if (!tool || typeof tool !== "object") continue;
      const name = typeof tool.name === "string" ? tool.name : "";
      if (!name) continue;
      const description =
        typeof tool.description === "string" ? tool.description : "";
      const parameters =
        tool.parameters && typeof tool.parameters === "object"
          ? tool.parameters
          : { type: "object", properties: {} };
      // Pi passes tool.parameters straight to the LLM as JSON Schema, so the
      // Omnigent schema is usable as-is. execute() round-trips the call to the
      // Omnigent server's MCP proxy and returns the result to Pi.
      if (typeof pi.registerTool === "function") {
        pi.registerTool({
          name,
          label: name,
          description,
          promptSnippet: description ? description.slice(0, 120) : name,
          parameters,
          async execute(toolCallId, params) {
            return callOmnigentTool(config, name, params || {}, toolCallId);
          },
        });
        bridgedTools.add(name);
      }
    }
  }

  let taskList = [];

  function normalizeTaskList(value) {
    if (!Array.isArray(value)) return null;
    const statuses = new Set(["not-started", "in-progress", "completed"]);
    const normalized = [];
    for (const task of value) {
      if (
        !task ||
        typeof task !== "object" ||
        !Number.isInteger(task.id) ||
        typeof task.title !== "string" ||
        typeof task.description !== "string" ||
        !statuses.has(task.status)
      ) {
        return null;
      }
      normalized.push({
        id: task.id,
        title: task.title,
        description: task.description,
        status: task.status,
      });
    }
    return normalized;
  }

  async function publishTaskList() {
    const status = {
      "not-started": "pending",
      "in-progress": "in_progress",
      completed: "completed",
    };
    await postEvent(config, {
      type: "external_session_todos",
      data: {
        todos: taskList.map((task) => ({
          content: task.title,
          status: status[task.status],
          activeForm: task.description || task.title,
        })),
      },
    });
  }

  function restoreTaskList(ctx) {
    taskList = [];
    const entries =
      ctx &&
      ctx.sessionManager &&
      typeof ctx.sessionManager.getBranch === "function"
        ? ctx.sessionManager.getBranch()
        : [];
    for (const entry of entries) {
      const message = entry && entry.type === "message" ? entry.message : null;
      if (
        !message ||
        message.role !== "toolResult" ||
        message.toolName !== "manage_todo_list"
      ) {
        continue;
      }
      const restored = normalizeTaskList(
        message.details && message.details.todos,
      );
      if (restored) taskList = restored;
    }
  }

  function registerTaskToolIfMissing() {
    if (typeof pi.registerTool !== "function") return;
    const existing =
      typeof pi.getAllTools === "function"
        ? pi.getAllTools().some((tool) => tool.name === "manage_todo_list")
        : false;
    if (existing) return;
    pi.registerTool({
      name: "manage_todo_list",
      label: "Task Plan",
      description:
        "Read or replace the task plan shown in the Omnigent Tasks panel.",
      promptSnippet: "Read or replace the current task plan",
      promptGuidelines: [
        "For every multi-step task, you must call manage_todo_list before using other tools to create a plan, then update it after each step until all tasks are completed.",
      ],
      parameters: {
        type: "object",
        properties: {
          operation: { type: "string", enum: ["read", "write"] },
          todoList: {
            type: "array",
            items: {
              type: "object",
              properties: {
                id: { type: "integer" },
                title: { type: "string" },
                description: { type: "string" },
                status: {
                  type: "string",
                  enum: ["not-started", "in-progress", "completed"],
                },
              },
              required: ["id", "title", "description", "status"],
              additionalProperties: false,
            },
          },
        },
        required: ["operation"],
        additionalProperties: false,
      },
      async execute(_toolCallId, params) {
        if (params && params.operation === "read") {
          return {
            content: [
              { type: "text", text: JSON.stringify(taskList, null, 2) },
            ],
            details: { operation: "read", todos: taskList },
          };
        }
        if (!params || params.operation !== "write") {
          throw new Error("operation must be read or write");
        }
        const next = normalizeTaskList(params.todoList);
        if (!next) throw new Error("todoList must contain valid task items");
        taskList = next;
        await publishTaskList();
        return {
          content: [
            {
              type: "text",
              text: `Task plan updated (${taskList.length} task${taskList.length === 1 ? "" : "s"}).`,
            },
          ],
          details: { operation: "write", todos: taskList },
        };
      },
    });
  }

  async function syncTaskListFromResult(event) {
    if (!event || event.toolName !== "manage_todo_list" || event.isError)
      return;
    const result =
      event.result && typeof event.result === "object" ? event.result : event;
    const next = normalizeTaskList(result.details && result.details.todos);
    if (!next) return;
    const changed = JSON.stringify(next) !== JSON.stringify(taskList);
    taskList = next;
    if (changed) await publishTaskList();
  }

  // Cumulative session token usage. Pi reports PER-MESSAGE counts (one
  // assistant message per LLM call); session billing is their SUM — each call
  // is billed for the full context it re-sent, so summing per-message inputs is
  // the correct cumulative input. The server applies vendor pricing to these
  // cumulative totals and republishes a ``session.usage`` event (the SAME
  // contract claude-native / codex-native / cursor-native use), so the web
  // Session-cost badge + per-model token breakdown light up with no
  // server/frontend changes. Dedup by message fingerprint so a re-emitted
  // ``message_end`` / ``turn_end`` / ``agent_end`` carrying the same assistant
  // message never double-counts. ``usageModel`` tracks the latest message's
  // model (mirrors a mid-session model switch). ``lastPostedUsageKey`` dedups
  // the POST itself so a flush with no new tokens is skipped.
  const countedUsageMessages = new Set();
  let cumulativeInputTokens = 0;
  let cumulativeOutputTokens = 0;
  let cumulativeCacheReadTokens = 0;
  let usageModel = null;
  let lastPostedUsageKey = "";
  let lastCacheUsage = null;
  let lastCacheWarmEntryId = null;
  let cacheWarmPoller = null;

  // Build a stable fingerprint for one assistant message so the same message
  // arriving on multiple lifecycle events is only counted once. Pi's
  // ``AssistantMessage`` (``@earendil-works/pi-ai``) carries NO ``id`` field
  // but DOES carry an optional provider ``responseId`` and a required numeric
  // ``timestamp`` — both stable across the same message's re-emission on
  // ``message_end`` / ``turn_end`` / ``agent_end``. Prefer those identity
  // fields (plus a forward-compat ``id``) over the usage-count fingerprint:
  // hashing counts alone collides two DISTINCT LLM calls that happen to report
  // identical token counts (e.g. two identical short acks under prompt
  // caching), which would silently drop the second call's tokens (undercount).
  // The usage-count fingerprint stays only as a last resort for a message that
  // carries no identity field at all.
  function usageMessageKey(message, usage) {
    if (message && typeof message === "object") {
      if (typeof message.id === "string" && message.id)
        return `id:${message.id}`;
      if (typeof message.responseId === "string" && message.responseId)
        return `rid:${message.responseId}`;
      if (typeof message.timestamp === "number")
        return `ts:${message.timestamp}`;
    }
    return `u:${usage.input}-${usage.output}-${usage.cacheRead}-${usage.cacheWrite}-${usage.total}-${usage.provider || ""}-${usage.model || ""}`;
  }

  // Fold one assistant message's usage into the cumulative session totals,
  // deduped by fingerprint. Returns true when it counted (totals advanced).
  function accumulateUsage(message) {
    const usage = extractPiUsage(message);
    if (!usage) return false;
    const key = usageMessageKey(message, usage);
    if (countedUsageMessages.has(key)) return false;
    countedUsageMessages.add(key);
    // The server's ``cumulative_input_tokens`` is INCLUSIVE of cache reads (it
    // splits the cache portion back out and prices it at the cache-read rate),
    // so add cacheRead into the input total. ``cacheWrite`` (cache creation)
    // has no dedicated cumulative field on the server, so fold it into the
    // input total too — it is then priced at the input rate rather than the
    // ~1.25x cache-write rate, a small, documented approximation that never
    // drops the tokens.
    cumulativeInputTokens += usage.input + usage.cacheRead + usage.cacheWrite;
    cumulativeOutputTokens += usage.output;
    cumulativeCacheReadTokens += usage.cacheRead;
    if (usage.model) usageModel = usage.model;
    lastCacheUsage = {
      observedAt: Math.floor(Date.now() / 1000),
      read: usage.cacheRead,
      write: usage.cacheWrite,
      model: usage.model,
      provider: usage.provider,
    };
    return true;
  }

  // POST the cumulative session usage so the server prices it and publishes a
  // ``session.usage`` event. Cumulative (SET) semantics — the server overwrites
  // its stored totals each flush. Deduped so a flush with no advance is a
  // no-op. Fail-open via ``postEvent`` so a failed POST never wedges Pi.
  async function postSessionUsage(ctx) {
    const data = {
      cumulative_input_tokens: cumulativeInputTokens,
      cumulative_output_tokens: cumulativeOutputTokens,
      cumulative_cache_read_input_tokens: cumulativeCacheReadTokens,
    };
    if (usageModel) data.model = usageModel;
    if (lastCacheUsage) {
      const currentModel = ctx && ctx.model;
      const retention = process.env.PI_CACHE_RETENTION === "long" ? "long" : "short";
      const sameModelAndProvider = currentModel &&
        lastCacheUsage.model && lastCacheUsage.model === currentModel.id &&
        lastCacheUsage.provider && lastCacheUsage.provider === currentModel.provider;
      const ttl = sameModelAndProvider && currentModel.promptCache &&
        currentModel.promptCache[retention];
      data.pi_cache_ttl_seconds = Number.isInteger(ttl) && ttl > 0 ? ttl : null;
      if (data.pi_cache_ttl_seconds !== null) {
        data.pi_cache_observed_at = lastCacheUsage.observedAt;
        data.pi_cache_read_input_tokens = lastCacheUsage.read;
        data.pi_cache_write_input_tokens = lastCacheUsage.write;
      }
    }
    if (ctx && typeof ctx.getContextUsage === "function") {
      try {
        const snapshot = ctx.getContextUsage();
        if (snapshot && typeof snapshot === "object") {
          if (Number.isInteger(snapshot.tokens) && snapshot.tokens >= 0)
            data.context_tokens = snapshot.tokens;
          if (Number.isInteger(snapshot.contextWindow) && snapshot.contextWindow > 0)
            data.context_window = snapshot.contextWindow;
        }
      } catch (_err) {
        // Older Pi versions may not expose a usable context snapshot.
      }
    }
    if (!(cumulativeInputTokens || cumulativeOutputTokens || data.context_window ||
          data.pi_cache_ttl_seconds !== undefined)) return;
    const postKey = JSON.stringify(data);
    if (postKey === lastPostedUsageKey) return;
    lastPostedUsageKey = postKey;
    await postEvent(config, { type: "external_session_usage", data });
  }

  async function observeCacheWarmUsage(ctx) {
    if (!ctx || !ctx.sessionManager ||
        typeof ctx.sessionManager.getEntries !== "function") return;
    let entries;
    try { entries = ctx.sessionManager.getEntries(); }
    catch (_err) { return; }
    if (!Array.isArray(entries)) return;
    for (let index = entries.length - 1; index >= 0; index -= 1) {
      const entry = entries[index];
      if (!entry || entry.type !== "usage" || entry.kind !== "cache_warm") continue;
      if (entry.id === lastCacheWarmEntryId) return;
      lastCacheWarmEntryId = entry.id;
      const observedAt = Math.floor(Date.parse(entry.timestamp) / 1000);
      const usage = extractPiUsage({ role: "assistant", usage: entry.usage,
        model: entry.model, provider: entry.provider });
      if (!Number.isInteger(observedAt) || !usage ||
          (lastCacheUsage && observedAt < lastCacheUsage.observedAt)) return;
      lastCacheUsage = {
        observedAt, read: usage.cacheRead, write: usage.cacheWrite,
        model: usage.model, provider: usage.provider,
      };
      await postSessionUsage(ctx);
      return;
    }
  }

  function rememberContext(ctx) {
    if (ctx) latestContext = ctx;
  }

  function newResponseId(prefix) {
    return `pi-${prefix}-${Date.now()}-${++sequence}`;
  }

  function currentResponseId() {
    if (!activeResponseId) activeResponseId = newResponseId("turn");
    return activeResponseId;
  }

  function hasPendingInterrupt() {
    if (!pendingInterruptUntil) return false;
    if (Date.now() > pendingInterruptUntil) {
      pendingInterruptUntil = 0;
      return false;
    }
    return true;
  }

  function safeIsIdle(ctx) {
    // Returns true/false from the SDK's isIdle(), or null when the signal is
    // unavailable (older SDK) or throws, so the caller can fall back.
    // Deliberately returns null (not true) on throw so callers fall back to loop
    // state (!agentRunning) rather than blindly treating the agent as idle.
    if (!ctx || typeof ctx.isIdle !== "function") return null;
    try {
      return ctx.isIdle();
    } catch (_err) {
      return null;
    }
  }

  function requestInterrupt(ctx) {
    // ctx.abort() is a silent no-op when the Pi agent is idle (it does NOT
    // throw), so an interrupt that arrives with no live turn must NOT arm the
    // replay window — otherwise the 30s window poisons the next legitimately
    // started turn (F18). Only arm when a turn is genuinely in-flight: prefer
    // the SDK's isIdle(), and fall back to the agent loop state on SDK versions
    // that don't expose it.
    const idle = safeIsIdle(ctx);
    const turnIsIdle = idle === null ? !agentRunning : idle;
    if (turnIsIdle) return false;
    const accepted = interruptActiveContext(ctx);
    if (!accepted) return false;
    pendingInterruptUntil = Date.now() + pendingInterruptMs;
    return true;
  }

  function replayPendingInterrupt(ctx) {
    if (!hasPendingInterrupt()) return false;
    interruptActiveContext(ctx);
    return true;
  }

  function clearPendingInterrupt() {
    pendingInterruptUntil = 0;
  }

  async function postToolCall(toolCall, responseId) {
    if (!toolCall || typeof toolCall !== "object") return;
    const callId = String(toolCall.id || toolCall.toolCallId || "");
    const name = String(toolCall.name || toolCall.toolName || "");
    if (!callId || !name) return;
    const key = `${responseId}:${callId}`;
    toolCallsById.set(callId, { key, responseId, name });
    if (postedToolCalls.has(key)) return;
    postedToolCalls.add(key);
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: responseId,
        item_type: "function_call",
        item_data: {
          agent: "Pi",
          name,
          arguments: safeJsonStringify(
            toolCall.arguments ?? toolCall.input ?? {},
          ),
          call_id: callId,
        },
      },
    });
  }

  async function postToolResult(event, responseId) {
    if (!event || typeof event !== "object") return;
    const callId = String(event.toolCallId || event.id || "");
    if (!callId) return;
    const known = toolCallsById.get(callId);
    const key = known && known.key ? known.key : `${responseId}:${callId}`;
    if (postedToolResults.has(key)) return;
    postedToolResults.add(key);
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: known && known.responseId ? known.responseId : responseId,
        item_type: "function_call_output",
        item_data: {
          call_id: callId,
          output: textFromToolResult(event),
        },
      },
    });
  }

  async function postCompletedReasoning(text, responseId, keyHint, streamed) {
    if (typeof text !== "string" || !text.trim()) return;
    const textKey = `${responseId}:text:${fingerprint(text)}`;
    const key = `${responseId}:${keyHint || fingerprint(text)}`;
    if (postedReasoning.has(key) || postedReasoning.has(textKey)) return;
    postedReasoning.add(key);
    postedReasoning.add(textKey);
    if (!streamed) {
      queuePreview("external_output_reasoning_delta",
        { delta: text, started: true }, null);
    }
    await flushPreviews();
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: responseId,
        item_type: "reasoning",
        item_data: {
          agent: "Pi",
          summary: [],
          content: [{ type: "reasoning_text", text }],
        },
      },
    });
  }

  function reasoningBlockKey(responseId, contentIndex) {
    return `${responseId}:msg:${streamingMessageOrdinal}:thinking:${contentIndex}`;
  }

  function postReasoningDelta(update, responseId) {
    if (typeof update.delta !== "string" || !update.delta) return;
    const blockKey = reasoningBlockKey(responseId, update.contentIndex);
    const started = !streamedReasoningBlocks.has(blockKey);
    streamedReasoningBlocks.add(blockKey);
    queuePreview("external_output_reasoning_delta",
      { delta: update.delta, started }, blockKey);
  }

  function streamingMessageId(responseId) {
    // Stable per-assistant-message id across all of the message's text
    // chunks. The web UI keys an in-flight "live:<message_id>" preview off
    // this, appends each chunk in `index` order, and reconciles it against
    // the authoritative assistant item by FIFO retirement. responseId
    // scopes it to this turn and the ordinal distinguishes successive
    // assistant messages within the turn, so a finalized message's id is
    // never reused by a later one.
    return `${responseId}:msg:${streamingMessageOrdinal}`;
  }

  function postOutputTextDelta(messageId, delta, options) {
    // Transient assistant-text chunk for live preview (Responses-style
    // response.output_text.delta on the wire). Not persisted; the
    // authoritative final text arrives separately via
    // external_conversation_item. A blank, non-final delta carries no
    // signal — skip it so an empty token can't churn the UI buffer.
    const final = !!(options && options.final);
    if (typeof delta !== "string") return;
    if (!delta && !final) return;
    queuePreview("external_output_text_delta",
      { delta, message_id: messageId, final },
      final ? null : `${messageId}:block:${options && options.contentIndex}`);
  }

  function postTextDelta(update, responseId) {
    // assistantMessageEvent of type "text_delta": one streamed token of
    // the current assistant message. All text blocks of the message share
    // its id, so the preview reads as one growing message.
    if (!update || typeof update.delta !== "string" || !update.delta) return;
    const messageId = streamingMessageId(responseId);
    if (finalizedTextBlocks.has(messageId)) return;
    postOutputTextDelta(messageId, update.delta, { contentIndex: update.contentIndex });
  }

  async function finalizeStreamingMessage(responseId) {
    // Emit a final-marker delta so the web UI knows no further chunks will
    // arrive for this message_id and can stop the live buffer. The marker
    // carries no new text (the running preview already holds the full
    // message); message_end posts the authoritative item that replaces the
    // preview in place. Only finalize a message we actually streamed (a
    // message with no text_delta has no live preview to close).
    const messageId = streamingMessageId(responseId);
    if (finalizedTextBlocks.has(messageId)) return;
    if (!streamedTextIndex.has(messageId) &&
        !previewQueue.some((entry) => entry.data.message_id === messageId)) return;
    finalizedTextBlocks.add(messageId);
    postOutputTextDelta(messageId, "", { final: true });
    await flushPreviews();
  }

  async function mirrorAssistantMessage(message, responseId) {
    const blocks = contentBlocks(message);
    for (let index = 0; index < blocks.length; index += 1) {
      const block = blocks[index];
      if (!block || typeof block !== "object") continue;
      if (block.type === "toolCall") await postToolCall(block, responseId);
      if (block.type === "thinking") {
        const text = typeof block.thinking === "string" ? block.thinking : "";
        const key = block.thinkingSignature || `${turnOrdinal}:${index}`;
        const blockKey = reasoningBlockKey(responseId, index);
        await postCompletedReasoning(
          text,
          responseId,
          key,
          streamedReasoningBlocks.has(blockKey),
        );
      }
    }
  }

  pi.registerCommand("omnigent", {
    description: "Show the Omnigent conversation URL",
    async handler(_args, ctx) {
      setOmnigentStatus(config, ctx, "linked");
      if (ctx && ctx.ui && config && config.conversationUrl) {
        ctx.ui.notify(`Omnigent: ${config.conversationUrl}`, "info");
      }
    },
  });

  pi.on("before_agent_start", (event) => {
    const instructions = config && config.startupInstructions;
    if (typeof instructions !== "string" || !instructions.trim()) return;
    const nativePrompt = event && typeof event.systemPrompt === "string"
      ? event.systemPrompt : "";
    if (nativePrompt === instructions || nativePrompt.endsWith(`\n\n${instructions}`)) return;
    return { systemPrompt: nativePrompt ? `${nativePrompt}\n\n${instructions}` : instructions };
  });

  async function resumeRotation(ctx) {
    if (rotationInFlight) return true;
    rotationInFlight = true;
    clearTimeout(pi.__omnigentRotationRetry);
    try {
      const successor = await rotateNativeSession(config);
      if (!successor) rotationPending = false;
      return !!successor;
    } catch (error) {
      if (ctx && ctx.ui && typeof ctx.ui.notify === "function")
        ctx.ui.notify(`Omnigent handover is pending; retrying: ${error.message}`, "error");
      pi.__omnigentRotationRetry = setTimeout(() => resumeRotation(ctx), 1000);
      pi.__omnigentRotationRetry.unref();
      return true;
    } finally {
      rotationInFlight = false;
    }
  }

  pi.on("session_shutdown", async () => {
    if (cacheWarmPoller) clearInterval(cacheWarmPoller);
    cacheWarmPoller = null;
    clearTimeout(pi.__omnigentRotationRetry);
    clearInterval(pi.__omnigentInboxPoller);
    pi.__omnigentInboxPoller = null;
  });

  pi.on("session_start", async (event, ctx) => {
    rememberContext(ctx);
    if (cacheWarmPoller) clearInterval(cacheWarmPoller);
    cacheWarmPoller = null;
    if (config && config.serverUrl && config.sessionId &&
        event && event.reason === "startup" && !config.pendingRotationSource) {
      try {
        const source = await rotationRequest(config, "GET",
          `/v1/sessions/${encodeURIComponent(config.sessionId)}`);
        const pending = source && source.labels && source.labels["omnigent.rotation_successor"];
        if (pending) {
          const prepared = JSON.parse(pending);
          if (prepared.revision === source.archive_revision ||
              source.archived && source.labels["omnigent.succeeded_by"] === prepared.id)
            config.pendingRotationSource = config.sessionId;
        }
      } catch (_error) {
        // Ordinary startup retains the existing permission bootstrap behavior.
      }
    }
    if (config && config.serverUrl && config.sessionId &&
        (event && event.reason === "new" || config.pendingRotationSource ||
        config.pendingSuccessorId)) {
      rotationPending = true;
      clearInterval(pi.__omnigentInboxPoller);
      pi.__omnigentInboxPoller = null;
      if (await resumeRotation(ctx)) return;
    }
    cacheWarmPoller = setInterval(() => {
      void observeCacheWarmUsage(latestContext);
    }, 15000);
    cacheWarmPoller.unref?.();
    await observeCacheWarmUsage(ctx);
    registerTaskToolIfMissing();
    const permissionState = readPermissionState(config);
    if (permissionState && typeof pi.getActiveTools === "function") {
      const currentTools = pi.getActiveTools();
      if (Array.isArray(currentTools) && currentTools.every((name) => typeof name === "string")) {
        baselineActiveTools = currentTools;
        const active = activeToolsForTier(pi, config, baselineActiveTools, permissionState.tier);
        markPermissionInstallation(pi, config, permissionState, active);
      }
    }
    restoreTaskList(ctx);
    if (taskList.length) await publishTaskList();
    setOmnigentStatus(config, ctx, "linked");
    startInboxPoller(
      pi,
      config,
      () => requestInterrupt(latestContext),
      async (customInstructions) => {
        const responseId = turnStatusResponseId ?? newResponseId("compact");
        compacting = true;
        try {
          await postEvent(config, {
            type: "external_session_status",
            data: { status: "running", response_id: responseId },
          });
          await triggerCompaction(config, latestContext, customInstructions);
        } finally {
          if (!agentRunning) {
            await postEvent(config, {
              type: "external_session_status",
              data: { status: "idle", response_id: responseId },
            });
          }
          compacting = false;
        }
      },
      (model) => applyModelChange(pi, config, latestContext, model),
      (level) => {
        settingThinkingLevel = true;
        try {
          pi.setThinkingLevel(level);
        } catch (_err) {
          // Report the unchanged effective value if Pi rejects the request.
        } finally {
          settingThinkingLevel = false;
        }
        // A clamped request can leave Pi unchanged and emit no native event.
        return reportThinkingLevel(true);
      },
      () => {
        // Prefer the SDK's live idle signal; fall back to the agent loop
        // state on SDK versions that don't expose isIdle() (same fallback
        // as requestInterrupt).
        const idle = safeIsIdle(latestContext);
        return idle === null ? agentRunning : !idle;
      },
      () => compacting,
      (payload) => {
        const state = readPermissionState(config);
        const target = payload && payload.target_tier;
        const idle = safeIsIdle(latestContext);
        const turnActive = idle === null ? agentRunning : !idle;
        const valid = state && state.status === "switching" &&
          state.revision === payload.expected_revision &&
          PERMISSION_TIERS.has(target) && baselineActiveTools &&
          (target === "full" || permissionExtensionOrderProven()) &&
          !turnActive;
        const active = valid
          ? activeToolsForTier(pi, config, baselineActiveTools, target)
          : null;
        const attested = active && markPermissionInstallation(
          pi,
          config,
          { ...state, tier: target,
            revision: state.revision + (target === state.tier ? 0 : 1) },
          active,
        );
        try {
          writePermissionControlFile(config, "permission_ack.json", {
            id: payload.id,
            session_id: config.sessionId,
            target_tier: target,
            expected_revision: payload.expected_revision,
            ok: !!attested,
            active_tools: active || [],
          });
        } catch (_err) {}
      },
    );
    markInputReady(config);
    const nativeSessionId =
      ctx && ctx.sessionManager && ctx.sessionManager.getSessionId
        ? ctx.sessionManager.getSessionId()
        : undefined;
    await patchExternalSessionId(config, nativeSessionId);
    // Publish Pi's live model catalog so the Web UI picker populates from what
    // Pi actually loaded, independent of how it authenticated.
    await postModelOptions(config, ctx);
    // Report the model Pi launched with so the composer pill and the picker's
    // active row reflect the current model from the start. Without this, a
    // ``/login`` session (no Omnigent ``model_override``, no ``llm_model``)
    // shows no active model until the user switches. Mirrors the
    // ``model_select`` handler, but for the startup value ``ctx.model``.
    const startupModel = modelReference(ctx ? ctx.model : undefined);
    if (startupModel) {
      await postEvent(config, {
        type: "external_model_change",
        data: { model: startupModel },
      });
    }
    await reportThinkingLevel();
    // Readiness is not turn completion: a queued prompt may already be running.
    // Only agent_end publishes idle so startup cannot complete a child task.
  });

  pi.on("session_tree", async (_event, ctx) => {
    restoreTaskList(ctx);
    await publishTaskList();
  });

  pi.on("thinking_level_select", async () => {
    if (!settingThinkingLevel) await reportThinkingLevel();
  });

  pi.on("model_select", async (event, ctx) => {
    rememberContext(ctx);
    await reportThinkingLevel();
    // Mirror a model switch made inside the Pi TUI (the ``/model`` command or
    // Ctrl+P cycling) back to Omnigent so the web picker reflects it. Skip
    // ``restore`` — that is Pi re-applying the session's saved model at
    // startup, not a user switch, and posting it could clobber a pending
    // web-side override. The server dedups against ``model_override``, so a
    // web-initiated switch (which already persisted the value before queuing
    // the inbox ``model_change``) round-trips here as a no-op.
    const source =
      event && typeof event.source === "string" ? event.source : "";
    if (source === "restore") {
      await postEvent(config, {
        type: "external_session_usage", data: { pi_cache_ttl_seconds: null },
      });
      return;
    }
    const model = event && event.model ? event.model : undefined;
    const selectedModel = modelReference(model);
    if (!selectedModel) {
      await postEvent(config, {
        type: "external_session_usage", data: { pi_cache_ttl_seconds: null },
      });
      return;
    }
    await postEvent(config, {
      type: "external_model_change",
      data: { model: selectedModel },
    });
    if (lastCacheUsage) await postSessionUsage(ctx);
    else await postEvent(config, {
      type: "external_session_usage", data: { pi_cache_ttl_seconds: null },
    });
  });

  pi.on("after_provider_response", async (event) => {
    const raw = event && event.headers;
    const headers = {};
    for (const [name, value] of Object.entries(raw && typeof raw === "object" ? raw : {})) {
      const normalized = name.toLowerCase();
      if (
        /^anthropic-ratelimit-(requests|tokens|input-tokens|output-tokens)-(limit|remaining|reset)$/.test(normalized) &&
        typeof value === "string" && value.length <= 128
      ) {
        headers[normalized] = value;
      }
    }
    await postEvent(config, {
      type: "external_session_usage",
      data: { pi_provider_usage_limits_headers: headers },
    });
  });

  pi.on("cache_warming_decision", async (event) => {
    if (!event || event.action !== "warm") return { action: "stop" };
    // Pi's running-turn policy remains its own. Idle refresh also needs the
    // owner's live Omnigent switch and cap, checked at each planned refresh.
    if (event.continuationProbability === 1) return { action: "warm" };
    try {
      const response = await postEventChecked(config, {
        type: "external_session_usage",
        data: { pi_cache_warming_probe: true },
      });
      const policy = response && await response.json();
      return { action: policy && policy.pi_cache_warming_allowed === true ? "warm" : "stop" };
    } catch (_err) {
      return { action: "stop" };
    }
  });

  pi.on("agent_start", async (_event, ctx) => {
    if (rotationPending || config && config.pendingRotationSource) {
      if (ctx && typeof ctx.abort === "function") ctx.abort();
      return;
    }
    rememberContext(ctx);
    // A brand-new agent loop must never inherit a replay window armed before it
    // began (e.g. a spuriously-armed window from an interrupt that landed while
    // idle). A legitimate interrupt that arrives after this point belongs to
    // this loop and can still arm/replay; agent_end clears once the loop
    // completes. See F18.
    clearPendingInterrupt();
    await flushPreviews();
    previewDisabled = false;
    agentRunning = true;
    setOmnigentStatus(config, ctx, "running");
    activeResponseId = null;
    turnOrdinal = 0;
    postedToolCalls.clear();
    postedToolResults.clear();
    postedReasoning.clear();
    postedAssistantMessages.clear();
    streamedReasoningBlocks.clear();
    toolCallsById.clear();
    streamedTextIndex.clear();
    finalizedTextBlocks.clear();
    streamingMessageOrdinal = 0;
    // Pin the status response_id for this agent loop through agent_end.
    turnStatusResponseId = `pi-${Date.now()}-${++sequence}`;
    await postEvent(config, {
      type: "external_session_status",
      data: {
        status: "running",
        response_id: turnStatusResponseId,
      },
    });
  });

  pi.on("agent_end", async (event, ctx) => {
    rememberContext(ctx);
    clearPendingInterrupt();
    agentRunning = false;
    setOmnigentStatus(config, ctx, "idle");
    activeResponseId = null;
    // Last-chance usage capture from the agent loop's final message set, in
    // case neither ``message_end`` nor ``turn_end`` carried usage for some
    // call. ``event.messages`` may hold the whole conversation; the
    // fingerprint dedup means re-scanning already-counted messages is a no-op,
    // so a plain forward-scan is safe (no overcount).
    const messages =
      event && Array.isArray(event.messages) ? event.messages : [];
    for (const message of messages) {
      accumulateUsage(message);
    }
    await postSessionUsage(ctx);
    await flushPreviews();
    // Reuse the agent_start response_id so the web client matches the idle
    // edge and clears the "streaming" status, unblocking queued follow-ups.
    const endResponseId =
      turnStatusResponseId ?? `pi-${Date.now()}-${++sequence}`;
    turnStatusResponseId = null;
    // Manual compact aborts the turn first; its own completion publishes idle.
    if (compacting) return;
    await postEvent(config, {
      type: "external_session_status",
      data: { status: "idle", response_id: endResponseId },
    });
  });

  pi.on("turn_start", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const index =
      event && typeof event.turnIndex === "number"
        ? event.turnIndex
        : turnOrdinal + 1;
    turnOrdinal = index;
    activeResponseId = newResponseId(`turn-${turnOrdinal}`);
  });

  pi.on("message_update", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const responseId = currentResponseId();
    const update = event ? event.assistantMessageEvent : undefined;
    if (!update || typeof update !== "object") return;
    if (update.type === "text_delta") {
      postTextDelta(update, responseId);
      return;
    }
    if (update.type === "thinking_delta") {
      postReasoningDelta(update, responseId);
      return;
    }
    if (update.type === "toolcall_end") {
      await flushPreviews();
      await postToolCall(update.toolCall, responseId);
      return;
    }
    if (update.type === "thinking_end") {
      const key = `${turnOrdinal}:${update.contentIndex}`;
      const blockKey = reasoningBlockKey(responseId, update.contentIndex);
      await postCompletedReasoning(
        update.content,
        responseId,
        key,
        streamedReasoningBlocks.has(blockKey),
      );
    }
  });

  pi.on("tool_execution_start", async (_event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
  });

  pi.on("tool_call", async (event, ctx) => {
    rememberContext(ctx);
    if (rotationPending || rotationInFlight || config && config.pendingRotationSource)
      return { block: true, reason: "Session handover is pending" };
    const blocked = replayPendingInterrupt(ctx);
    const responseId = currentResponseId();
    await postToolCall(
      {
        id: event && event.toolCallId,
        name: event && event.toolName,
        arguments: event && event.input,
      },
      responseId,
    );
    if (blocked) {
      return { block: true, reason: "Interrupted by user" };
    }
    const permissionState = readPermissionState(config);
    if (!permissionState || permissionState.status !== "ready") {
      return { block: true, reason: "Permission state needs recovery; tools are paused" };
    }
    if (!event || typeof event.toolCallId !== "string" || !event.toolCallId ||
        !event.input || typeof event.input !== "object" || Array.isArray(event.input)) {
      return { block: true, reason: "invalid tool call" };
    }
    if (permissionState.tier !== "full" && permissionToolRegistry &&
        !permissionToolSources.matches(pi, permissionToolRegistry, event.toolName)) {
      return { block: true, reason: "Pi extension registration changed; restart the session to verify its source" };
    }
    // Bridged Omnigent tools (registered via pi.registerTool above) are
    // policy-evaluated server-side inside the /mcp proxy when execute() runs,
    // so skip the hook-level eval for them to avoid double-evaluation and, for
    // ASK policies, a double prompt. Pi's own built-in tools (read/shell/etc)
    // are NOT bridged and stay gated here.
    if (
      bridgedTools.has((event && event.toolName) || "") &&
      !piNativeTools.has(event.toolName) &&
      (!permissionToolRegistry || permissionToolSources.isBridge(
        permissionToolRegistry, event.toolName, __filename))
    ) {
      return;
    }
    // Evaluate TOOL_CALL policy via the Omnigent server's session-level HTTP
    // endpoint. This works even after the harness turn has completed (which
    // happens immediately for pi-native — just enqueue + TurnComplete), so
    // the verdict is always evaluated against live session policies regardless
    // of whether an Omnigent turn is currently in flight.
    const verdict = await evalNativePolicyHttp(
      config,
      event && event.toolName,
      event && event.input,
      event && event.toolCallId,
    );
    if (verdict.block) {
      return {
        block: true,
        reason: verdict.reason || "blocked by Omnigent policy",
      };
    }
    if (permissionState.tier !== "full") {
      permittedNativeCalls.set(event.toolCallId, {
        revision: permissionState.revision, name: event.toolName, args: event.input,
      });
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const callId = event && event.toolCallId;
    await finishNativePermissionCall(callId, event);
    await syncTaskListFromResult(event);
    await postToolResult(event, currentResponseId());
  });

  pi.on("tool_execution_end", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const callId = event && event.toolCallId;
    await finishNativePermissionCall(callId, event);
    await postToolResult(event, currentResponseId());
  });

  pi.on("input", async (event, ctx) => {
    rememberContext(ctx);
    setOmnigentStatus(config, ctx, "running");
  });

  pi.on("message_end", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    setOmnigentStatus(config, ctx, undefined);
    let message = event ? event.message : undefined;
    const role = messageRole(message);
    if (role === "user") {
      // Pi emits input before preflight and while steering is still queued;
      // message_end is the first durable signal that it consumed the prompt.
      const text = textFromMessage(message).trim();
      if (!text) return;
      await postEvent(config, {
        type: "external_conversation_item",
        data: {
          response_id: `pi-consumed-user-${Date.now()}-${++sequence}`,
          item_type: "message",
          item_data: {
            role: "user",
            content: [{ type: "input_text", text }],
          },
        },
      });
      return;
    }
    if (role !== "assistant") return;
    const authFailure = nativeAuthFailure(message, ctx);
    if (authFailure) {
      message = { ...message, errorMessage: authFailure.message };
      if (ctx && ctx.ui && ctx.ui.notify)
        ctx.ui.notify(authFailure.message, "error");
    }
    const responseId = currentResponseId();
    const messageId = streamingMessageId(responseId);
    // Close the live preview for this message (no-op if nothing streamed),
    // then bump the ordinal so the NEXT assistant message of this turn
    // streams under a fresh, distinct id and never reuses this one's. The
    // ordinal advances here (not on message_start) so the deltas just
    // posted and this finalize agree on the id regardless of whether Pi
    // fires message_start.
    await finalizeStreamingMessage(responseId);
    await mirrorAssistantMessage(message, responseId);
    previewDisabled = false;
    streamingMessageOrdinal += 1;
    // ``message_end`` is the primary usage-capture site (one completed
    // assistant message per LLM call); fold its token counts into the
    // cumulative session totals and flush to the server for pricing.
    accumulateUsage(message);
    await postSessionUsage(ctx);
    // Surface Pi-reported errors (e.g. 404 for unknown model ids, 400 for
    // unsupported API types) as visible error items in the web UI so users
    // aren't left staring at an empty turn.
    const stopReason =
      message && typeof message.stopReason === "string"
        ? message.stopReason
        : "";
    const errorMessage =
      message && typeof message.errorMessage === "string"
        ? message.errorMessage
        : "";
    if (stopReason === "error" && errorMessage) {
      await postConversationItem(
        config,
        {
          response_id: responseId,
          item_type: "error",
          item_data: {
            source: "execution",
            code: authFailure ? authFailure.code : "RuntimeError",
            message: `Pi model error: ${errorMessage}`,
          },
        },
        assistantMessageSourceId(message, responseId, errorMessage),
      );
      return;
    }
    const text = textFromMessage(message);
    if (!text) return;
    const sourceId = assistantMessageSourceId(message, responseId, text);
    if (postedAssistantMessages.has(sourceId)) return;
    // The durable identity also suppresses a timed-out preview applied late.
    await postConversationItem(
      config,
      {
        response_id: responseId,
        message_id: messageId,
        item_type: "message",
        item_data: {
          role: "assistant",
          agent: "Pi",
          content: [{ type: "output_text", text }],
        },
      },
      sourceId,
    );
    // Retry a failed callback on its next delivery; only suppress duplicates
    // after the authoritative POST has been accepted.
    postedAssistantMessages.add(sourceId);
  });

  pi.on("turn_end", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const responseId = currentResponseId();
    await mirrorAssistantMessage(event && event.message, responseId);
    // Fallback usage capture: if Pi attached usage to the turn's final
    // assistant message but no ``message_end`` carried it, fold it in here.
    // Deduped by fingerprint, so a message already counted on ``message_end``
    // is a no-op.
    accumulateUsage(event && event.message);
    await postSessionUsage(ctx);
    const results =
      event && Array.isArray(event.toolResults) ? event.toolResults : [];
    for (const result of results) {
      await postToolResult(result, responseId);
    }
  });
};
