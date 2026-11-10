const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { pathToFileURL, fileURLToPath } = require("node:url");
const nodeModule = require("node:module");

// Generated parent/child extensions share actual SDK-object keys, not names or payload flags.
const key = Symbol.for("omnigent.pi.native-support.v2");
const store = globalThis[key] ||= { bindings: new WeakMap(), loaders: new WeakMap(), sessions: new WeakMap() };
store.apiRecords ||= new WeakMap();
store.definitionOwners ||= new WeakMap();
store.peerHooks ||= new Map();
const plain = value => value && typeof value === "object" && !Array.isArray(value);
const text = value => typeof value === "string" && value.trim().length > 0;
const integer = (value, minimum = 1) => Number.isSafeInteger(value) && value >= minimum;
const allowed = (value, keys) => plain(value) && Object.keys(value).every(key => keys.includes(key));
const hash = value => crypto.createHash("sha256").update(value).digest("hex");
function codePointOrder(left, right) {
  const a=Array.from(left, item=>item.codePointAt(0)), b=Array.from(right, item=>item.codePointAt(0));
  for(let index=0;index<Math.min(a.length,b.length);index++)
    if(a[index]!==b[index]) return a[index]-b[index];
  return a.length-b.length;
}
function numberDecimal(value) {
  if (!Number.isFinite(value)) throw new Error("Non-finite native number");
  if (Number.isInteger(value)) return BigInt(value).toString();
  const bytes=Buffer.alloc(8);
  bytes.writeDoubleBE(value);
  const bits=bytes.readBigUInt64BE();
  const exponent=Number((bits>>52n)&2047n);
  let numerator=(bits&((1n<<52n)-1n))+(exponent===0?0n:1n<<52n);
  let places=exponent===0?1074:1075-exponent;
  while(numerator%2n===0n) {numerator/=2n;places--;}
  const digits=(numerator*5n**BigInt(places)).toString().padStart(places+1,"0");
  return (value<0?"-":"")+digits.slice(0,-places)+"."+digits.slice(-places);
}
function canonicalJson(value) {
  // Paired with Python _canonical_json: scalar UTF-8, code-point keys, exact binary64 decimals.
  if(value===null || value===undefined) return "null";
  if(typeof value==="string") {
    if(Array.from(value).some(item=>item.codePointAt(0)>=0xd800&&item.codePointAt(0)<=0xdfff)) throw new Error("Invalid Unicode scalar");
    return JSON.stringify(value);
  }
  if(typeof value==="boolean") return value?"true":"false";
  if(typeof value==="number") return numberDecimal(value);
  if(Array.isArray(value)) return "["+value.map(canonicalJson).join(",")+"]";
  if(plain(value)) return "{"+Object.keys(value).filter(name=>value[name]!==undefined).sort(codePointOrder)
    .map(name=>canonicalJson(name)+":"+canonicalJson(value[name])).join(",")+"}";
  throw new Error("Native proof requires JSON values");
}
const digest = value => hash(canonicalJson(value));
function rules() {
  const value = require("../../permission_tiers/native_support_rules.json");
  if (value.version !== 1 || !Object.entries(value).every(([name, entries]) => name === "version" || Array.isArray(entries) && entries.every(text)))
    throw new Error("Unsupported native support rules");
  const actions=[...value.read_actions,...value.control_actions,...value.launch_actions,...value.outside_actions];
  if(new Set(actions).size!==actions.length) throw new Error("Ambiguous native action classification");
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
  if (!fs.constants.O_NOFOLLOW && process.platform !== "win32") throw new Error("No-follow validation unavailable");
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
  const roots=[path.join(require("node:os").homedir(),".pi"),path.join(require("node:os").homedir(),".agents"),bridgeDir,__dirname,fs.realpathSync(__dirname)];
  if (text(bridgeDir)) {
    try {
      const expected=JSON.parse(fs.readFileSync(path.join(bridgeDir,"permission_expected.json"),"utf8"));
      for (const key of ["extension_path","bridge_extension_path"])
        if (text(expected?.[key])) roots.push(path.dirname(expected[key]));
    } catch {}
  }
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
function validNativeOptions(options) {
  return !(options.context !== undefined && !["fresh", "fork"].includes(options.context)) &&
    !(options.agentScope !== undefined && !["user", "project", "both"].includes(options.agentScope)) &&
    !["timeoutMs", "toolTimeoutMs"].some(key => key in options && !integer(options[key])) &&
    !("includeProgress" in options && typeof options.includeProgress !== "boolean") &&
    !("maxOutput" in options && (!allowed(options.maxOutput, ["bytes", "lines"]) || !Object.values(options.maxOutput).every(value => integer(value))));
}
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
  if (!allowed(input, r.run_keys) || ("async" in input && typeof input.async !== "boolean")) return null;
  const options = input.options === undefined ? {} : input.options;
  if (!allowed(options, r.option_keys) || !validNativeOptions(options)) return null;
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
  if (settings.subagents === undefined) return null;
  if (!plain(settings.subagents)) throw new Error("Invalid native settings");
  if (["agentRoots", "agentDirs", "agentsDir", "agentScanDirs", "agentExcludeDirs"].some(key => key in settings.subagents)) throw new Error("Unreviewed native agent roots");
  const groups = [settings.subagents.agentOverrides];
  if (settings.subagents.agentOverridesByProvider !== undefined) {
    if (!plain(settings.subagents.agentOverridesByProvider)) throw new Error("Invalid provider overrides");
    if (text(provider)) groups.push(settings.subagents.agentOverridesByProvider[provider]);
  }
  const selected=groups.filter(group => group !== undefined).map(group => {
    if (!plain(group) || name in group && !plain(group[name])) throw new Error("Invalid native agent override");
    const value=group[name];
    if(value===undefined) return undefined;
    if("runner" in value || "launcher" in value || "fallbackModels" in value) throw new Error("Unreviewed runner/launcher override");
    // The package drops entries containing only ignored fields before precedence is applied.
    const keys=["description","advertise","output","outputMode","model","fast","thinking","systemPromptMode",
      "inheritProjectContext","inheritGlobalContext","inheritSkills","defaultContext","acceptanceRole","disabled",
      "toolBudget","systemPrompt","machine","defaultReads","defaultProvider","skills","tools","excludeTools",
      "allowNestedSubagents","allowedAgents","extensions","subagentOnlyExtensions","mutationTools"];
    const normalized=Object.fromEntries(Object.entries(value).filter(([key])=>keys.includes(key)));
    return Object.keys(normalized).length?normalized:undefined;
  });
  const merged=Object.assign(Object.create(null),...selected.filter(value=>value!==undefined));
  if("runner" in merged || "launcher" in merged) throw new Error("Unreviewed runner/launcher override");
  return selected.some(value=>value!==undefined)?merged:null;
}
function effectiveDefinition(record, parsed, agent, request, inputs) {
  if(typeof request.projectTrusted!=="boolean") throw new Error("Project trust is unresolved");
  const scope=request.projectTrusted?(request.agentScope||"both"):"user";
  if(!["user","project","both"].includes(scope) || (!request.projectTrusted&&request.agentScope==="project"))
    throw new Error("Agent scope is unresolved");
  if((scope==="user"&&agent.source==="project")||(scope==="project"&&agent.source==="user"))
    throw new Error("Agent source differs from effective scope");
  const captured=new Map();
  const read=file=>{
    if(captured.has(file)) return captured.get(file);
    const entry=protectedInput(record,file,true);inputs.push(entry);
    const value=entry.sha256===null?{}:JSON.parse(entry.bytes.toString("utf8"));
    if(!plain(value)||value.subagents!==undefined&&!plain(value.subagents)) throw new Error("Invalid native settings");
    captured.set(file,value);return value;
  };
  let user=null,project=null;
  if(scope!=="project") user=overrides(read(path.join(record.api.getAgentDir(),"settings.json")),agent.name,request.parentModel?.provider);
  if(scope!=="user") {
    const directoryExists=file=>{
      try {return fs.statSync(file).isDirectory();}
      catch(error) {if(["ENOENT","ENOTDIR"].includes(error.code)) return false;throw error;}
    };
    const homes=[require("node:os").homedir(),process.env.HOME,process.env.USERPROFILE,
      process.env.HOMEDRIVE&&process.env.HOMEPATH?process.env.HOMEDRIVE+process.env.HOMEPATH:null]
      .filter(text).filter(directoryExists).map(file=>fs.realpathSync(file));
    let nearest=null;
    for(let directory=record.workspace;;directory=path.dirname(directory)) {
      if(homes.includes(fs.realpathSync(directory))) break;
      if(directoryExists(path.join(directory,".pi"))||directoryExists(path.join(directory,".agents"))) {
        const file=path.join(directory,".pi/settings.json");
        nearest ||= file;
        const mode=read(file).subagents?.projectRootResolution;
        if(mode!==undefined&&mode!=="nearest") throw new Error("Uncertified project root policy");
        if(mode==="nearest") break;
      }
      if(directory===path.dirname(directory)) break;
    }
    if(nearest) project=overrides(read(nearest),agent.name,request.parentModel?.provider);
  }
  // Builtins choose the project override wholesale; custom agents merge user then project.
  const applied=agent.source==="builtin"?[project||user]:[user,project];
  return Object.assign(Object.create(null),parsed,...applied.filter(Boolean));
}
function placement(value) {
  let outside = null;
  if("launcher" in value) throw new Error("Uncertified native launcher");
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
  return outside;
}

function normalizeSharing(input) {
  const nested=plain(input.options)?input.options.share:undefined;
  if(input.share===undefined && nested===undefined) return input;
  if((input.share!==undefined&&typeof input.share!=="boolean") || (nested!==undefined&&typeof nested!=="boolean") ||
      (input.share!==undefined&&nested!==undefined&&input.share!==nested)) throw new Error("Invalid or conflicting sharing options");
  const normalized={...input,share:input.share??nested};
  if(plain(input.options)) {normalized.options={...input.options};delete normalized.options.share;}
  return normalized;
}
function outsideAction(name,input,record) {
  if(name!=="subagent") return null;
  if(input.share===true) {
    const native={...input};delete native.share;
    return semanticAction(name,native,record)==="delegate"?"gist":null;
  }
  const fields=["action","agent","task","name","id","runId","cwd","dir","handoffPath","repo","planId","laneId","merge","supersession",
    "index","childId","toolCallId","view","lines","topic","message","mode","steeringRecovery","additional","scope","target","focus","thinking",
    "at","every","sessionOnly","quiet","on","timezone","overlap","catchUp","missionId","mission","missionUpdate","missionStatus","missionScope",
    "runMode","runStatus","summary","config","workflow","args","async","options"];
  if(!rules().outside_actions.includes(input.action)||!allowed(input,fields)) return null;
  // Resume needs retained-launch evidence; a tool action name does not identify its target.
  const options=input.options===undefined?{}:input.options;
  const topOnly=["action","agent","task","id","cwd","message","async","workflow","args","options"];
  if(!allowed(options,[...rules().option_keys,...fields.filter(key=>!topOnly.includes(key))]) || !validNativeOptions(options)) return null;
  if(Object.entries(options).some(([key,value])=>input[key]!==undefined&&canonicalJson(input[key])!==canonicalJson(value))) return null;
  const values={...input,...options};
  if("cwd" in values&&(!text(values.cwd)||path.resolve(record.workspace,values.cwd)!==record.workspace)) return null;
  if(Object.entries(values).some(([key,value])=>["agent","task","name","id","runId","cwd","at","every","message","mode","topic"].includes(key)&&!text(value))) return null;
  return input.action.startsWith("schedule.")?"schedule":"management";
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
    if (record.definitions && ([...record.definitions].some(([definition, wrapper]) => definition.execute !== wrapper) ||
        [...record.definitionNames].some(([name, definition]) => record.session.getToolDefinition(name) !== definition)))
      return failed(bound, "Native SDK execution boundary changed");
    const inputs = [];
    for (const expected of record.inputs) {
      const actual = protectedInput(record, expected.path, expected.sha256 === null);
      if (digest(actual) !== digest(expected)) return failed(bound, "Native bootstrap inputs changed");
      inputs.push(actual);
    }
    const tool = record.session.getAllTools().find(tool => tool.name === event.toolName);
    if (!tool) return failed(bound, "Native tool is not registered in this loader");
    const candidate = ["subagent", "subagent_supervisor", "bg_wait", "contact_supervisor", "subagents_enable"].includes(event.toolName) || /^(?:npm:)?pi-subagents(?:@|$)/.test(tool.sourceInfo?.source || "");
    const pkg = candidate ? packageForTool(record, tool, inputs) : null;
    if (!pkg) {
      if (/^(?:npm:)?pi-subagents(?:@|$)/.test(tool.sourceInfo?.source || "")) return failed(bound, "Native helper source is unavailable");
      return {binding: bound, status:"attested_native", action:"ordinary", inputs};
    }
    const normalized=event.toolName==="subagent"?normalizeSharing(event.input):event.input;
    if(event.toolName==="subagent" && "action" in normalized) {
      const r=rules();
      // Management names that may launch (including retained or deferred work) need launch proof.
      if(r.launch_actions.includes(normalized.action) ||
          ![...r.read_actions,...r.control_actions,...r.outside_actions].includes(normalized.action))
        return failed(bound,"Native launching action requires its effective launch proof");
    }
    const unsupported=outsideAction(event.toolName,normalized,record);
    if(unsupported&&unsupported!=="gist") return {binding:bound,status:"outside_native_support",action:"ordinary",outside_kind:unsupported,
      reason:"Established package action outside native support",inputs,semantics_digest:digest(event.input)};
    const executionInput={...normalized};
    if(event.toolName==="subagent") delete executionInput.share;
    const action = semanticAction(event.toolName, executionInput, record);
    if (!action) return failed(bound, "Native helper input, action or cwd is outside the admitted subset");
    if (action !== "delegate") return {binding:bound, status:"attested_native", action, inputs, semantics_digest:digest({action, bound, inputs})};
    if (typeof ctx.isProjectTrusted !== "function" || typeof ctx.modelRegistry?.getAvailable !== "function")
      return failed(bound, "Native preflight context is unavailable");
    const source = {...executionInput, ...executionInput.options};
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
      // Package messages can include private paths and configuration; expose only known codes.
      if (preflight?.ok === false && ["missing_agent", "ambiguous_agent", "missing_skill", "denied_required_tool",
          "invalid_artifact_dir", "invalid_cwd", "unsupported_mode", "restricted_agent", "thinking_ceiling",
          "model_scope", "invalid_extension_bindings", "invalid_intercom_bridge"].includes(preflight.code)) {
        const recovery = ["missing_agent", "ambiguous_agent"].includes(preflight.code)
          ? ' Use subagent with action:"list" to discover available agents, then retry with a listed name.' : "";
        return failed(bound, `Native launch preflight failed (${preflight.code}).${recovery}`);
      }
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
      const effective=effectiveDefinition(record,parsed,contract.agent,request,inputs);
      const stepOutside = placement(effective)||(unsupported==="gist"?"gist":null);
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
  const client = descriptor.scopedClient;
  if (client && ["evaluate", "execute", "finish", "seal", "close", "project"].some(name => typeof client[name] !== "function"))
    throw new Error("Native scoped client is incomplete");
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
      if(fs.realpathSync(directory)===fs.realpathSync(require("node:os").homedir())) break;
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
  if (client) {
    if (typeof session.getToolDefinition !== "function") throw new Error("Native execution boundary is unavailable");
    record.contexts = new Map();
    record.definitions = new Map();
    record.definitionNames = new Map();
    record.scopedClient = client;
    record.evaluateScope = async (event, ctx, support) => {
      const verdict = await client.evaluate(event, ctx, support);
      if (verdict?.block === false && !record.closed) record.contexts.set(event.toolCallId, ctx);
      return verdict;
    };
    for (const tool of session.getAllTools()) {
      const definition = session.getToolDefinition(tool.name);
      if (!definition || typeof definition.execute !== "function") throw new Error("Native tool definition is unavailable");
      const owner = store.definitionOwners.get(definition);
      if (owner && !owner.record.closed) throw new Error("Native tool definition is shared by active installations");
      const original = owner?.original || definition.execute;
      const wrapper = async function (...args) {
        if (record.closed || !record.isInstalled()) throw new Error("Native execution is sealed");
        const event = {toolCallId:args[0], toolName:tool.name, input:args[1]};
        const ctx = record.contexts.get(event.toolCallId);
        if (!ctx) throw new Error("Native execution has no scoped policy context");
        try {
          return await client.execute(event, ctx, () => {
            if (record.closed || !record.isInstalled()) throw new Error("Native execution is sealed");
            const input = [...args];
            if (typeof input[3] === "function") {
              const update = input[3];
              input[3] = value => update(client.project(value));
            }
            return original.apply(this, input);
          });
        } finally {
          record.contexts.delete(event.toolCallId);
          if (!await client.finish(event)) throw new Error("Native execution settlement was not acknowledged");
        }
      };
      definition.execute = wrapper;
      record.definitions.set(definition, wrapper);
      record.definitionNames.set(tool.name, definition);
      store.definitionOwners.set(definition, {record, original});
    }
  }
  if (lifecycle) {
    if (typeof session.getToolDefinition !== "function") throw new Error("Native execution boundary is unavailable");
    for (const tool of client ? [] : session.getAllTools()) {
      const definition = session.getToolDefinition(tool.name);
      if (!definition || typeof definition.execute !== "function") throw new Error("Native tool definition is unavailable");
      const execute = definition.execute;
      definition.execute = function (...args) {
        if (record.closed || !record.isInstalled()) throw new Error("Native child execution is sealed");
        return execute.apply(this, args);
      };
    }
    lifecycle.closingSignal.addEventListener("abort", () => {record.closed = true;client?.seal();}, {once:true});
    lifecycle.bindHost({id:`omnigent:${record.scope.scope_id}`, async beforeClose(event) {
      record.closed = true;
      client?.seal();
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
  // The guard can run before user extensions finish registering startup tools.
  // The bridge prepares input after registrations; the final handler also binds.
}
function prepareNativeSession(ctx) {
  requireNativeSession(ctx);
  const session = store.sessions.get(ctx.sessionManager);
  const loader = store.loaders.get(session.resourceLoader);
  if (loader.profile.getState().tier !== "full") return loader.bind(session);
}
async function loadPackageApi(directory, sdk, sdkEntry) {
  const manifest = snapshot(path.join(directory, "package.json"));
  const pkg = JSON.parse(manifest.bytes.toString("utf8"));
  if (pkg.name !== "pi-subagents" || typeof sdk?.parseFrontmatter !== "function" || typeof sdk.getAgentDir !== "function" || sdk.CONFIG_DIR_NAME!==".pi")
    throw new Error("Unreviewed native package or SDK");
  const inputs = [manifest];
  const modules = [];
  const unavailable = detail => Object.freeze({inputs:Object.freeze(inputs),
    unavailable:`pi-subagents is missing ${detail}. Subagent tools are off until a pi-subagents with that API is installed.`});
  if (!/^0\.80\.\d+$/.test(pkg.version || "")) return unavailable("a reviewed package version");
  const exports = ["./preflight", "./native-child-lifecycle", "./required-child-extensions"];
  for (const subpath of exports) {
    const target = pkg.exports?.[subpath];
    const file = typeof target === "string" ? target : target?.import || target?.default;
    if (!text(file) || !file.startsWith("./")) return unavailable(subpath);
  }
  if (sdkEntry) {
    if (typeof nodeModule.registerHooks !== "function") return unavailable("Node module.registerHooks");
    const root = fs.realpathSync(directory);
    const sdkUrl = pathToFileURL(sdkEntry).href;
    const registered = store.peerHooks.get(root);
    if (registered && registered.sdkUrl !== sdkUrl) throw new Error("Native package SDK identity changed");
    if (!registered) {
      const peers = new Set(Object.keys(pkg.peerDependencies || {}));
      const hook = nodeModule.registerHooks({resolve(specifier, context, nextResolve) {
        const peer = specifier.startsWith("@") ? specifier.split("/").slice(0,2).join("/") : specifier.split("/")[0];
        if (peers.has(peer) && context.parentURL?.startsWith("file:") && inside(fileURLToPath(context.parentURL), root))
          return nextResolve(specifier, {...context, parentURL:sdkUrl});
        return nextResolve(specifier, context);
      }});
      store.peerHooks.set(root, {sdkUrl, hook});
    }
  }
  for (const subpath of exports) {
    const target = pkg.exports[subpath];
    const file = typeof target === "string" ? target : target.import || target.default;
    const entry = snapshot(path.resolve(directory, file), true);
    inputs.push(entry);
    if (entry.sha256 === null) return unavailable(subpath);
    if (!inside(entry.resolved_path, fs.realpathSync(directory))) throw new Error("Native public export escapes its package");
    try { modules.push(await import(pathToFileURL(entry.resolved_path).href)); }
    catch (_error) {
      if (inputs.some(input => digest(snapshot(input.path)) !== digest(input))) throw new Error("Native public export changed while loading");
      return unavailable(subpath);
    }
  }
  if (inputs.some(input => digest(snapshot(input.path)) !== digest(input))) throw new Error("Native public export changed while loading");
  for (const [index, name] of [[0,"resolveSubagentLaunchContract"],[1,"getNativeChildLifecycle"],[2,"registerRequiredChildExtensions"]])
    if (typeof modules[index][name] !== "function") return unavailable(`${exports[index]} (${name})`);
  if (modules[0].SUBAGENT_LAUNCH_CONTRACT_VERSION !== 3) return unavailable("./preflight (launch contract version 3)");
  const result = Object.freeze({...modules[0], ...modules[1], parseFrontmatter:sdk.parseFrontmatter, getAgentDir:sdk.getAgentDir,
    registerRequiredChildExtensions:modules[2].registerRequiredChildExtensions,
    packageRoot:fs.realpathSync(directory), version:pkg.version, inputs:Object.freeze(inputs)});
  store.apiRecords.set(result, true);
  return result;
}
function getNativeState(ctx) {
  const record = store.bindings.get(ctx?.sessionManager);
  const session = store.sessions.get(ctx?.sessionManager);
  return record?.getState() || (session && store.loaders.get(session.resourceLoader)?.profile.getState()) || null;
}
function closeNativeSession(manager) {
  const record = store.bindings.get(manager);
  if (!record) return;
  record.closed = true;
  record.scopedClient?.seal();
  return record.scopedClient?.close();
}
function hasScopedEvaluator(ctx) { return typeof store.bindings.get(ctx?.sessionManager)?.evaluateScope === "function"; }
function hasScopedClient(ctx) {
  const record = store.bindings.get(ctx?.sessionManager);
  return !!record && !record.closed && record.isInstalled() && typeof record.scopedClient?.bridge === "function";
}
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
function callScopedBridge(event, ctx, port) {
  const record=store.bindings.get(ctx?.sessionManager);
  if(!record || record.closed || !record.isInstalled() || typeof record.scopedClient?.bridge!=="function")
    return Promise.resolve({error:"Scoped Pi bridge unavailable"});
  return record.scopedClient.bridge(event,port);
}
module.exports = {bindNativeSession, classifyNativeCall, requireNativeSession, prepareNativeSession, semanticAction, snapshot, loadPackageApi, getNativeState, closeNativeSession, hasScopedEvaluator, hasScopedClient, evaluateNativeCall, policyVerdict, createInputGuard, callScopedBridge};
