import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import http from "node:http";
import {pathToFileURL} from "node:url";
const [sdkRoot, locator, root, adapter, extension, mode="normal"] = process.argv.slice(2);
let ackDispatch=false, definitionStarts=0;
const originalRequest=http.request;
http.request=function(url,options,callback){
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
const {AssistantMessageEventStream} = await import(pathToFileURL(path.join(sdkRoot,"node_modules/@earendil-works/pi-ai/dist/utils/event-stream.js")).href);
const {installScopedSdkFromHandoff} = await import(pathToFileURL(adapter).href);
const dispose = await installScopedSdkFromHandoff(sdk, locator, {sdkVersion:JSON.parse(fs.readFileSync(path.join(sdkRoot,"package.json"))).version,sdkEntry:path.join(sdkRoot,"dist/index.js")});
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
sdk.initTheme("dark");
const settings=sdk.SettingsManager.inMemory({retry:{enabled:false},compaction:{enabled:false}});
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
  const tool=user.includes("exception")?"sys_exception":user.includes("remote")?"sys_fixture":"read";
  const content=done?[{type:"text",text:"done"}]:[{type:"toolCall",id:"probe-"+tool,name:tool,arguments:tool==="read"?{path:"marker.txt"}:{}}];
  const message={role:"assistant",api:model.api,provider:model.provider,model:model.id,content,
   usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},
   stopReason:done?"stop":"toolUse",timestamp:Date.now()};
  queueMicrotask(()=>{stream.push({type:"start",partial:message});stream.push({type:"done",reason:message.stopReason,message});stream.end();});
  return stream;
 }
});
const loader=new sdk.DefaultResourceLoader({cwd,agentDir,settingsManager:settings,noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true,extensionFactories:extension?[{name:"omnigent-permission",factory:(await import("node:module")).createRequire(import.meta.url)(extension)}]:[]});
await loader.reload();
const {session}=await sdk.createAgentSession({cwd,agentDir,resourceLoader:loader,modelRuntime:runtime,model:runtime.getModel("scoped-fixture","local"),
 sessionManager:sdk.SessionManager.create(cwd,path.join(root,"sessions")),settingsManager:settings,tools:["read","sys_fixture","sys_exception"]});
const alive=setInterval(()=>{},1000);
try{
 await session.bindExtensions({mode:"print",onError(error){throw new Error(String(error.error));}});
 session.subscribe(event=>events.push(event));
 await session.prompt("Read marker.txt");
 if(mode==="normal"){
  await session.prompt("Run remote fixture");
  await session.prompt("Run exception fixture");
 }
 const result=session.messages.find(item=>item.role==="toolResult");
 assert.ok(result,JSON.stringify(session.messages));
 if(mode==="normal"){
  assert.equal(result.isError,false,JSON.stringify(result));
  assert.ok(JSON.stringify(result).includes("REAL_SCOPED_READ"));
  assert.ok(session.messages.some(item=>item.role==="toolResult"&&item.toolName==="sys_fixture"&&!item.isError&&JSON.stringify(item).includes("REMOTE_SCOPED_RESULT")),JSON.stringify(session.messages));
  assert.ok(session.messages.some(item=>item.role==="toolResult"&&item.toolName==="sys_exception"&&item.isError),JSON.stringify(session.messages));
  assert.equal(definitionStarts,3);
 }else{
  assert.equal(result.isError,true,JSON.stringify(result));
  assert.equal(definitionStarts,0);
  assert.ok(JSON.stringify(result).includes("settlement was not acknowledged"));
 }
 assert.equal(JSON.stringify([session.messages,events]).includes(canary),false);
 const journal=fs.readFileSync(session.sessionManager.getSessionFile(),"utf8");
 assert.equal(journal.includes(canary),false);
 assert.equal(journal.includes("pi_scope_"),false);
 assert.equal(journal.includes("pi_attestor_"),false);
 fs.writeFileSync(path.join(root,"record.json"),JSON.stringify([session.messages,events]));
 console.log(JSON.stringify({sdk:JSON.parse(fs.readFileSync(path.join(sdkRoot,"package.json"))).version,result,events:events.length,definitionStarts,mode,receiptExcluded:true}));
 if(mode==="normal")await session.extensionRunner.emit({type:"session_shutdown",reason:"quit"});
 else await assert.rejects(()=>session.extensionRunner.emit({type:"session_shutdown",reason:"quit"}),/Scoped Pi/);
}finally{clearInterval(alive);session.dispose();dispose();http.request=originalRequest;}
