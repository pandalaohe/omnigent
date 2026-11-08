const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {pathToFileURL} = require("node:url");
const support = require("./native_support.cjs");
const {createChildNativeGate} = require("./omnigent_pi_child_extension.js");

async function fixture(t, {origin = "parent", tier = "auto", duplicate = false, definition = {name:"reader"}, noEvaluator = false, refuseNativeAdmission = false, settings, projectSettings, projectTrusted=false, definitionSource="user", missingRequired=false, scopedClient, asyncDescriptor=false, missingExport, startupRegistration=false, packageVersion="0.80.0"} = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "native-support-"));
  t.after(() => fs.rmSync(root, {recursive:true, force:true}));
  const workspace = path.join(root, "workspace");
  const packageRoot = path.join(root, "package");
  fs.mkdirSync(workspace);
  fs.mkdirSync(packageRoot);
  const agentPath = path.join(root, "reader.md");
  const guard = path.join(root, "guard.cjs");
  const entry = path.join(packageRoot, "index.js");
  fs.writeFileSync(agentPath, JSON.stringify(definition));
  fs.writeFileSync(guard, "// independent host guard\n");
  fs.writeFileSync(entry, "// registered fixture extension\n");
  if (settings) fs.writeFileSync(path.join(root,"settings.json"),JSON.stringify(settings));
  if (projectSettings) {
    fs.mkdirSync(path.join(workspace,".pi"));
    fs.writeFileSync(path.join(workspace,".pi/settings.json"),JSON.stringify(projectSettings));
  }
  fs.writeFileSync(path.join(packageRoot, "package.json"), JSON.stringify({name:"pi-subagents",version:packageVersion,type:"module",exports:{
    "./preflight":"./preflight.js", "./native-child-lifecycle":"./lifecycle.js", "./required-child-extensions":"./required.js"}}));
  fs.writeFileSync(path.join(packageRoot, "preflight.js"), `export const SUBAGENT_LAUNCH_CONTRACT_VERSION=3;
export async function resolveSubagentLaunchContract(input) {
 if (${refuseNativeAdmission} && input.parentSessionId) return {ok:false,code:"unsupported_mode"};
 return {ok:true,contract:{version:3,
 agent:{name:input.agent,source:${JSON.stringify(definitionSource)},filePath:${JSON.stringify(agentPath)}}, launchContractDigest:"${"0".repeat(64)}", roots:{cwd:input.cwd},
 protocol:{packageVersion:"0.80.0"}, tools:{disableAmbientExtensions:true,requiredExtensionIds:${JSON.stringify(missingRequired?[]:["omnigent.permission"])},
 extensionArgs:[],configuredExtensions:[],runtimeExtensions:[],toolExtensionPaths:[]}, diagnostics:[]}}; }`);
  fs.writeFileSync(path.join(packageRoot, "lifecycle.js"), "export function getNativeChildLifecycle(manager) { return globalThis[Symbol.for('fixture.native-lifecycles')].get(manager); }");
  fs.writeFileSync(path.join(packageRoot, "required.js"), "export function registerRequiredChildExtensions() { return {dispose(){}}; }");
  if (missingExport) {
    const manifestPath=path.join(packageRoot,"package.json");
    const manifest=JSON.parse(fs.readFileSync(manifestPath));
    if (missingExport.startsWith("./")) delete manifest.exports[missingExport];
    else fs.writeFileSync(path.join(packageRoot, {
      resolveSubagentLaunchContract:"preflight.js",getNativeChildLifecycle:"lifecycle.js",
      registerRequiredChildExtensions:"required.js",
    }[missingExport]),"export {};\n");
    fs.writeFileSync(manifestPath,JSON.stringify(manifest));
  }
  globalThis[Symbol.for("fixture.native-lifecycles")] ||= new WeakMap();
  const api = await support.loadPackageApi(packageRoot, {parseFrontmatter:text=>({frontmatter:JSON.parse(text)}),getAgentDir:()=>root,CONFIG_DIR_NAME:".pi"});
  const state = {tier,revision:3,status:"ready",workspace};
  let backendStarts = 0, policyCalls = 0;
  const facts = [], notices = [];
  let startupProof;
  const scope = {scope_id:"scope",installation_id:"installation",registry_generation:"generation",origin,canonical_cwd:fs.realpathSync(workspace)};
  const toolRows = [
    {name:"subagent",sourceInfo:{path:entry,scope:"user",source:`npm:pi-subagents@${packageVersion}`,origin:"package",baseDir:root}},
    {name:"read",sourceInfo:{source:"builtin",path:"builtin:read"}},
    {name:"sys_os_read",sourceInfo:{path:guard}},
    {name:"sys_read_inbox",sourceInfo:{path:guard}},
  ];
  const definitions = new Map(toolRows.map(tool => [tool.name, {execute:async () => {backendStarts++; return "fixture result";}}]));
  const sourceExtension = {path:entry,resolvedPath:entry,handlers:new Map()};
  const guardExtension = {path:guard,resolvedPath:guard,handlers:new Map()};
  if (startupRegistration) {
    guardExtension.handlers.set("session_start", [(_event, ctx)=>support.requireNativeSession(ctx)]);
    sourceExtension.handlers.set("session_start", [async (_event, ctx)=>{
      for (const name of ["bash", "manage_todo_list", "bg_wait"]) {
        toolRows.push({name,sourceInfo:name==="bg_wait"?toolRows[0].sourceInfo:{path:guard}});
        definitions.set(name,{execute:async()=>{backendStarts++;return "startup tool result";}});
      }
      await support.prepareNativeSession(ctx);
      startupProof = await support.classifyNativeCall({toolCallId:"startup",toolName:"read",input:{}},ctx,state);
    }]);
  }
  const initial = startupRegistration ? [guardExtension, sourceExtension] : duplicate ? [sourceExtension, sourceExtension, guardExtension] : [sourceExtension, guardExtension];
  class Loader {
    constructor() { this.cwd=workspace; this.eventBus={}; this.result={extensions:[],errors:[],runtime:{}}; this.resolveCalls=[]; }
    resolveExtensionLoadPath(value) { this.resolveCalls.push(value); return value; }
    async loadExtensionPaths() {}
    async loadCurrentExtensionSet() {}
    async reload() { await this.loadExtensionPaths([entry, guard], {}); this.result.extensions=[...initial]; }
    getExtensions() { return this.result; }
    async loadExtensionFactories(runtime) {
      const extensions=[];
      for (const item of this.extensionFactories) {
        const extension={path:`<inline:${item.name}>`,handlers:new Map()};
        await item.factory({events:{emit(){}},sendMessage:message=>notices.push(message),on(name, handler) { const handlers=extension.handlers.get(name)||[]; handlers.push((...args)=>handler(...args)); extension.handlers.set(name,handlers); }});
        extensions.push(extension);
      }
      return {extensions,errors:[],runtime};
    }
  }
  class Session {
    constructor(loader) {this.resourceLoader=loader; this.sessionManager={getSessionId:()=>"session",getSessionDir:()=>root,getSessionFile:()=>null,getLeafId:()=>null};}
    getAllTools() {return toolRows;}
    getToolDefinition(name) {return definitions.get(name);}
    async bindExtensions() {
      if (startupRegistration) for (const extension of this.resourceLoader.getExtensions().extensions)
        for (const handler of extension.handlers.get("session_start") || [])
          await handler({reason:"startup"},{cwd:workspace,sessionManager:this.sessionManager});
    }
  }
  const controller = new AbortController();
  let participant;
  const lifecycle = {state:"open",closingSignal:controller.signal,bindHost(value){participant=value; return {};},async close(){this.state="closing";controller.abort();await participant.beforeClose({generation:"lifecycle"});}};
  const sdk = {DefaultResourceLoader:Loader,AgentSession:Session};
  const {installNativeSupportBootstrap} = await import(pathToFileURL(path.join(__dirname, "omnigent_pi_bootstrap.mjs")).href);
  let actual;
  const dispose = installNativeSupportBootstrap(sdk, {
    sdkVersion:"1.3.0",
    prepareLoader:() => ({getState:()=>state,checkInput(file){assert.ok(path.isAbsolute(file));}}),
    describeSession:() => (asyncDescriptor ? value=>Promise.resolve(value) : value=>value)({scope,api:api.unavailable?undefined:api,nativePackageFailure:api.unavailable,getState:()=>state,workspace,bridgeDir:root,parentSession:actual,scopedClient,
      requiredIdentities:[support.snapshot(guard)],configInputs:[],settingsPaths:[],
      beforeClose:async()=>({revoked:true,scope_id:scope.scope_id,installation_id:scope.installation_id})}),
    evaluate:noEvaluator ? undefined : async (_event, _ctx, fact) => {policyCalls++;facts.push(fact);return {block:false};},
  });
  t.after(dispose);
  const loader = new Loader();
  await loader.reload();
  actual = new Session(loader);
  if (origin === "child") globalThis[Symbol.for("fixture.native-lifecycles")].set(actual.sessionManager, lifecycle);
  await actual.bindExtensions();
  const ctx = {cwd:workspace,sessionManager:actual.sessionManager,isProjectTrusted:()=>projectTrusted,
    modelRegistry:{getAvailable:()=>[]},model:{provider:"fixture",id:"local"},scopedModels:[]};
  const final = loader.getExtensions().extensions.at(-1).handlers.get("tool_call").at(-1);
  async function run(name, input) {
    const event={toolCallId:"call",toolName:name,input};
    const result=await final(event,ctx);
    if (!result?.block) await actual.getToolDefinition(name).execute();
    return result;
  }
  return {root,workspace,agentPath,guard,entry,packageRoot,scope,api,state,loader,actual,ctx,final,run,dispose,lifecycle,
    backendStarts:()=>backendStarts,policyCalls:()=>policyCalls,facts,notices,startupProof};
}

test("startup registrations with stock pi-subagents preserve base tools and deny helpers",async t=>{
  for (const packageVersion of ["0.80.0","0.87.0"]) await t.test(packageVersion,async t=>{
    const f=await fixture(t,{missingExport:"./native-child-lifecycle",startupRegistration:true,packageVersion});
    assert.equal(f.startupProof.status,"attested_native","tools are bound before startup metadata completes");
    assert.equal(f.notices.length,1);
    for (const name of ["read","bash","sys_os_read","sys_read_inbox","manage_todo_list"])
      assert.equal(await f.run(name,{}),undefined,name);
    assert.equal(f.backendStarts(),5);
    for (const name of ["subagent","bg_wait"])
      assert.equal((await f.run(name,{}))?.block,true,name);
    assert.equal(f.backendStarts(),5);
    f.actual.getAllTools().push({name:"late",sourceInfo:{path:f.guard}});
    assert.equal((await f.run("read",{}))?.block,true,"registrations after startup remain fenced");
  });
});

test("missing package APIs leave ordinary tools gated and helper tools denied",async t=>{
  for(const missingExport of ["./native-child-lifecycle","./preflight","./required-child-extensions",
    "resolveSubagentLaunchContract","getNativeChildLifecycle","registerRequiredChildExtensions"]){
    await t.test(missingExport,async t=>{
      const f=await fixture(t,{missingExport});
      const start=f.loader.getExtensions().extensions.at(-1).handlers.get("session_start")[0];
      await start({},f.ctx);
      await start({},f.ctx);
      assert.equal(f.notices.length,1);
      assert.ok(f.notices[0].content.includes(missingExport));
      assert.equal(f.notices[0].display,true);
      assert.equal(await f.run("read",{path:"notes.txt"}),undefined);
      assert.equal(f.backendStarts(),1);
      for(const input of [{agent:"reader",task:"read"},{action:"status"},{action:"gist"}]){
        const result=await f.run("subagent",input);
        assert.equal(result.block,true);
      }
      assert.equal(f.backendStarts(),1);
    });
  }
});

test("verified native source still requires admitted sync/async and action semantics", async t => {
  for (const [input, action] of [[{agent:"reader",task:"read",async:false},"delegate"], [{agent:"reader",task:"read",async:true},"delegate"], [{action:"list"},"read"], [{action:"stop",id:"child"},"control"]]) {
    await t.test(action+JSON.stringify(input), async t => {
      const f=await fixture(t);
      assert.equal(await f.run("subagent",input),undefined);
      assert.equal(f.backendStarts(),1);
      assert.equal(f.policyCalls(),1);
      assert.equal(f.facts[0].status,"attested_native");
      assert.equal(f.facts[0].action,action);
      assert.equal(f.facts[0].binding.arguments_digest.length,64);
    });
  }
});

test("acknowledged async binding wraps root definitions and keeps receipts private", async t => {
  const seen=[];
  const client={
    evaluate:async()=>({block:false}),
    execute:async(event,ctx,start)=>{seen.push([event.toolCallId,ctx.cwd]);return start();},
    finish:async event=>{seen.push(["finish",event.toolCallId]);return true;},
    seal(){seen.push(["seal"]);},close:async()=>{seen.push(["close"]);},
    project:value=>value,
  };
  const f=await fixture(t,{scopedClient:client,asyncDescriptor:true});
  const event={toolCallId:"scoped-read",toolName:"read",input:{path:"notes"}};
  assert.equal(await f.final(event,f.ctx),undefined);
  await f.actual.getToolDefinition("read").execute(event.toolCallId,event.input);
  assert.deepEqual(seen,[["scoped-read",f.workspace],["finish","scoped-read"]]);
  assert.equal(f.backendStarts(),1);
  const originalDefinition=f.actual.getToolDefinition;
  f.actual.getToolDefinition=()=>({execute:()=>{throw new Error("replacement must not run");}});
  const changed=await f.final({...event,toolCallId:"replacement"},f.ctx);
  assert.equal(changed.block,true);
  f.actual.getToolDefinition=originalDefinition;
  await support.closeNativeSession(f.actual.sessionManager);
  await assert.rejects(async()=>f.actual.getToolDefinition("read").execute("late",{}),/sealed/);
  assert.equal(f.backendStarts(),1);
});
test("reload seals and closes the previous scoped client before the SDK reloads", async t => {
  const seen=[];
  const client={
    evaluate:async()=>({block:false}),
    execute:async(_event,_ctx,start)=>start(),
    finish:async()=>true,
    seal(){seen.push("seal");},close:async()=>{seen.push("close");},
    project:value=>value,
  };
  const f=await fixture(t,{scopedClient:client,asyncDescriptor:true});
  assert.equal(await f.final({toolCallId:"bind",toolName:"read",input:{path:"notes"}},f.ctx),undefined);
  f.loader.loadExtensionPaths=async()=>{seen.push("sdk-reload");};
  await f.loader.reload();
  assert.deepEqual(seen,["seal","close","sdk-reload"]);
});
test("source proof cannot bypass workflow/machine/cwd/output or extra fields", async t => {
  const additions=[{workflow:"unknown"},{machine:"unknown"},{worktree:true},{cwd:"child"},{options:{output:"other"}}, {options:{sessionDir:"other"}}, {options:{acceptance:{run:"echo changed"}}}, {options:null}];
  for (const extra of additions) await t.test(JSON.stringify(extra), async t => {
    const f=await fixture(t);
    const result=await f.run("subagent",{agent:"reader",task:"read",async:false,...extra});
    assert.equal(result.block,true);
    assert.equal(f.backendStarts(),0);
    assert.equal(f.policyCalls(),0);
  });
});
test("missing native origin/bootstrap blocks reads, control, bridge and delegation", async t => {
  for (const name of ["read","sys_read_inbox","sys_os_read","subagent"]) {
    let policyCalls=0;
    const gate=createChildNativeGate({readState:()=>({tier:"auto",status:"ready",revision:3}),evaluate:async()=>{policyCalls++;return {block:false};}});
    const result=await gate({toolName:name,toolCallId:"call",input:{path:"file"}},{sessionManager:{}});
    assert.equal(result.block,true);
    assert.equal(policyCalls,0);
  }
});
test("positive remote/external configuration is outside-native rather than failed proof", async t => {
  for (const [definition, kind] of [[{machine:"registered-host"},"remote"],[{runner:{type:"external-cli",command:"external"}},"external_cli"],[{runner:{type:"external-job",provider:"known"}},"unsupported_runner"]]) {
    await t.test(kind, async t => {
      const f=await fixture(t,{definition});
      const result=await support.classifyNativeCall({toolName:"subagent",toolCallId:"call",input:{agent:"reader",task:"read",async:false}},f.ctx,f.state);
      assert.equal(result.status,"outside_native_support");
      assert.equal(result.outside_kind,kind);
    });
  }
});
test("unknown runner, changed input, displaced final hook and closed bootstrap all refuse", async t => {
  for (const fault of ["runner","input","settings","final","disposed"]) await t.test(fault, async t => {
    const f=await fixture(t,{definition:fault==="runner"?{runner:{type:"future"}}:{name:"reader"}});
    if (fault==="input") fs.writeFileSync(f.guard,"changed");
    if (fault==="settings") fs.writeFileSync(path.join(f.root,"settings.json"),"{}");
    if (fault==="final") f.loader.getExtensions().extensions.push({path:"late",handlers:new Map()});
    if (fault==="disposed") f.dispose();
    assert.equal((await f.run("subagent",{agent:"reader",task:"read",async:false})).block,true);
    assert.equal(f.backendStarts(),0);
    assert.equal(f.policyCalls(),0);
  });
});
test("native admission rejection permits only positively proven outside metadata", async t => {
  for (const outside of [true,false]) await t.test(String(outside), async t => {
    const f=await fixture(t,{refuseNativeAdmission:true,definition:outside?{runner:{type:"external-cli",command:"external"}}:{name:"reader"}});
    const result=await support.classifyNativeCall({toolName:"subagent",toolCallId:"call",input:{agent:"reader",task:"read",async:false}},f.ctx,f.state);
    assert.equal(result.status,outside?"outside_native_support":"failed_native");
    assert.equal(f.backendStarts(),0);
    assert.equal(f.policyCalls(),0);
  });
});
test("unreviewed discovery roots refuse and unused provider overrides do not change placement", async t => {
  const roots=await fixture(t,{settings:{subagents:{agentScanDirs:["/opt/unreviewed/agents"]}}});
  const event={toolName:"subagent",toolCallId:"call",input:{agent:"reader",task:"read",async:false}};
  assert.equal((await support.classifyNativeCall(event,roots.ctx,roots.state)).status,"failed_native");
  const provider=await fixture(t,{settings:{subagents:{agentOverridesByProvider:{other:{reader:{machine:"other-host"}}}}}});
  assert.equal((await support.classifyNativeCall(event,provider.ctx,provider.state)).status,"attested_native");
});
test("SDK order/repeated entries remain, pre-trust entry is rejected", async t => {
  const f=await fixture(t,{duplicate:true});
  const entries=f.loader.getExtensions().extensions;
  assert.equal(entries[0],entries[1]);
  assert.equal(entries.length,4);
  assert.throws(()=>f.loader.loadCurrentExtensionSet(),/not admitted/);
  assert.equal(await f.run("read",{path:"notes"}),undefined);
});
test("actual child definition fence seals before the close acknowledgement", async t => {
  const f=await fixture(t,{origin:"child"});
  const allowed=await f.final({toolName:"read",toolCallId:"call",input:{path:"notes"}},f.ctx);
  assert.equal(allowed,undefined);
  const close=f.lifecycle.close();
  assert.throws(()=>f.actual.getToolDefinition("read").execute(),/sealed/);
  await close;
  assert.equal(f.backendStarts(),0);
});
test("full keeps the existing path; native policy callback cannot leak private fields", async () => {
  const full=createChildNativeGate({readState:()=>({tier:"full",status:"ready"}),evaluate:async()=>({block:false,permission_tier_binding:{pi_origin_receipt:"private-fixture-receipt"}})});
  assert.equal(await full({toolName:"read",input:{}},{sessionManager:{}}),undefined);
  assert.deepEqual(support.policyVerdict({block:true,reason:"denied",permission_tier_binding:{pi_origin_receipt:"private-fixture-receipt"}}),{block:true,reason:"denied"});
  assert.equal(JSON.stringify(support.snapshot(__filename)).includes("const test"),false);
});
test("an admitted loader without a scoped policy consumer never falls back", async t => {
  const f=await fixture(t,{noEvaluator:true});
  assert.equal((await f.run("read",{path:"notes"})).block,true);
  assert.equal(f.backendStarts(),0);
});
test("full does not depend on a provable new SDK layout", async () => {
  const {installNativeSupportBootstrap}=await import(pathToFileURL(path.join(__dirname,"omnigent_pi_bootstrap.mjs")).href);
  assert.doesNotThrow(()=>installNativeSupportBootstrap({}, {getRootState:()=>({tier:"full"})})());
  assert.throws(()=>installNativeSupportBootstrap({}, {getRootState:()=>({tier:"auto"})}), /Unsupported/);
});

test("effective placement never downgrades failed local admission to outside", async t => {
  const machineOverride=machine=>({subagents:{agentOverrides:{reader:{machine}}}});
  const cases=[
    {name:"provider false wins",settings:{subagents:{agentOverrides:{reader:{machine:"host"}},agentOverridesByProvider:{fixture:{reader:{machine:false}}}}}},
    {name:"untrusted project excluded",projectSettings:machineOverride("host")},
    {name:"user scope excludes project",projectTrusted:true,projectSettings:machineOverride("host"),agentScope:"user"},
    {name:"project scope excludes user",projectTrusted:true,settings:machineOverride("host"),definitionSource:"project",agentScope:"project"},
    {name:"project false wins user",projectTrusted:true,settings:machineOverride("host"),projectSettings:machineOverride(false)},
    {name:"builtin project override replaces user",projectTrusted:true,definitionSource:"builtin",settings:machineOverride("host"),projectSettings:{subagents:{agentOverrides:{reader:{description:"local"}}}}},
    {name:"ambiguous root policy",projectTrusted:true,projectSettings:{subagents:{projectRootResolution:"git-root",agentOverrides:{reader:{machine:"host"}}}}},
    {name:"prototype key is not placement",definition:JSON.parse('{"__proto__":{"machine":"host"}}')},
  ];
  for (const {name,agentScope,...options} of cases) await t.test(name,async t=>{
    const f=await fixture(t,{...options,missingRequired:true});
    const input={agent:"reader",task:"read",async:false,...agentScope?{options:{agentScope}}:{}};
    const result=await support.classifyNativeCall({toolName:"subagent",toolCallId:"call",input},f.ctx,f.state);
    assert.equal(result.status,"failed_native");
    assert.equal((await f.run("subagent",input)).block,true);
    assert.equal(f.policyCalls(),0);
    assert.equal(f.backendStarts(),0);
  });
});
test("uncertified launcher recipe is never native or outside attestation",async t=>{
  for(const definition of [{launcher:"broker"},{launcher:"broker",machine:"host"},{launcher:"broker",runner:{type:"external-cli"}}]) for(const share of [false,true]) await t.test(JSON.stringify({definition,share}),async t=>{
    const f=await fixture(t,{definition});
    assert.equal((await f.run("subagent",{agent:"reader",task:"read",async:false,...share?{share:true}:{}}))?.block,true);
    assert.equal(f.policyCalls(),0);
    assert.equal(f.backendStarts(),0);
  });
});
test("effective placement keeps valid local and remote projections",async t=>{
  const settings={subagents:{agentOverrides:{reader:{machine:"host"}}}};
  for(const [name,options,input,expected] of [
    ["user remote",{settings},{},"outside_native_support"],
    ["provider clears remote",{settings:{subagents:{...settings.subagents,agentOverridesByProvider:{fixture:{reader:{machine:false}}}}}},{},"attested_native"],
    ["untrusted project ignored",{projectSettings:settings},{},"attested_native"],
    ["trusted project remote",{projectSettings:settings,projectTrusted:true},{},"outside_native_support"],
    ["project-only ignores user",{settings,projectTrusted:true,definitionSource:"project"},{options:{agentScope:"project"}},"attested_native"],
  ]) await t.test(name,async t=>{
    const f=await fixture(t,options);
    const result=await support.classifyNativeCall({toolName:"subagent",toolCallId:"call",input:{agent:"reader",task:"read",async:false,...input}},f.ctx,f.state);
    assert.equal(result.status,expected);
  });
});
test("known outside actions and Gist produce explicit outside facts",async t=>{
  const cases=[
    {action:"mission.list"},{action:"project.status"},
    {action:"mission.list",options:{topic:"fixture",agentScope:"both"}},
    {action:"create",agent:"reader",config:{description:"fixture"}},
    {action:"worktree.cleanup",mode:"plan"},
    {agent:"reader",task:"read",async:false,share:true},
    {agent:"reader",task:"read",async:false,options:{share:true}},
    {agent:"reader",task:"read",async:false,share:true,options:{share:true,timeoutMs:1000}},
  ];
  for(const input of cases) await t.test(JSON.stringify(input),async t=>{
    const f=await fixture(t);
    const fact=await support.classifyNativeCall({toolName:"subagent",toolCallId:"call",input},f.ctx,f.state);
    assert.equal(fact.status,"outside_native_support");
    assert.equal(fact.outside_kind,input.share||input.options?.share?"gist":input.action.startsWith("schedule.")?"schedule":"management");
  });
  for(const input of [{action:"schedule.future"},{action:"schedule.list",unknown:true},{agent:"reader",task:"read",async:false,share:"yes"}]) {
    const f=await fixture(t);
    assert.equal((await support.classifyNativeCall({toolName:"subagent",toolCallId:"call",input},f.ctx,f.state)).status,"failed_native");
  }
});
test("the whole package action domain follows the non-launching or native-proof gate",async t=>{
  const cases=JSON.parse(process.env.OMNIGENT_NATIVE_ACTION_CASES||"[]");
  assert.equal(cases.length,61,"Explicit reviewed package action domain required");
  for(const {action,policy} of cases) await t.test(action,async t=>{
    const f=await fixture(t);
    const input={action};
    const fact=await support.classifyNativeCall({toolName:"subagent",toolCallId:"call",input},f.ctx,f.state);
    assert.equal(fact.status,policy==="launch"?"failed_native":policy==="outside"?"outside_native_support":"attested_native");
    if(policy==="launch") {
      assert.match(fact.reason,/Native launching action requires its effective launch proof/);
      assert.equal((await f.run("subagent",input))?.block,true);
      assert.equal(f.policyCalls(),0);
      assert.equal(f.backendStarts(),0);
    } else if(policy!=="outside") assert.equal(fact.action,policy);
  });
});
test("resume and unknown outside options cannot bypass retained launch proof",async t=>{
  for(const definition of [{name:"reader"},{launcher:"retained-broker"},{machine:"host"}]) await t.test(JSON.stringify(definition),async t=>{
    const f=await fixture(t,{definition});
    // No retained launch is bound to this id, even if the current agent looks remote.
    assert.equal((await f.run("subagent",{action:"resume",id:"retained-run"}))?.block,true);
    assert.equal(f.policyCalls(),0);
    assert.equal(f.backendStarts(),0);
  });
  for(const options of [{unknown:true},null,[],{agentScope:"unknown"}]) await t.test(JSON.stringify(options),async t=>{
    const f=await fixture(t);
    assert.equal((await f.run("subagent",{action:"mission.list",options}))?.block,true);
    assert.equal(f.policyCalls(),0);
    assert.equal(f.backendStarts(),0);
  });
});
test("empty and ignored-only builtin overrides preserve effective placement",async t=>{
  for(const project of [{},{ignored:true},{name:"ignored",prompt:"ignored"}]) await t.test(JSON.stringify(project),async t=>{
    const f=await fixture(t,{definitionSource:"builtin",projectTrusted:true,
      settings:{subagents:{agentOverrides:{reader:{machine:"host"}}}},
      projectSettings:{subagents:{agentOverrides:{reader:project}}}});
    const fact=await support.classifyNativeCall({toolName:"subagent",toolCallId:"call",input:{agent:"reader",task:"read",async:false}},f.ctx,f.state);
    assert.equal(fact.status,"outside_native_support");
    assert.equal(fact.outside_kind,"remote");
  });
});
test("public sharing options preserve launcher and configuration fences",async t=>{
  for(const [definition,input] of [
    [{launcher:"broker"},{options:{share:true}}],
    [{worktree:true},{options:{share:true}}],
    [{name:"reader"},{share:false,options:{share:true}}],
    [{name:"reader"},{options:{share:"yes"}}],
    [{name:"reader"},{options:{share:true,unknown:true}}],
  ]) await t.test(JSON.stringify({definition,input}),async t=>{
    const f=await fixture(t,{definition});
    assert.equal((await f.run("subagent",{agent:"reader",task:"read",async:false,...input}))?.block,true);
    assert.equal(f.policyCalls(),0);
    assert.equal(f.backendStarts(),0);
  });
  const f=await fixture(t);
  const fact=await support.classifyNativeCall({toolName:"subagent",toolCallId:"call",input:{agent:"reader",task:"read",async:false,options:{share:false}}},f.ctx,f.state);
  assert.equal(fact.status,"attested_native");
});
test("native argument digests conform to Python over legal JSON shapes",async t=>{
  const cases=JSON.parse(process.env.OMNIGENT_NATIVE_DIGEST_CASES||"[]");
  assert.ok(cases.length>=6,"Python supplies cross-language canonical vectors");
  for(const {name,input,expected} of cases) await t.test(name,async t=>{
    const f=await fixture(t);
    const fact=await support.classifyNativeCall({toolName:"read",toolCallId:"call",input},f.ctx,f.state);
    assert.equal(fact.status,"attested_native");
    assert.equal(fact.binding.arguments_digest,expected);
  });
});
