const crypto = require("node:crypto");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");

const privateKeys = new Set(["pi_origin_receipt","permission_tier_binding","scope_credential","attestor_credential","process_capability"]);
function publicValue(value) {
  if(typeof value === "string") return value.replace(/pi_(?:scope|receipt|attestor)_[A-Za-z0-9_-]{32,}/g,"[private Pi authority]")
    .replace(/(\b(?:pi_origin_receipt|scope_credential|attestor_credential|process_capability)["']?\s*[:=]\s*["']?)[A-Za-z0-9_-]+/g,"$1[private Pi authority]");
  if(Array.isArray(value)) return value.map(publicValue);
  if(value && typeof value === "object") return Object.fromEntries(Object.entries(value).filter(([key])=>!privateKeys.has(key)).map(([key,item])=>[key,publicValue(item)]));
  return value;
}
const denied = cause => ({block:true,reason:"Scoped Pi authority is unavailable"+(cause?
  ` (${cause}). ${["state_not_ready","revision_changed"].includes(cause)?"Wait for the permission update; if it persists, restart the Pi session.":"Restart the Pi session to establish fresh scoped authority."}`:"")});
const failedResult = () => ({isError:true,content:[{type:"text",text:"Scoped Pi execution is unavailable"}]});
function toolErrorResult(error) {
  let text="";
  try {
    const raw=typeof error==="string"?error:
      error&&typeof error==="object"&&typeof error.message==="string"?error.message:
      error==null?"":String(error);
    text=typeof raw==="string"&&!raw.includes("permission_tier_binding")?publicValue(raw):"";
  } catch (_) {
    text="";
  }
  return {isError:true,content:[{type:"text",text:text.trim()?text:"Tool execution failed"}],details:{}};
}
const fingerprint = event => JSON.stringify([event.toolName,event.input]);
// Only fixed codes and elapsed time cross this boundary; never pass an event,
// state, scope, response or exception (even through publicValue) to diagnostics.
const diagnostic = () => {};
const beforeDispatch = new Set(["call_missing","call_not_executing","signature_changed","transport_not_current",
  "state_not_ready","revision_changed","transport_lost","scope_revoked","support_changed","state_changed",
  "client_close","lifecycle_closing","installation_closed","explicit_seal","binding_unavailable","rpc_refused","rpc_configuration_invalid"]);
const deliveryFailures = new Set(["rpc_delivery_failed","rpc_timeout","rpc_http_rejected","rpc_response_invalid","rpc_response_too_large","rpc_execution_failed"]);
function bridgeFailure(reason) {
  if(!beforeDispatch.has(reason)&&!deliveryFailures.has(reason))reason="rpc_delivery_failed";
  const detail=beforeDispatch.has(reason)?"command was not dispatched by this bridge":
    reason==="rpc_response_too_large"?"command ran, but its reply exceeded the 1 MiB limit":
    "command may have run; check its outcome before retrying";
  return {error:`Scoped Pi bridge unavailable (${reason}); ${detail}.`,reason};
}
function rpcError(reason) {return Object.assign(new Error("Scoped Pi transport unavailable"),{bridgeReason:reason});}
function fileDiagnostic(directory) {
  const destination=path.join(directory,"scoped-pi.log");
  return record=>{
    fs.appendFileSync(destination,"[scoped-pi] "+JSON.stringify({time:new Date().toISOString(),...record})+"\n",{mode:0o600});
  };
}
function reportDiagnostic(report, record) {
  try {report(record);} catch (_) { /* Diagnostics must not affect authority. */ }
}

/** The injected transport is private bootstrap code, never a model-facing API. */
function createScopedClient({scope, transport, readState, recheck, report=diagnostic}) {
  const calls = new Map(), finished = new Set(), evaluating = new Set();
  let closedCause = null;
  const unavailable = revision => {
    const state=readState();
    if(closedCause)return closedCause;
    if(!transport.isCurrent(scope))return "transport_not_current";
    if(state?.status!=="ready")return "state_not_ready";
    if(state.revision!==revision)return "revision_changed";
    return null;
  };
  const current = revision => unavailable(revision)===null;
  const seal = (cause="explicit_seal") => {
    if(closedCause)return;
    closedCause=["transport_lost","scope_revoked","support_changed","state_changed","client_close",
      "lifecycle_closing","installation_closed"].includes(cause)?cause:"explicit_seal";
    reportDiagnostic(report,{event:"client_sealed",cause:closedCause});
    for(const [id,call] of calls) if(call.phase!=="executing") calls.delete(id);
  };
  transport.onLoss(cause=>seal(cause==="scope_revoked"?cause:"transport_lost"));
  function metadata(call) {
    return {...call.binding,expected_revision:call.revision,pi_origin_receipt:call.receipt,
      name:call.event.toolName,arguments:call.event.input,operation_id:call.operation,step:"initial"};
  }
  async function evaluate(event, _ctx, support) {
    const started=performance.now();
    let stage="initial";
    const refuse=(branch,cause)=>{
      reportDiagnostic(report,{event:"evaluate_denied",branch,stage,elapsed_ms:Math.round(performance.now()-started),...(cause?{cause}:{})});
      return denied(cause);
    };
    const refuseCurrent=revision=>refuse("not_current",unavailable(revision));
    const state=readState();
    if(!event || typeof event.toolCallId!=="string" || !event.toolCallId) return refuse("invalid_event");
    if(!current(state?.revision)) return refuseCurrent(state?.revision);
    const id=event.toolCallId, signature=fingerprint(event), old=calls.get(id);
    if(finished.has(id) || evaluating.has(id) || calls.size+finished.size>=32768) return refuse("replay_or_capacity");
    if(old) return old.signature===signature && old.phase==="allowed" ? {block:false} : refuse("call_changed");
    evaluating.add(id);
    try {
      stage="attest";
      await transport.attest(event,support,state.revision);
      if(!current(state.revision)) return refuseCurrent(state.revision);
      stage="policy";
      const response=await transport.evaluate(event,state.revision);
      if(!current(state.revision)) return refuseCurrent(state.revision);
      if(!["POLICY_ACTION_ALLOW","POLICY_ACTION_UNSPECIFIED"].includes(response?.result))
        return {block:true,reason:publicValue(typeof response?.reason==="string"?response.reason:"Scoped Pi policy denied")};
      const binding=response.permission_tier_binding;
      const canonical="pi:"+crypto.createHash("sha256").update(scope.scope_id+"\0"+id).digest("hex");
      if(!binding || binding.call_id!==canonical || binding.revision!==state.revision ||
        !/^[0-9a-f]{64}$/.test(binding.arguments_digest||"") || !/^pi_receipt_[A-Za-z0-9_-]{32,256}$/.test(binding.pi_origin_receipt||"")) return refuse("invalid_binding");
      calls.set(id,{event:JSON.parse(JSON.stringify(event)),signature,revision:state.revision,
        binding:{call_id:canonical,arguments_digest:binding.arguments_digest},receipt:binding.pi_origin_receipt,
        support:JSON.stringify(support),operation:crypto.randomUUID(),phase:"allowed"});
      return {block:false};
    } catch (_) { return refuse("transport_exception"); } finally {evaluating.delete(id);}
  }
  async function execute(event, ctx, start) {
    const call=calls.get(event.toolCallId);
    if(!call || call.phase!=="allowed" || call.signature!==fingerprint(event) || !current(call.revision)) return failedResult();
    call.phase="checking";
    try {
      const support=await recheck(event,ctx);
      if(JSON.stringify(support)!==call.support || !current(call.revision)) {
        seal(JSON.stringify(support)!==call.support?"support_changed":"state_changed");return failedResult();
      }
      await transport.attest(event,support,call.revision);
      if(!current(call.revision)) return failedResult();
      return await new Promise(resolve=>{
        transport.execute(metadata(call),ack=>{
          // Enter the actual definition in this ACK dispatch, never retain a start permit.
          if(ack?.executed!==true || !current(call.revision) || call.phase!=="checking") {resolve(failedResult());return;}
          call.phase="executing";
          try {Promise.resolve(start()).then(value=>resolve(publicValue(value)),error=>resolve(toolErrorResult(error)));}
          catch (error) {resolve(toolErrorResult(error));}
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
    seal("client_close");
    const reply=await transport.revoke();
    if(reply?.revoked!==true || reply.scope_id!==scope.scope_id || reply.installation_id!==scope.installation_id)
      throw new Error("Scoped Pi revocation was not acknowledged");
    return reply;
  }
  async function bridge(event, port) {
    const call=calls.get(event.toolCallId);
    const refusal=!call?"call_missing":call.phase!=="executing"?"call_not_executing":
      call.signature!==fingerprint(event)?"signature_changed":unavailable(call.revision);
    if(refusal)return bridgeFailure(refusal);
    try {
      const reply=await transport.rpc(port,metadata(call));
      if(reply?.error)return bridgeFailure(reply.reason);
      if(!reply || !Object.hasOwn(reply,"result"))return bridgeFailure("rpc_response_invalid");
      return publicValue(reply);
    } catch (error) {return bridgeFailure(error?.bridgeReason);}
  }
  return Object.freeze({evaluate,execute,finish,close,seal,bridge,project:publicValue});
}

/** A pinned, loopback-only control/continuity channel; capabilities stay in this closure. */
async function connectAttestor({endpoint,credential,brokerIncarnation,launchEpoch,report=diagnostic,onLoss=diagnostic,deadline=performance.now()+60000}) {
  const url=new URL(endpoint);
  if(url.protocol!=="http:" || !["127.0.0.1","[::1]"].includes(url.hostname) || !url.port ||
    url.username || url.password || url.search || url.hash || !["","/"].includes(url.pathname))
    throw new Error("Scoped Pi endpoint is not trusted loopback");
  let live=true, ready=false, sequence=0, queue=Promise.resolve();
  const lifetime=new AbortController();
  const listeners=new Set(), scopeListeners=new Map(), revoked=new Set();
  const lose=cause=>{
    if(!live)return;
    live=false;ready=false;lifetime.abort();
    reportDiagnostic(report,{event:"continuity_lost",cause});
    for(const callback of listeners)callback("transport_lost");
    if(cause!=="connection_close")onLoss();
  };
  function request(route,body,token,onResponse,signal) {
    signal=signal?AbortSignal.any([signal,lifetime.signal]):lifetime.signal;
    return new Promise((resolve,reject)=>{
      const started=performance.now();
      let reported=false;
      const failure=outcome=>{
        if(reported)return;
        reported=true;
        reportDiagnostic(report,{event:"request_failed",route,outcome,elapsed_ms:Math.round(performance.now()-started)});
      };
      const bytes=JSON.stringify(body);
      const req=http.request(new URL(route,url),{method:"POST",signal,headers:{authorization:`Bearer ${token}`,"content-type":"application/json","content-length":Buffer.byteLength(bytes)}},res=>{
        if(res.statusCode!==200) {
          failure("http_rejected");
          if(route!=="/pi/rpc"){res.resume();reject(rpcError("rpc_http_rejected"));return;}
        }
        let text="",received=0;
        res.setEncoding("utf8");
        res.on("data",chunk=>{received+=Buffer.byteLength(chunk);if(received>1024*1024){reject(rpcError("rpc_response_too_large"));res.destroy();return;}text+=chunk;});
        res.on("end",()=>{try {
          const reply=JSON.parse(text);
          if(res.statusCode!==200){resolve(bridgeFailure(reply?.reason||"rpc_http_rejected"));return;}
          if(onResponse)onResponse(reply);resolve(reply);
        }catch(_){failure("response_invalid");reject(rpcError("rpc_response_invalid"));}});
        res.on("error",()=>{failure("response_error");reject(rpcError("rpc_delivery_failed"));});
      });
      req.setTimeout(route==="/pi/bootstrap" || route==="/pi/rpc" ? 0 : route==="/policies/evaluate" ? 86400000 : 60000,()=>{reject(rpcError("rpc_timeout"));req.destroy();});
      req.on("error",()=>reject(rpcError("rpc_delivery_failed")));
      req.end(bytes);

      req.on("timeout",()=>failure("timeout"));
      req.on("error",()=>failure(signal?.aborted?"deadline":"request_error"));
    });
  }
  const watch=http.get(new URL("/pi/bootstrap/watch",url),{headers:{authorization:`Bearer ${credential}`}},res=>{
    if(res.statusCode!==200) {res.resume();lose("watch_http_rejected");return;}
    let text="";
    res.on("data",chunk=>{
      text+=chunk;
      let end;
      while((end=text.indexOf("\n"))!==-1){
        if(end>16384){res.destroy();lose("watch_buffer_limit");return;}
        const line=text.slice(0,end);text=text.slice(end+1);
        try {
          const frame=JSON.parse(line);
          if(frame.incarnation!==brokerIncarnation || frame.launch_epoch!==launchEpoch || frame.live!==true || !Array.isArray(frame.revoked_scopes))lose("watch_frame_invalid");
          else if(live){
            for(const id of frame.revoked_scopes){revoked.add(id);for(const callback of scopeListeners.get(id)||[])callback("scope_revoked");}
            if(!ready)reportDiagnostic(report,{event:"continuity_ready"});
            ready=true;
          }
        }
        catch(_){lose("watch_frame_invalid");}
      }
      if(text.length>16384){res.destroy();lose("watch_buffer_limit");}
    });
    res.on("close",()=>lose("watch_close"));res.on("end",()=>lose("watch_end"));res.on("error",()=>lose("watch_error"));
  });
  watch.on("error",()=>lose("watch_request_error"));
  while(performance.now()<deadline&&!ready&&live)await new Promise(resolve=>setTimeout(resolve,10));
  if(!ready||!live){watch.destroy();throw new Error("Scoped Pi continuity unavailable");}
  async function control(op,values,scopeSignal) {
    // Queueing and transport share one control deadline; never replay a bind.
    const deadline=performance.now()+65000;
    const signal=AbortSignal.any([AbortSignal.timeout(65000),lifetime.signal,...(scopeSignal?[scopeSignal]:[])]);
    const task=queue.then(async()=>{
      if(deadline!==undefined && performance.now()>=deadline) throw new Error("Scoped Pi attestation deadline");
      signal?.throwIfAborted();
      if(!live||!ready)throw new Error("Scoped Pi continuity unavailable");
      const reply=await request("/pi/bootstrap",{protocol:1,sequence:++sequence,op,...values},credential,undefined,signal);
      if(reply.broker_incarnation!==undefined&&reply.broker_incarnation!==brokerIncarnation){lose("broker_changed");throw new Error("Scoped Pi broker changed");}
      return reply;
    });
    queue=task.catch(()=>{});
    if(!signal)return task;
    return new Promise((resolve,reject)=>{
      const abort=()=>{
        reject(new Error("Scoped Pi attestation deadline"));
      };
      signal.addEventListener("abort",abort,{once:true});
      task.then(resolve,reject).finally(()=>signal.removeEventListener("abort",abort));
    });
  }
  function client(binding,handle,readState,recheck) {
    const scope=binding.scope, scopeCredential=binding.credential, scopeLifetime=new AbortController();
    return createScopedClient({scope,readState,recheck,report,transport:{
      isCurrent:value=>live&&ready&&!revoked.has(value.scope_id)&&value.runner_epoch===scope.runner_epoch&&value.launch_epoch===launchEpoch,
      onLoss:callback=>{const close=cause=>{scopeLifetime.abort();callback(cause);};listeners.add(close);const set=scopeListeners.get(scope.scope_id)||new Set();set.add(close);scopeListeners.set(scope.scope_id,set);if(revoked.has(scope.scope_id))close("scope_revoked");},
      attest:(event,support,revision)=>control("attest",{handle,call_id:event.toolCallId,name:event.toolName,arguments:event.input,revision,support},scopeLifetime.signal),
      evaluate:(event,revision)=>request("/policies/evaluate",{event:{type:"PHASE_TOOL_CALL",target:"",context:{},data:{name:event.toolName,arguments:event.input,call_id:event.toolCallId,expected_permission_tier_revision:revision}}},scopeCredential),
      execute:(metadata,onResponse)=>request("/permission-tiers/control",{type:"permission_tier_execute",...metadata},scopeCredential,onResponse),
      finish:metadata=>request("/permission-tiers/control",{type:"permission_tier_finish",...metadata},scopeCredential),
      revoke:()=>{scopeLifetime.abort();return control("revoke",{handle});},
      rpc:(port,metadata)=>{
        if(port===0||port===null||port===undefined)
          return request("/pi/rpc",{payload:JSON.stringify(metadata)},scopeCredential,undefined,scopeLifetime.signal);
        return new Promise((resolve,reject)=>{
          if(!Number.isInteger(port)||port<1||port>65535){reject(rpcError("rpc_configuration_invalid"));return;}
          const id=crypto.randomUUID();let text="",received=0,settled=false;
          const socket=net.createConnection({port,host:"127.0.0.1",signal:AbortSignal.any([lifetime.signal,scopeLifetime.signal])});
          socket.setEncoding("utf8");
          const fail=(reason="rpc_delivery_failed")=>{if(!settled){settled=true;reject(rpcError(reason));}socket.destroy();};
          socket.on("connect",()=>socket.write(JSON.stringify({id,kind:"pi_scoped",credential:scopeCredential,payload:JSON.stringify(metadata)})+"\n"));
          socket.on("data",chunk=>{
            received+=Buffer.byteLength(chunk);
            if(received>1024*1024){fail("rpc_response_too_large");return;}
            text+=chunk;
            const end=text.indexOf("\n");if(end<0)return;
            try {const reply=JSON.parse(text.slice(0,end));if(reply.id!==id){fail("rpc_response_invalid");return;}settled=true;resolve(reply);socket.destroy();}
            catch(_){fail("rpc_response_invalid");}
          });
          socket.on("error",()=>fail());socket.on("close",()=>{if(!settled)fail();});
        });
      },
    }});
  }
  return Object.freeze({bind:values=>control("bind",values),client,close:()=>{lose("connection_close");watch.destroy();}});
}

async function connectFromHandoff(locator, {timeoutMs=120000}={}) {
  if(typeof locator!=="string" || !path.isAbsolute(locator) || !/^[a-f0-9]{32}\.json$/.test(path.basename(locator)))
    throw new Error("Scoped Pi handoff locator unavailable");
  const directory=path.dirname(locator), info=fs.lstatSync(directory);
  // Windows uses the per-user profile ACL; POSIX mode bits and O_NOFOLLOW are unavailable.
  const windows=process.platform==="win32";
  if(!info.isDirectory() || !windows&&(info.mode&0o077) || process.getuid&&info.uid!==process.getuid() || fs.realpathSync(directory)!==directory)
    throw new Error("Scoped Pi handoff directory unavailable");
  const deadline=performance.now()+timeoutMs;
  while(!fs.existsSync(locator) && performance.now()<deadline) await new Promise(resolve=>setTimeout(resolve,10));
  let descriptor;
  const fd=fs.openSync(locator,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW|fs.constants.O_NONBLOCK);
  try {
    const opened=fs.fstatSync(fd), before=fs.lstatSync(locator);
    if(!opened.isFile() || !before.isFile() || !windows&&(!fs.constants.O_NOFOLLOW || opened.mode&0o077) ||
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
  const connection=await connectAttestor({...descriptor,deadline:Math.min(deadline,performance.now()+60000),report:fileDiagnostic(descriptor.bridgeDir),onLoss:()=>{
    // A launched SDK cannot reuse authority from a dead runner incarnation.
    process.stderr.write("Scoped Pi authority transport lost; restart the Pi session to establish fresh authority.\n");
    process.exit(1);
  }});
  return Object.freeze({connection,bridgeDir:descriptor.bridgeDir,statePath:descriptor.statePath});
}

module.exports={createScopedClient,connectAttestor,connectFromHandoff,publicValue,fileDiagnostic,bridgeFailure};
