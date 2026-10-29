const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { pathToFileURL } = require("node:url");

// Generated parent/child extensions share actual SDK-object keys, not names or payload flags.
const key = Symbol.for("omnigent.pi.native-support.v2");
const store = globalThis[key] ||= { bindings: new WeakMap(), loaders: new WeakMap(), sessions: new WeakMap() };
store.apiRecords ||= new WeakMap();
const plain = value => value && typeof value === "object" && !Array.isArray(value);
const text = value => typeof value === "string" && value.trim().length > 0;
const integer = (value, minimum = 1) => Number.isSafeInteger(value) && value >= minimum;
const allowed = (value, keys) => plain(value) && Object.keys(value).every(key => keys.includes(key));
const hash = value => crypto.createHash("sha256").update(value).digest("hex");
function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (plain(value)) return Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])]));
  if (typeof value === "number" && !Number.isFinite(value)) throw new Error("Non-JSON native input");
  return value;
}
const digest = value => hash(JSON.stringify(stable(value)));
function rules() {
  const value = require("../../permission_tiers/native_support_rules.json");
  if (value.version !== 1 || !Object.entries(value).every(([name, entries]) => name === "version" || Array.isArray(entries) && entries.every(text)))
    throw new Error("Unsupported native support rules");
  return value;
}
function inside(file, root) {
  const relative = path.relative(root, file);
  return relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}
const identity = info => [info.dev, info.ino, info.mode, info.size, info.mtimeMs, info.ctimeMs].join(":");
function snapshot(file, optional = false) {
  const lexical = path.resolve(file);
  let before;
  try { before = fs.lstatSync(lexical); }
  catch (error) {
    if (optional && error.code === "ENOENT") return { path: lexical, resolved_path: null, sha256: null };
    throw error;
  }
  if (!fs.constants.O_NOFOLLOW) throw new Error("No-follow validation unavailable");
  const resolved = fs.realpathSync(lexical);
  const target = fs.lstatSync(resolved);
  if (!target.isFile()) throw new Error("Native input is not a file");
  const fd = fs.openSync(resolved, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  try {
    const opened = fs.fstatSync(fd);
    if (identity(opened) !== identity(target)) throw new Error("Native input changed before open");
    const bytes = fs.readFileSync(fd);
    if (identity(fs.fstatSync(fd)) !== identity(opened) || identity(fs.lstatSync(lexical)) !== identity(before) ||
        identity(fs.lstatSync(resolved)) !== identity(opened) || fs.realpathSync(lexical) !== resolved)
      throw new Error("Native input changed during read");
    return Object.freeze(Object.defineProperty({ path: lexical, resolved_path: resolved, sha256: hash(bytes) }, "bytes", {value:bytes}));
  } finally { fs.closeSync(fd); }
}
function protectedInput(record, file, optional = false) {
  const value = snapshot(file, optional);
  for (const candidate of [value.path, value.resolved_path].filter(Boolean)) {
    if (record.workspaces.some(root => inside(candidate, root)) && !record.protectedRoots.some(root => inside(candidate, root)))
      throw new Error("Native launch input is model-writable");
  }
  return value;
}
function inputContext(workspace, bridgeDir) {
  const lexicalWorkspace=path.resolve(workspace);
  const realWorkspace=fs.realpathSync(lexicalWorkspace);
  const roots=[path.join(require("node:os").homedir(),".pi"),path.join(require("node:os").homedir(),".agents"),bridgeDir];
  for (const start of new Set([lexicalWorkspace,realWorkspace]))
    for (let directory=start;;directory=path.dirname(directory)) {
      roots.push(path.join(directory,".pi"),path.join(directory,".agents"));
      if (directory===path.dirname(directory)) break;
    }
  return {workspace:realWorkspace,realWorkspace,lexicalWorkspace,workspaces:[lexicalWorkspace,realWorkspace],protectedRoots:roots.filter(text).map(root=>path.resolve(root))};
}
function createInputGuard(workspace, bridgeDir) {
  const context=inputContext(workspace,bridgeDir);
  return (file, optional=false)=>protectedInput(context,file,optional);
}
function toolSnapshot(session) {
  return session.getAllTools().map(tool => ({ name: tool.name, sourceInfo: tool.sourceInfo || null }));
}
function binding(record, event, state) {
  return { scope_id: record.scope.scope_id, installation_id: record.scope.installation_id,
    registry_generation: record.scope.registry_generation, name: event.toolName,
    arguments_digest: digest(event.input), tier_revision: state.revision };
}
function failed(bound, reason) { return { binding: bound, status: "failed_native", action: "ordinary", reason }; }
function semanticAction(name, input, record) {
  const r = rules();
  const sameCwd = value => text(value) && path.resolve(record.workspace, value) === record.workspace &&
    fs.realpathSync(path.resolve(record.workspace, value)) === record.realWorkspace;
  const strings = (object, keys) => keys.every(key => !(key in object) || text(object[key]));
  if (!sameCwd(input.cwd === undefined ? "." : input.cwd)) return null;
  if (name === "bg_wait") return allowed(input, ["id", "nonBlocking", "all", "timeoutMs", "stopOnAttention"]) &&
    strings(input, ["id"]) && ["nonBlocking", "all", "stopOnAttention"].every(key => !(key in input) || typeof input[key] === "boolean") &&
    (!("timeoutMs" in input) || integer(input.timeoutMs)) ? "control" : null;
  if (name === "subagent_supervisor") return Object.keys(input).length === 0 ? "control" : null;
  if (name !== "subagent") return null;
  if (r.read_actions.includes(input.action) || r.control_actions.includes(input.action)) {
    const action = r.read_actions.includes(input.action) ? "read" : "control";
    const options = input.options === undefined ? {} : input.options;
    if (!allowed(input, action === "read" ? r.read_keys : r.control_keys) || !allowed(options, ["agentScope", "topic"]) ||
        !strings(input, ["agent", "id", "runId", "message", "mode", "childId", "toolCallId", "view"]) || !strings(options, ["agentScope", "topic"]) ||
        ("index" in input && !integer(input.index, 0)) || ("lines" in input && !integer(input.lines)) ||
        ("capabilities" in input && typeof input.capabilities !== "boolean")) return null;
    if (!(input.view === undefined || ["fleet","transcript"].includes(input.view)) ||
        !(input.mode === undefined || ["steer","follow_up","auto"].includes(input.mode)) ||
        !(options.agentScope === undefined || ["user","project","both"].includes(options.agentScope))) return null;
    return action;
  }
  if (!allowed(input, r.run_keys) || typeof input.async !== "boolean") return null;
  const options = input.options === undefined ? {} : input.options;
  if (!allowed(options, r.option_keys) || !(options.context === undefined || ["fresh", "fork"].includes(options.context)) ||
      !(options.agentScope === undefined || ["user", "project", "both"].includes(options.agentScope)) ||
      ["timeoutMs", "toolTimeoutMs"].some(key => key in options && !integer(options[key])) ||
      ("includeProgress" in options && typeof options.includeProgress !== "boolean") ||
      ("maxOutput" in options && (!allowed(options.maxOutput, ["bytes", "lines"]) || !Object.values(options.maxOutput).every(value => integer(value))))) return null;
  const forms = ["agent", "tasks", "chain"].filter(key => key in input);
  if (forms.length !== 1) return null;
  const steps = forms[0] === "agent" ? [input] : input[forms[0]];
  if (!Array.isArray(steps) || !steps.length) return null;
  for (const step of steps) {
    if (!plain(step) || (forms[0] !== "agent" && !allowed(step, forms[0] === "chain" ? r.chain_step_keys : r.step_keys))) return null;
    const launch = {...input, ...step};
    if (!text(launch.agent) || !text(launch.task) || !strings(launch, ["model", "as"]) || !sameCwd(launch.cwd === undefined ? "." : launch.cwd)) return null;
  }
  return "delegate";
}
function packageForTool(record, tool, inputs) {
  const source = tool.sourceInfo;
  if (!source || typeof source.path !== "string") return null;
  const candidates = record.loaded.filter(entry => entry.path === source.path || entry.resolvedPath === source.path);
  const targets = new Set(candidates.map(entry => entry.resolvedPath));
  if (targets.size !== 1 || !text(candidates[0]?.resolvedPath)) return null;
  const entry = protectedInput(record, candidates[0].resolvedPath);
  inputs.push(entry);
  let directory = path.dirname(entry.resolved_path);
  while (true) {
    const file = protectedInput(record, path.join(directory, "package.json"), true);
    inputs.push(file);
    if (file.sha256 !== null) {
      const metadata = JSON.parse(file.bytes.toString("utf8"));
      if (metadata.name !== "pi-subagents") return null;
      const apiRecord = record.api && store.apiRecords.get(record.api);
      if (source.scope !== "user" || !/^0\.80\.\d+$/.test(metadata.version || "") ||
          !apiRecord || record.api.packageRoot !== directory || record.api.version !== metadata.version ||
          record.api.SUBAGENT_LAUNCH_CONTRACT_VERSION !== 3 || typeof record.api.resolveSubagentLaunchContract !== "function" ||
          typeof record.api.parseFrontmatter !== "function") throw new Error("Native helper source or compatibility failed");
      return { directory, metadata };
    }
    const parent = path.dirname(directory);
    if (parent === directory) return null;
    directory = parent;
  }
}
function overrides(settings, name, provider) {
  if (!plain(settings)) throw new Error("Invalid native settings");
  if (settings.subagents === undefined) return [];
  if (!plain(settings.subagents)) throw new Error("Invalid native settings");
  if (["agentRoots", "agentDirs", "agentsDir", "agentScanDirs", "agentExcludeDirs"].some(key => key in settings.subagents)) throw new Error("Unreviewed native agent roots");
  const groups = [settings.subagents.agentOverrides];
  if (settings.subagents.agentOverridesByProvider !== undefined) {
    if (!plain(settings.subagents.agentOverridesByProvider)) throw new Error("Invalid provider overrides");
    if (text(provider)) groups.push(settings.subagents.agentOverridesByProvider[provider]);
  }
  return groups.filter(group => group !== undefined).map(group => {
    if (!plain(group) || name in group && !plain(group[name])) throw new Error("Invalid native agent override");
    return group[name] || {};
  });
}
function placement(values) {
  let outside = null;
  for (const value of values) {
    if ("machine" in value && value.machine !== false) {
      if (!text(value.machine)) throw new Error("Unresolved machine placement");
      outside = "remote";
    }
    if (value.runner !== undefined) {
      if (!plain(value.runner) || !["pi", "external-cli", "external-job"].includes(value.runner.type)) throw new Error("Unknown native runner");
      if (value.runner.type === "external-cli") outside ||= "external_cli";
      if (value.runner.type === "external-job") outside ||= "unsupported_runner";
    }
    if (["worktree", "output", "sessionDir", "artifactDir", "artifactsDir", "acceptance", "shellHooks"].some(key => key in value && value[key] !== false))
      throw new Error("Unreviewed native launch configuration");
  }
  return outside;
}
async function classifyNativeCall(event, ctx, state) {
  let bound = null;
  try {
    const record = ctx && store.bindings.get(ctx.sessionManager);
    if (!record || record.closed || !record.isInstalled() || !plain(event) || !text(event.toolName) || !text(event.toolCallId) || !plain(event.input))
      return failed(null, "Native bootstrap or origin is unavailable");
    bound = binding(record, event, state);
    if (!state || state.status !== "ready" || record.getState().revision !== state.revision || record.getState().tier !== state.tier ||
        (record.initialTier === "full" && state.tier !== "full") || ![record.workspace,record.lexicalWorkspace].includes(path.resolve(ctx.cwd)) || fs.realpathSync(ctx.cwd) !== record.realWorkspace)
      return failed(bound, "Native scope or live permission state changed");
    const current = record.session.resourceLoader.getExtensions().extensions;
    if (current.length !== record.loaded.length || current.some((entry, index) => entry !== record.loaded[index]) || current.at(-1) !== record.finalExtension ||
        current.at(-1).handlers.get("tool_call").at(-1) !== record.finalHook || digest(toolSnapshot(record.session)) !== record.registrationDigest)
      return failed(bound, "Native loader, final hook or registrations changed");
    const inputs = [];
    for (const expected of record.inputs) {
      const actual = protectedInput(record, expected.path, expected.sha256 === null);
      if (digest(actual) !== digest(expected)) return failed(bound, "Native bootstrap inputs changed");
      inputs.push(actual);
    }
    const tool = record.session.getAllTools().find(tool => tool.name === event.toolName);
    if (!tool) return failed(bound, "Native tool is not registered in this loader");
    const candidate = ["subagent", "subagent_supervisor", "bg_wait"].includes(event.toolName) || /^(?:npm:)?pi-subagents(?:@|$)/.test(tool.sourceInfo?.source || "");
    const pkg = candidate ? packageForTool(record, tool, inputs) : null;
    if (!pkg) {
      if (/^(?:npm:)?pi-subagents(?:@|$)/.test(tool.sourceInfo?.source || "")) return failed(bound, "Native helper source is unavailable");
      return {binding: bound, status:"attested_native", action:"ordinary", inputs};
    }
    const action = semanticAction(event.toolName, event.input, record);
    if (!action) return failed(bound, "Native helper input, action or cwd is outside the admitted subset");
    if (action !== "delegate") return {binding:bound, status:"attested_native", action, inputs, semantics_digest:digest({action, bound, inputs})};
    if (typeof ctx.isProjectTrusted !== "function" || typeof ctx.modelRegistry?.getAvailable !== "function")
      return failed(bound, "Native preflight context is unavailable");
    const source = {...event.input, ...event.input.options};
    const steps = source.agent ? [source] : source.tasks || source.chain;
    const contracts = [];
    let outside = null;
    for (const step of steps) {
      const launch = {...source, ...step};
      const request = {
        agent:launch.agent, task:launch.task, cwd:record.workspace, model:launch.model,
        context:source.context, agentScope:source.agentScope, parentSessionId:ctx.sessionManager.getSessionId(),
        sessionRoot:ctx.sessionManager.getSessionDir?.(), parentSessionFile:ctx.sessionManager.getSessionFile?.(),
        parentLeafId:ctx.sessionManager.getLeafId?.(), availableModels:ctx.modelRegistry.getAvailable(), parentModel:ctx.model,
        scopedModelIds:(ctx.scopedModels || []).map(item => `${item.model.provider}/${item.model.id}`),
        projectTrusted:ctx.isProjectTrusted(), runtimeSnapshotHost:record.runtimeSnapshotHost,
      };
      const admitted = await record.api.resolveSubagentLaunchContract(request);
      // A mandatory native extension rejects external runners. Resolve their metadata only;
      // this result cannot attest a native launch or replace its scoped admission.
      const preflight = !admitted?.ok && admitted?.code === "unsupported_mode"
        ? await record.api.resolveSubagentLaunchContract({...request, parentSessionId:undefined})
        : admitted;
      if (!preflight?.ok || preflight.contract?.version !== 3 || !/^[0-9a-f]{64}$/.test(preflight.contract.launchContractDigest || "") ||
          preflight.contract.protocol?.packageVersion !== pkg.metadata.version ||
          preflight.contract.diagnostics?.some(item => ["error", "host-required"].includes(item.severity)))
        return failed(bound, "Native launch preflight did not prove the selected request");
      const contract = preflight.contract;
      if (!text(contract.roots?.cwd) || path.resolve(contract.roots.cwd) !== record.workspace || fs.realpathSync(contract.roots.cwd) !== record.realWorkspace)
        return failed(bound, "Native launch resolved a different cwd");
      if (!["user", "project", "builtin"].includes(contract.agent?.source) || !text(contract.agent.filePath))
        return failed(bound, "Native agent definition provenance is unavailable");
      const definition = protectedInput(record, contract.agent.filePath);
      inputs.push(definition);
      const parsed = record.api.parseFrontmatter(definition.bytes.toString("utf8")).frontmatter;
      if (!plain(parsed)) return failed(bound, "Native agent definition is malformed");
      const settings = [];
      for (const settingsPath of record.settingsPaths) {
        const entry = protectedInput(record, settingsPath, true);
        inputs.push(entry);
        if (entry.sha256 !== null) settings.push(...overrides(JSON.parse(entry.bytes.toString("utf8")), contract.agent.name, ctx.model?.provider));
      }
      const stepOutside = placement([parsed, ...settings]);
      outside ||= stepOutside;
      if (!stepOutside) {
        if (preflight !== admitted) return failed(bound, "Native scoped admission did not prove the selected request");
        if (contract.tools?.disableAmbientExtensions !== true || !contract.tools.requiredExtensionIds?.includes("omnigent.permission"))
          return failed(bound, "Native required enforcement or extension selection is unavailable");
        for (const name of ["extensionArgs", "configuredExtensions", "runtimeExtensions", "toolExtensionPaths"]) {
          if (!Array.isArray(contract.tools[name]) || !contract.tools[name].every(text)) return failed(bound, "Native extension provenance is incomplete");
          for (const specifier of contract.tools[name]) {
            // The actual SDK loader owns Unicode/tilde/URI/drive resolution.
            const resolved = record.session.resourceLoader.resolveExtensionLoadPath(specifier);
            if (resolved.startsWith("builtin:")) continue;
            inputs.push(protectedInput(record, resolved));
          }
        }
      }
      contracts.push(contract.launchContractDigest);
    }
    if (outside) return {binding:bound, status:"outside_native_support", action:"ordinary", outside_kind:outside,
      reason:"Positively identified outside-native runner; use the existing unknown-tool policy", inputs, semantics_digest:digest(contracts)};
    return {binding:bound, status:"attested_native", action:"delegate", inputs, semantics_digest:digest(contracts)};
  } catch (_error) { return failed(bound, "Native support could not be verified"); }
}
function bindNativeSession(session, descriptor) {
  if (!session || !session.sessionManager || !session.resourceLoader || typeof session.getAllTools !== "function" ||
      !descriptor || typeof descriptor.getState !== "function" || !plain(descriptor.scope) ||
      !["scope_id", "installation_id", "registry_generation"].every(key => text(descriptor.scope[key])) ||
      !text(descriptor.scope.canonical_cwd) || !path.isAbsolute(descriptor.scope.canonical_cwd) ||
      !["parent", "child"].includes(descriptor.scope.origin) || typeof descriptor.isInstalled !== "function" ||
      !Array.isArray(descriptor.sources) || !descriptor.sources.length || !Array.isArray(descriptor.requiredIdentities) || !descriptor.requiredIdentities.length)
    throw new Error("Native bootstrap binding is incomplete");
  const loaded = session.resourceLoader.getExtensions().extensions;
  if (loaded.at(-1) !== descriptor.finalExtension || loaded.at(-1).handlers.get("tool_call").at(-1) !== descriptor.finalHook)
    throw new Error("Native final hook is not installed");
  const state = descriptor.getState();
  if (descriptor.scope.origin === "parent" && descriptor.parentSession !== session)
    throw new Error("Native parent requires the launcher's held SDK session");
  const lifecycle = descriptor.scope.origin === "child" && descriptor.api?.getNativeChildLifecycle?.(session.sessionManager);
  if (descriptor.scope.origin === "child" && (!lifecycle || lifecycle.state !== "open" || !lifecycle.closingSignal || typeof lifecycle.bindHost !== "function" || typeof descriptor.beforeClose !== "function"))
    throw new Error("Native child lifecycle and revocation acknowledgement are unavailable");
  const context=inputContext(state.workspace,descriptor.bridgeDir);
  const workspace=context.workspace;
  if (descriptor.scope.canonical_cwd!==workspace) throw new Error("Native cwd differs from the runner's ready scope");
  const settingsPaths = new Set(descriptor.settingsPaths || []);
  if (descriptor.api) {
    const agentDir = descriptor.api.getAgentDir?.();
    if (!text(agentDir) || !path.isAbsolute(agentDir)) throw new Error("Native SDK agent root is unavailable");
    settingsPaths.add(path.join(agentDir,"settings.json"));
  }
  for (const start of new Set([workspace,fs.realpathSync(workspace)]))
    for (let directory=start;;directory=path.dirname(directory)) {
      settingsPaths.add(path.join(directory,".pi/settings.json"));
      if (directory===path.dirname(directory)) break;
    }
  const record = {...context,session, scope:Object.freeze({...descriptor.scope}), getState:descriptor.getState,
    initialTier:state.tier,
    loaded:[...loaded], finalExtension:descriptor.finalExtension, finalHook:descriptor.finalHook,
    registrationDigest:digest(toolSnapshot(session)), api:descriptor.api,
    isInstalled:descriptor.isInstalled, evaluateScope:descriptor.evaluateScope,
    runtimeSnapshotHost:descriptor.runtimeSnapshotHost, settingsPaths:[...settingsPaths], inputs:[], closed:false};
  for (const expected of descriptor.sources) {
    const current = protectedInput(record, expected.path);
    if (digest(current) !== digest(expected) || !loaded.some(entry => entry.resolvedPath === expected.path))
      throw new Error("Native loaded source identity differs from its captured source");
    record.inputs.push(current);
  }
  for (const required of descriptor.requiredIdentities)
    if (!record.inputs.some(entry => entry.resolved_path === required.resolved_path && entry.sha256 === required.sha256))
      throw new Error("Native required loaded identity is missing");
  for (const input of [...descriptor.configInputs || [], ...descriptor.api?.inputs || []]) {
    const current = protectedInput(record, input.path, input.sha256 === null);
    if (digest(current) !== digest(input)) throw new Error("Native configuration changed during binding");
    record.inputs.push(current);
  }
  for (const file of settingsPaths) record.inputs.push(protectedInput(record,file,true));
  const previous = store.bindings.get(session.sessionManager);
  if (previous && !previous.closed) throw new Error("Native loader already has an installation");
  if (lifecycle) {
    if (typeof session.getToolDefinition !== "function") throw new Error("Native execution boundary is unavailable");
    for (const tool of session.getAllTools()) {
      const definition = session.getToolDefinition(tool.name);
      if (!definition || typeof definition.execute !== "function") throw new Error("Native tool definition is unavailable");
      const execute = definition.execute;
      definition.execute = function (...args) {
        if (record.closed || !record.isInstalled()) throw new Error("Native child execution is sealed");
        return execute.apply(this, args);
      };
    }
    lifecycle.closingSignal.addEventListener("abort", () => {record.closed = true;}, {once:true});
    lifecycle.bindHost({id:`omnigent:${record.scope.scope_id}`, async beforeClose(event) {
      record.closed = true;
      const ack = await descriptor.beforeClose(event);
      if (!ack || ack.revoked !== true || ack.scope_id !== record.scope.scope_id || ack.installation_id !== record.scope.installation_id)
        throw new Error("Native child revocation acknowledgement was rejected");
    }});
  }
  store.bindings.set(session.sessionManager, record);
  return () => { record.closed = true; };
}
function requireNativeSession(ctx) {
  const session = store.sessions.get(ctx?.sessionManager);
  const loader = session && store.loaders.get(session.resourceLoader);
  if (loader?.profile.getState().tier === "full") return;
  if (!loader || typeof loader.bind !== "function") throw new Error("Independent native bootstrap is missing");
  loader.bind(session);
  if (!store.bindings.has(ctx.sessionManager)) throw new Error("Native bootstrap did not bind this SDK session");
}
async function loadPackageApi(directory, sdk) {
  const manifest = snapshot(path.join(directory, "package.json"));
  const pkg = JSON.parse(manifest.bytes.toString("utf8"));
  if (pkg.name !== "pi-subagents" || !/^0\.80\.\d+$/.test(pkg.version || "") || typeof sdk?.parseFrontmatter !== "function" || typeof sdk.getAgentDir !== "function")
    throw new Error("Unreviewed native package or SDK");
  const inputs = [manifest];
  const modules = [];
  for (const subpath of ["./preflight", "./native-child-lifecycle"]) {
    const target = pkg.exports?.[subpath];
    const file = typeof target === "string" ? target : target?.import || target?.default;
    if (!text(file) || !file.startsWith("./")) throw new Error("Native public export unavailable");
    const entry = snapshot(path.resolve(directory, file));
    if (!inside(entry.resolved_path, fs.realpathSync(directory))) throw new Error("Native public export escapes its package");
    modules.push(await import(pathToFileURL(entry.resolved_path).href));
    inputs.push(entry);
  }
  if (inputs.some(input => digest(snapshot(input.path)) !== digest(input))) throw new Error("Native public export changed while loading");
  const result = Object.freeze({...modules[0], ...modules[1], parseFrontmatter:sdk.parseFrontmatter, getAgentDir:sdk.getAgentDir,
    packageRoot:fs.realpathSync(directory), version:pkg.version, inputs:Object.freeze(inputs)});
  store.apiRecords.set(result, true);
  return result;
}
function getNativeState(ctx) {
  const record = store.bindings.get(ctx?.sessionManager);
  const session = store.sessions.get(ctx?.sessionManager);
  return record?.getState() || (session && store.loaders.get(session.resourceLoader)?.profile.getState()) || null;
}
function closeNativeSession(manager) { const record = store.bindings.get(manager); if (record) record.closed = true; }
function hasScopedEvaluator(ctx) { return typeof store.bindings.get(ctx?.sessionManager)?.evaluateScope === "function"; }
function policyVerdict(value) {
  if (!plain(value) || typeof value.block !== "boolean") return {block:true, reason:"Native scoped policy returned no usable verdict"};
  return value.block ? {block:true, reason:typeof value.reason === "string" ? value.reason : "Blocked by native scoped policy"} : undefined;
}
async function evaluateNativeCall(event, ctx, support) {
  const record = store.bindings.get(ctx?.sessionManager);
  if (!record || typeof record.evaluateScope !== "function") return {block:true, reason:"Scoped native policy transport is not installed"};
  try { return policyVerdict(await record.evaluateScope(event, ctx, support)); }
  catch (_error) { return {block:true, reason:"Scoped native policy could not be evaluated"}; }
}
module.exports = {bindNativeSession, classifyNativeCall, requireNativeSession, semanticAction, snapshot, loadPackageApi, getNativeState, closeNativeSession, hasScopedEvaluator, evaluateNativeCall, policyVerdict, createInputGuard};
