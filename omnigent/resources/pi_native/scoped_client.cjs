const crypto = require("node:crypto");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");

const privateKeys = new Set(["pi_origin_receipt","permission_tier_binding","scope_credential","attestor_credential","process_capability"]);
function publicValue(value) {
  if(typeof value === "string") return value.replace(/\bpi_(?:scope|receipt|attestor)_[A-Za-z0-9_-]{32,256}/g,"[private Pi authority]")
    .replace(/(\b(?:pi_origin_receipt|scope_credential|attestor_credential|process_capability)["']?\s*[:=]\s*["']?)[A-Za-z0-9_-]+/g,"$1[private Pi authority]");
  if(Array.isArray(value)) return value.map(publicValue);
  if(value && typeof value === "object") return Object.fromEntries(Object.entries(value).filter(([key])=>!privateKeys.has(key)).map(([key,item])=>[key,publicValue(item)]));
  return value;
}
const denied = () => ({block:true,reason:"Scoped Pi authority is unavailable"});
const failedResult = () => ({isError:true,content:[{type:"text",text:"Scoped Pi execution is unavailable"}]});
const fingerprint = event => JSON.stringify([event.toolName,event.input]);

/** The injected transport is private bootstrap code, never a model-facing API. */
function createScopedClient({scope, transport, readState, recheck}) {
  const calls = new Map(), finished = new Set(), evaluating = new Set();
  let closed = false;
  const current = revision => {
    const state=readState();
    return !closed && transport.isCurrent(scope) && state?.status==="ready" && state.revision===revision;
  };
  const seal = () => {closed=true;for(const [id,call] of calls) if(call.phase!=="executing") calls.delete(id);};
  transport.onLoss(seal);
  function metadata(call) {
    return {...call.binding,expected_revision:call.revision,pi_origin_receipt:call.receipt,
      name:call.event.toolName,arguments:call.event.input,operation_id:call.operation,step:"initial"};
  }
  async function evaluate(event, _ctx, support) {
    const state=readState();
    if(!event || typeof event.toolCallId!=="string" || !event.toolCallId || !current(state?.revision)) return denied();
    const id=event.toolCallId, signature=fingerprint(event), old=calls.get(id);
    if(finished.has(id) || evaluating.has(id) || calls.size+finished.size>=32768) return denied();
    if(old) return old.signature===signature && old.phase==="allowed" ? {block:false} : denied();
    evaluating.add(id);
    try {
      await transport.attest(event,support,state.revision);
      if(!current(state.revision)) return denied();
      const response=await transport.evaluate(event,state.revision);
      if(!current(state.revision)) return denied();
      if(!["POLICY_ACTION_ALLOW","POLICY_ACTION_UNSPECIFIED"].includes(response?.result))
        return {block:true,reason:publicValue(typeof response?.reason==="string"?response.reason:"Scoped Pi policy denied")};
      const binding=response.permission_tier_binding;
      const canonical="pi:"+crypto.createHash("sha256").update(scope.scope_id+"\0"+id).digest("hex");
      if(!binding || binding.call_id!==canonical || binding.revision!==state.revision ||
        !/^[0-9a-f]{64}$/.test(binding.arguments_digest||"") || !/^pi_receipt_[A-Za-z0-9_-]{32,256}$/.test(binding.pi_origin_receipt||"")) return denied();
      calls.set(id,{event:JSON.parse(JSON.stringify(event)),signature,revision:state.revision,
        binding:{call_id:canonical,arguments_digest:binding.arguments_digest},receipt:binding.pi_origin_receipt,
        support:JSON.stringify(support),operation:crypto.randomUUID(),phase:"allowed"});
      return {block:false};
    } catch (_) { return denied(); } finally {evaluating.delete(id);}
  }
  async function execute(event, ctx, start) {
    const call=calls.get(event.toolCallId);
    if(!call || call.phase!=="allowed" || call.signature!==fingerprint(event) || !current(call.revision)) return failedResult();
    call.phase="checking";
    try {
      const support=await recheck(event,ctx);
      if(JSON.stringify(support)!==call.support || !current(call.revision)) {seal();return failedResult();}
      await transport.attest(event,support,call.revision);
      if(!current(call.revision)) return failedResult();
      return await new Promise(resolve=>{
        transport.execute(metadata(call),ack=>{
          // Enter the actual definition in this ACK dispatch, never retain a start permit.
          if(ack?.executed!==true || !current(call.revision) || call.phase!=="checking") {resolve(failedResult());return;}
          call.phase="executing";
          try {Promise.resolve(start()).then(value=>resolve(publicValue(value)),()=>resolve(failedResult()));}
          catch (_) {resolve(failedResult());}
        }).catch(()=>resolve(failedResult()));
      });
    } catch (_) {return failedResult();}
  }
  async function finish(event) {
    if(finished.has(event.toolCallId)) return true;
    const call=calls.get(event.toolCallId);
    if(!call || call.phase!=="executing") return false;
    try {
      const reply=await transport.finish(metadata(call));
      if(reply?.finished!==true) return false;
      calls.delete(event.toolCallId);finished.add(event.toolCallId);return true;
    } catch (_) {return false;}
  }
  async function close() {
    seal();
    const reply=await transport.revoke();
    if(reply?.revoked!==true || reply.scope_id!==scope.scope_id || reply.installation_id!==scope.installation_id)
      throw new Error("Scoped Pi revocation was not acknowledged");
    return reply;
  }
  async function bridge(event, port) {
    const call=calls.get(event.toolCallId);
    if(!call || call.phase!=="executing" || call.signature!==fingerprint(event) || !current(call.revision))
      return {error:"Scoped Pi bridge unavailable"};
    try {return publicValue(await transport.rpc(port,metadata(call)));}
    catch (_) {return {error:"Scoped Pi bridge unavailable"};}
  }
  return Object.freeze({evaluate,execute,finish,close,seal,bridge,project:publicValue});
}

/** A pinned, loopback-only control/continuity channel; capabilities stay in this closure. */
async function connectAttestor({endpoint,credential,brokerIncarnation,launchEpoch}) {
  const url=new URL(endpoint);
  if(url.protocol!=="http:" || !["127.0.0.1","[::1]"].includes(url.hostname) || !url.port ||
    url.username || url.password || url.search || url.hash || !["","/"].includes(url.pathname))
    throw new Error("Scoped Pi endpoint is not trusted loopback");
  let live=true, ready=false, sequence=0, queue=Promise.resolve();
  const listeners=new Set(), scopeListeners=new Map(), revoked=new Set();
  const lose=()=>{live=false;ready=false;for(const callback of listeners)callback();};
  function request(route,body,token,onResponse) {
    return new Promise((resolve,reject)=>{
      const bytes=JSON.stringify(body);
      const req=http.request(new URL(route,url),{method:"POST",headers:{authorization:`Bearer ${token}`,"content-type":"application/json","content-length":Buffer.byteLength(bytes)}},res=>{
        if(res.statusCode!==200) {res.resume();reject(new Error("Scoped Pi transport rejected"));return;}
        let text="";
        res.on("data",chunk=>{text+=chunk;if(text.length>1024*1024)res.destroy();});
        res.on("end",()=>{try {const reply=JSON.parse(text);if(onResponse)onResponse(reply);resolve(reply);}catch(_){reject(new Error("Scoped Pi transport response unavailable"));}});
        res.on("error",()=>reject(new Error("Scoped Pi transport response unavailable")));
      });
      req.setTimeout(2000,()=>req.destroy());
      req.on("error",()=>reject(new Error("Scoped Pi transport unavailable")));
      req.end(bytes);
    });
  }
  const watch=http.get(new URL("/pi/bootstrap/watch",url),{headers:{authorization:`Bearer ${credential}`}},res=>{
    if(res.statusCode!==200) {res.resume();lose();return;}
    let text="";
    res.on("data",chunk=>{
      text+=chunk;
      if(text.length>16384){res.destroy();lose();return;}
      let end;
      while((end=text.indexOf("\n"))!==-1){
        const line=text.slice(0,end);text=text.slice(end+1);
        try {
          const frame=JSON.parse(line);
          if(frame.incarnation!==brokerIncarnation || frame.launch_epoch!==launchEpoch || frame.live!==true || !Array.isArray(frame.revoked_scopes))lose();
          else if(live){
            for(const id of frame.revoked_scopes){revoked.add(id);for(const callback of scopeListeners.get(id)||[])callback();}
            ready=true;
          }
        }
        catch(_){lose();}
      }
    });
    res.on("close",lose);res.on("end",lose);res.on("error",lose);
  });
  watch.on("error",lose);
  for(let count=0;count<100&&!ready&&live;count++)await new Promise(resolve=>setTimeout(resolve,10));
  if(!ready||!live){watch.destroy();throw new Error("Scoped Pi continuity unavailable");}
  async function control(op,values) {
    const task=queue.then(async()=>{
      if(!live||!ready)throw new Error("Scoped Pi continuity unavailable");
      const reply=await request("/pi/bootstrap",{protocol:1,sequence:++sequence,op,...values},credential);
      if(reply.broker_incarnation!==undefined&&reply.broker_incarnation!==brokerIncarnation){lose();throw new Error("Scoped Pi broker changed");}
      return reply;
    });
    queue=task.catch(()=>{});return task;
  }
  function client(binding,handle,readState,recheck) {
    const scope=binding.scope, scopeCredential=binding.credential;
    return createScopedClient({scope,readState,recheck,transport:{
      isCurrent:value=>live&&ready&&!revoked.has(value.scope_id)&&value.runner_epoch===scope.runner_epoch&&value.launch_epoch===launchEpoch,
      onLoss:callback=>{listeners.add(callback);const set=scopeListeners.get(scope.scope_id)||new Set();set.add(callback);scopeListeners.set(scope.scope_id,set);if(revoked.has(scope.scope_id))callback();},
      attest:(event,support,revision)=>control("attest",{handle,call_id:event.toolCallId,name:event.toolName,arguments:event.input,revision,support}),
      evaluate:(event,revision)=>request("/policies/evaluate",{event:{type:"PHASE_TOOL_CALL",target:"",context:{},data:{name:event.toolName,arguments:event.input,call_id:event.toolCallId,expected_permission_tier_revision:revision}}},scopeCredential),
      execute:(metadata,onResponse)=>request("/permission-tiers/control",{type:"permission_tier_execute",...metadata},scopeCredential,onResponse),
      finish:metadata=>request("/permission-tiers/control",{type:"permission_tier_finish",...metadata},scopeCredential),
      revoke:()=>control("revoke",{handle}),
      rpc:(port,metadata)=>new Promise((resolve,reject)=>{
        if(!Number.isInteger(port)||port<1||port>65535){reject(new Error("Scoped Pi bridge unavailable"));return;}
        const id=crypto.randomUUID();let text="",settled=false;
        const socket=net.createConnection({port,host:"127.0.0.1"});
        const fail=()=>{if(!settled){settled=true;reject(new Error("Scoped Pi bridge unavailable"));}socket.destroy();};
        socket.on("connect",()=>socket.write(JSON.stringify({id,kind:"pi_scoped",credential:scopeCredential,payload:JSON.stringify(metadata)})+"\n"));
        socket.on("data",chunk=>{
          text+=chunk;
          if(text.length>1024*1024){fail();return;}
          const end=text.indexOf("\n");if(end<0)return;
          try {const reply=JSON.parse(text.slice(0,end));if(reply.id!==id){fail();return;}settled=true;resolve(reply);socket.destroy();}
          catch(_){fail();}
        });
        socket.on("error",fail);socket.on("close",()=>{if(!settled)fail();});
        socket.setTimeout(86400000,fail);
      }),
    }});
  }
  return Object.freeze({bind:values=>control("bind",values),client,close:()=>{lose();watch.destroy();}});
}

async function connectFromHandoff(locator, {timeoutMs=8000}={}) {
  if(typeof locator!=="string" || !path.isAbsolute(locator) || !/^[a-f0-9]{32}\.json$/.test(path.basename(locator)))
    throw new Error("Scoped Pi handoff locator unavailable");
  const directory=path.dirname(locator), info=fs.lstatSync(directory);
  if(!info.isDirectory() || info.mode&0o077 || process.getuid&&info.uid!==process.getuid() || fs.realpathSync(directory)!==directory)
    throw new Error("Scoped Pi handoff directory unavailable");
  const deadline=Date.now()+timeoutMs;
  while(!fs.existsSync(locator) && Date.now()<deadline) await new Promise(resolve=>setTimeout(resolve,10));
  let descriptor;
  const fd=fs.openSync(locator,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW|fs.constants.O_NONBLOCK);
  try {
    const opened=fs.fstatSync(fd), before=fs.lstatSync(locator);
    if(!opened.isFile() || !fs.constants.O_NOFOLLOW || opened.mode&0o077 ||
      process.getuid&&opened.uid!==process.getuid() || before.ino!==opened.ino || before.dev!==opened.dev || opened.size>16384)
      throw new Error("Scoped Pi handoff identity unavailable");
    descriptor=JSON.parse(fs.readFileSync(fd,"utf8"));
    const after=fs.lstatSync(locator);
    if(after.ino!==opened.ino || after.dev!==opened.dev || after.size!==opened.size || after.mtimeMs!==opened.mtimeMs)
      throw new Error("Scoped Pi handoff changed");
    fs.unlinkSync(locator);
  } finally {fs.closeSync(fd);}
  if(descriptor?.protocol!==1 || descriptor.pid!==process.pid ||
    !/^pi_attestor_[A-Za-z0-9_-]{43}$/.test(descriptor.credential||"") ||
    descriptor.bridgeDir!==path.dirname(directory) || !path.isAbsolute(descriptor.statePath||""))
    throw new Error("Scoped Pi handoff binding unavailable");
  const connection=await connectAttestor(descriptor);
  return Object.freeze({connection,bridgeDir:descriptor.bridgeDir,statePath:descriptor.statePath});
}

module.exports={createScopedClient,connectAttestor,connectFromHandoff,publicValue};
