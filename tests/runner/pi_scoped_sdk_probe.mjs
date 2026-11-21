import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import http from "node:http";
import {createRequire, findPackageJSON} from "node:module";
import {pathToFileURL} from "node:url";
const [sdkRoot, locator, root, adapter, extension, mode="normal"] = process.argv.slice(2);
const mcpMode = ["mcp-startup", "mcp-late", "mcp-child", "control-shape"].includes(mode);
const unknownMode = ["unknown-source", "control-shape"].includes(mode);
const omissionMode = mcpMode || unknownMode;
const mcpName = "mcp__fixture__echo", unknownName = "opaque_fixture_tool";
const omittedNames = [...(mcpMode?[mcpName]:[]), ...(unknownMode?[unknownName]:[])];
let ackDispatch=false, definitionStarts=0;
const observedControls = [];
const originalRequest=http.request;
http.request=function(url,options,callback){
 if (url.pathname === "/pi/bootstrap" && mode === "control-shape") {
  const request = originalRequest(url, options, callback);
  const end = request.end;
  request.end = function (body) {
   const payload = JSON.parse(body);
   const operation = payload.op === "bind" && Object.hasOwn(payload,"registry") ? "registry" : payload.op;
   const baseFields = {
    bind: ["loader"],
    registry: ["loader", "registry"],
    attest: ["call_id", "name", "arguments", "revision", "support"],
    revoke: [],
   };
   assert.ok(Object.hasOwn(baseFields, operation));
   assert.deepEqual(Object.keys(payload).sort(), ["protocol", "sequence", "op", "handle", ...baseFields[operation]].sort());
   assert.equal(payload.protocol, 1);
   assert.equal(payload.sequence, observedControls.length + 1);
   if (operation === "registry") {
    assert.deepEqual(Object.keys(payload.registry).sort(), [
     "version", "installation_id", "session_id", "harness", "extension_path", "extension_sha256",
     "tool_registry_version", "bridge_extension_path", "bridge_extension_sha256", "scope_id", "runner_epoch",
     "launch_epoch", "origin", "canonical_cwd", "registry_generation", "scope_protocol_version", "tools", "loaded_extensions",
    ].sort());
   }
   observedControls.push(operation);
   return end.call(this, body);
  };
  return request;
 }
 if(url.pathname!=="/permission-tiers/control") return originalRequest(url,options,callback);
 return originalRequest(url,options,response=>{
  const on=response.on;
  response.on=function(name,handler){return on.call(this,name,name==="end"?function(...args){
   ackDispatch=true;try{return handler(...args);}finally{ackDispatch=false;}
  }:handler);};
  callback(response);
 });
};
const sdk = await import(pathToFileURL(path.join(sdkRoot,"dist/index.js")).href);
const require = createRequire(import.meta.url);
const aiRoot=path.dirname(findPackageJSON("@earendil-works/pi-ai",pathToFileURL(path.join(sdkRoot,"dist/index.js"))));
const {AssistantMessageEventStream} = await import(pathToFileURL(path.join(aiRoot,"dist/utils/event-stream.js")).href);
const {installScopedSdkFromHandoff} = await import(pathToFileURL(adapter).href);
const dispose = mode==="missing-bootstrap" ? ()=>{} : await installScopedSdkFromHandoff(sdk, locator, {sdkVersion:JSON.parse(fs.readFileSync(path.join(sdkRoot,"package.json"))).version,sdkEntry:path.join(sdkRoot,"dist/index.js")});
const bind=sdk.AgentSession.prototype.bindExtensions, observed=new WeakSet();
sdk.AgentSession.prototype.bindExtensions=function(...args){
 for(const tool of this.getAllTools()){
  const definition=this.getToolDefinition(tool.name);
  if(observed.has(definition))continue;
  observed.add(definition);const execute=definition.execute;
  definition.execute=function(...input){
   assert.equal(ackDispatch,true,"actual SDK entry occurs inside final ACK dispatch");
   definitionStarts++;return execute.apply(this,input);
  };
 }
 return bind.apply(this,args);
};
const cwd=path.join(root,"workspace"),agentDir=path.join(root,"agent");
fs.mkdirSync(agentDir,{recursive:true});
const helperNames=["subagent","bg_wait","contact_supervisor","subagents_enable"],forwardedNotices=[];
const discoveryGate = path.join(root, "mcp-discover");
if (mcpMode) {
 const server = path.join(root, "mcp-fixture.cjs");
 fs.writeFileSync(server, `const {existsSync, writeFileSync} = require("node:fs");
 const {setTimeout:delay} = require("node:timers/promises");
 require("node:readline").createInterface({input:process.stdin}).on("line", async line => {
  const {id, method, params} = JSON.parse(line);
  if (id === undefined) return;
  if (method === "tools/list") while (!existsSync(${JSON.stringify(discoveryGate)})) await delay(10);
  if (method === "tools/call") writeFileSync(${JSON.stringify(path.join(root, "mcp-called"))}, "called");
  const result = method === "initialize" ? {protocolVersion:params.protocolVersion, capabilities:{tools:{}}, serverInfo:{name:"fixture",version:"1"}}
    : method === "tools/list" ? {tools:[{name:"echo", description:"Fixture tool", inputSchema:{type:"object"}}]} : {};
  console.log(JSON.stringify({jsonrpc:"2.0",id,result}));
 });`);
 fs.writeFileSync(path.join(agentDir,"mcp.json"), JSON.stringify({mcpServers:{fixture:{command:process.execPath,args:[server],exposure:"direct"}}}));
}
let helperEntry;
const childLifecycles = new WeakMap();
if (mode === "mcp-child") globalThis[Symbol.for("fixture.native-lifecycles")] = childLifecycles;
if (mode === "incompatible" || mode === "mcp-child") {
 const directory=path.join(agentDir,"extensions/helper");fs.mkdirSync(directory,{recursive:true});
 const manifest=JSON.parse(fs.readFileSync(new URL("./pi_subagents_manifest.json",import.meta.url)));
 manifest.pi={extensions:["./index.cjs"]};
 if (mode === "mcp-child") {
  manifest.exports = {"./preflight":"./preflight.mjs", "./native-child-lifecycle":"./lifecycle.mjs", "./required-child-extensions":"./required.mjs"};
  fs.writeFileSync(path.join(directory,"preflight.mjs"), 'export const SUBAGENT_LAUNCH_CONTRACT_VERSION = 3; export function resolveSubagentLaunchContract() {}');
  fs.writeFileSync(path.join(directory,"lifecycle.mjs"), 'export const getNativeChildLifecycle = manager => globalThis[Symbol.for("fixture.native-lifecycles")].get(manager);');
  fs.writeFileSync(path.join(directory,"required.mjs"), 'export const registerRequiredChildExtensions = () => ({dispose(){}});');
 }
 fs.writeFileSync(path.join(directory,"package.json"),JSON.stringify(manifest));
 helperEntry=path.join(directory,"index.cjs");
 fs.writeFileSync(helperEntry,`module.exports=pi=>{for(const name of ${JSON.stringify(helperNames)})pi.registerTool({name,label:name,description:name,parameters:{type:"object",properties:{}},execute:async()=>{throw new Error("incompatible helper executed");}});};`);
}
sdk.initTheme("dark");
const settings=sdk.SettingsManager.inMemory({retry:{enabled:false},compaction:{enabled:false},...(helperEntry?{packages:[path.dirname(helperEntry)]}:{})});
const runtime=await sdk.ModelRuntime.create({authPath:path.join(agentDir,"auth.json"),modelsPath:null,refreshOnCreate:false});
const canary="pi_receipt_"+"m".repeat(43), events=[];
runtime.registerProvider("scoped-fixture",{
 api:"openai-completions",apiKey:"nonsecret-fixture",baseUrl:"http://127.0.0.1:1",
 models:[{id:"local",name:"Scripted local fixture",reasoning:false,input:["text"],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:64000,maxTokens:1024}],
 streamSimple(model,context){
  const stream=new AssistantMessageEventStream();
  const last=context.messages.findLastIndex(item=>item.role==="user");
  const done=context.messages.slice(last+1).some(item=>item.role==="toolResult");
  const user=JSON.stringify(context.messages[last]);
  const tool=[...helperNames,...omittedNames].find(name=>user.includes("helper "+name+'"')) || (user.includes("exception")?"sys_exception":user.includes("remote")?"sys_fixture":"read");
  const content=done?[{type:"text",text:"done"}]:[{type:"toolCall",id:"probe-"+tool,name:tool,arguments:tool==="read"?{path:"marker.txt"}:{}}];
  const message={role:"assistant",api:model.api,provider:model.provider,model:model.id,content,
   usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},
   stopReason:done?"stop":"toolUse",timestamp:Date.now()};
  queueMicrotask(()=>{stream.push({type:"start",partial:message});stream.push({type:"done",reason:message.stopReason,message});stream.end();});
  return stream;
 }
});
async function createSession({withMcp = false, child = false} = {}) {
 const discoveries = [];
 let loader;
 const observer = {name:"notice-observer", factory:api => {
  api.events.on("omnigent:pi-compatibility", text => forwardedNotices.push(text));
  if (withMcp) api.on("session_start", async (_event, ctx) => {
   const tools = loader.getExtensions().extensions.find(item=>item.path==="builtin:mcp").tools;
   const {promise, resolve} = Promise.withResolvers();
   discoveries.push(promise);
   const set = tools.set;
   tools.set = function (name, tool) {
    const result = set.call(this, name, tool);
    if (name === mcpName) resolve();
    return result;
   };
   if (mode === "mcp-startup") {
    fs.writeFileSync(discoveryGate, "allow");
    await promise;
    const {prepareNativeSession} = require(path.join(path.dirname(adapter), "native_support.cjs"));
    await prepareNativeSession(ctx);
    assert.deepEqual(forwardedNotices, [], "the notice consumer is not installed yet");
   }
  });
 }};
 const extensionFactories = [observer,...(extension?[{name:"omnigent-permission",factory:require(extension)}]:[])];
 let extensionsOverride;
 if (withMcp) {
  const {builtInExtensions} = await import(pathToFileURL(path.join(sdkRoot,"dist/extensions/index.js")).href);
  extensionFactories.unshift(...builtInExtensions.filter(item=>["mcp","tool-search"].includes(item.name)));
 }
 if (unknownMode) {
  extensionFactories.push({name:"future-fixture", factory:api=>api.registerTool({name:unknownName,label:"Fixture",description:"Fixture",parameters:{type:"object"},execute:async()=>assert.fail("unknown tool was executed")})});
  extensionsOverride = result => {
   result.extensions.find(item=>item.path === "<inline:future-fixture>").path = "<future:fixture>";
   return result;
  };
 }
 loader=new sdk.DefaultResourceLoader({cwd,agentDir,settingsManager:settings,noExtensions:!(helperEntry||withMcp||unknownMode),noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true,extensionFactories,extensionsOverride});
 await loader.reload();
 const tools = ["read", "sys_fixture", "sys_exception", ...helperNames];
 if (omissionMode) tools.push("tool_search", ...omittedNames);
 const sessionManager=sdk.SessionManager.create(cwd, path.join(root, child?"child-sessions":"sessions"));
 if (child) childLifecycles.set(sessionManager, {state:"open",closingSignal:new AbortController().signal,bindHost(){return {};}});
 const {session}=await sdk.createAgentSession({cwd,agentDir,resourceLoader:loader,modelRuntime:runtime,model:runtime.getModel("scoped-fixture","local"),sessionManager,settingsManager:settings,tools});
 return {session, discoveries};
}
async function assertOmitted(session, omittedName) {
 assert.equal(session.getAllTools().some(tool=>tool.name===omittedName),false);
 assert.equal(session.getCallableToolNames().includes(omittedName),false);
 assert.equal(session.getToolDefinition(omittedName),undefined);
 await session.prompt("Run helper "+omittedName);
 const result=session.messages.findLast(item=>item.role==="toolResult");
 assert.equal(result.toolName,omittedName);
 assert.equal(result.isError,true);
}

const bindings = {mode:"print",onError(error){throw new Error(String(error.error));}};
const parent = await createSession({withMcp:mcpMode && mode !== "mcp-child"});
let session = parent.session;
const alive=setInterval(()=>{},1000);
try{
 await session.bindExtensions(bindings);
 let discoveries = parent.discoveries;
 if (mode === "mcp-child") {
  ({session, discoveries} = await createSession({withMcp:true, child:true}));
  await session.bindExtensions(bindings);
 }
 session.subscribe(event=>events.push(event));
 if (mcpMode) {
  if (mode !== "mcp-startup") fs.writeFileSync(discoveryGate, "allow");
  assert.equal(discoveries.length, 1);
  await discoveries[0];
  assert.equal(session.getAllTools().find(tool=>tool.name==="tool_search")?.sourceInfo.path,"builtin:tool-search");
 }
 await session.prompt("Read marker.txt");
 if (omissionMode) {
  for (const name of omittedNames) await assertOmitted(session, name);
  if (mode === "mcp-late") {
   fs.unlinkSync(discoveryGate);
   await session.reload();
   assert.equal(discoveries.length, 2);
   fs.writeFileSync(discoveryGate, "allow again");
   await discoveries[1];
   await assertOmitted(session, mcpName);
  }
  const expectedKinds = [...(unknownMode?["unrecognized"]:[]), ...(mcpMode?["builtin:mcp"]:[])];
  assert.deepEqual(forwardedNotices, expectedKinds.map(kind=>`Scoped Pi left out tools from unsupported sources (1 total, ${kind}: 1).`));
  assert.equal(fs.existsSync(path.join(root,"mcp-called")),false);
 }
 if(mode==="incompatible"){
  for(const name of helperNames){
   await session.prompt("Run helper "+name);
   const result=session.messages.findLast(item=>item.role==="toolResult");
   assert.equal(result.toolName,name);assert.equal(result.isError,true,JSON.stringify(result));
  }
  assert.equal(definitionStarts,1,JSON.stringify(session.messages));
  const notices=session.messages.filter(item=>item.customType==="omnigent-pi-compatibility");
  assert.equal(notices.length,1,JSON.stringify(session.messages));
  assert.ok(notices[0].content.includes("./native-child-lifecycle"));
  assert.deepEqual(forwardedNotices,[notices[0].content]);
 }
 if(mode==="normal"){
  await session.prompt("Run remote fixture");
  await session.prompt("Run exception fixture");
 }
 const result=session.messages.find(item=>item.role==="toolResult");
 assert.ok(result,JSON.stringify(session.messages));
 if(mode==="normal" || mode==="incompatible" || omissionMode){
  assert.equal(result.isError,false,JSON.stringify(result));
  assert.ok(JSON.stringify(result).includes("REAL_SCOPED_READ"));
  if(mode==="normal"){
  assert.ok(session.messages.some(item=>item.role==="toolResult"&&item.toolName==="sys_fixture"&&!item.isError&&JSON.stringify(item).includes("REMOTE_SCOPED_RESULT")),JSON.stringify(session.messages));
  assert.ok(session.messages.some(item=>item.role==="toolResult"&&item.toolName==="sys_exception"&&item.isError),JSON.stringify(session.messages));
  assert.equal(definitionStarts,3);
  }
 }else{
  assert.equal(result.isError,true,JSON.stringify(result));
  assert.equal(definitionStarts,0);
  assert.ok(JSON.stringify(result).includes(mode==="missing-bootstrap"?"Scoped Pi bootstrap unavailable":"settlement was not acknowledged"));
 }
 assert.equal(JSON.stringify([session.messages,events]).includes(canary),false);
 const journal=fs.readFileSync(session.sessionManager.getSessionFile(),"utf8");
 assert.equal(journal.includes(canary),false);
 assert.equal(journal.includes("pi_scope_"),false);
 assert.equal(journal.includes("pi_attestor_"),false);
 fs.writeFileSync(path.join(root,"record.json"),JSON.stringify([session.messages,events]));
 console.log(JSON.stringify({sdk:JSON.parse(fs.readFileSync(path.join(sdkRoot,"package.json"))).version,result,events:events.length,definitionStarts,mode,receiptExcluded:true}));
 if(mode==="normal" || mode==="incompatible" || omissionMode)await session.extensionRunner.emit({type:"session_shutdown",reason:"quit"});
 else if(mode!=="missing-bootstrap")await assert.rejects(()=>session.extensionRunner.emit({type:"session_shutdown",reason:"quit"}),/Scoped Pi/);
 if (mode === "control-shape") {
  // The read is attested at policy evaluation and again immediately before execution.
  assert.deepEqual(observedControls, ["bind", "registry", "attest", "attest", "revoke"]);
 }
}finally{clearInterval(alive);session.dispose();if(session!==parent.session)parent.session.dispose();dispose();http.request=originalRequest;}
