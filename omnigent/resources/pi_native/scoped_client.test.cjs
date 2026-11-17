const test=require("node:test"),assert=require("node:assert/strict"),crypto=require("node:crypto");
const {createScopedClient,publicValue,connectAttestor,fileDiagnostic,connectFromHandoff}=require("./scoped_client.cjs");
const receipt="pi_receipt_"+"r".repeat(43), scope={scope_id:"scope",installation_id:"install",runner_epoch:"runner",launch_epoch:"launch"};
const event={toolCallId:"A",toolName:"read",input:{path:"notes"}}, support={status:"attested_native"};
function fixture({hold=false,override={},state={status:"ready",revision:3}}={}) {
 let live=true,loss,release,starts=0;
 const frames=[];
 const transport={isCurrent:()=>live,onLoss:fn=>loss=fn,
  attest:async()=>({attested:true}),
  evaluate:async(e,revision)=>({result:"POLICY_ACTION_ALLOW",permission_tier_binding:{call_id:"pi:"+crypto.createHash("sha256").update("scope\0"+e.toolCallId).digest("hex"),revision,arguments_digest:"0".repeat(64),pi_origin_receipt:receipt}}),
  execute:async(body,ack)=>{frames.push(body);if(hold)release=()=>ack({executed:true});else ack({executed:true});},
  finish:async()=>({finished:true}),revoke:async()=>({revoked:true,scope_id:"scope",installation_id:"install"})};
 Object.assign(transport,override);
 const diagnostics=[];
 const client=createScopedClient({scope,transport,readState:()=>state,recheck:async()=>support,report:record=>diagnostics.push(record)});
 return {client,frames,diagnostics,lose(){live=false;loss();},release:()=>release?.(),start(){starts++;return {content:[{type:"text",text:"read"}],permission_tier_binding:{pi_origin_receipt:receipt}};},starts:()=>starts};
}
test("private receipts never enter public verdicts, tool results or client serialization",async()=>{
 const f=fixture();const verdict=await f.client.evaluate(event,{},support);
 assert.deepEqual(verdict,{block:false});
 const result=await f.client.execute(event,{},()=>f.start());
 assert.equal(f.starts(),1);assert.equal(JSON.stringify([verdict,result,f.client]).includes(receipt),false);
 assert.equal(f.frames[0].pi_origin_receipt,receipt);
 assert.equal(await f.client.finish(event),true);assert.equal(await f.client.finish(event),true);
});
test("loss or replacement after final ACK and before SDK entry seals despite unchanged tier",async()=>{
 for(const cause of ["loss","replacement"]){
  const f=fixture({hold:true});await f.client.evaluate(event,{},support);
  const result=f.client.execute(event,{},()=>f.start());
  await new Promise(resolve=>setImmediate(resolve));
  f.lose();f.release();assert.equal((await result).isError,true);assert.equal(f.starts(),0,cause);
  assert.equal((await f.client.evaluate(event,{},support)).block,true);
 }
});
test("unknown call, altered arguments and replay cannot start a tool",async()=>{
 const f=fixture();await f.client.evaluate(event,{},support);
 for(const changed of [{...event,toolCallId:"B"},{...event,input:{path:"other"}}])
  assert.equal((await f.client.execute(changed,{},()=>f.start())).isError,true);
 assert.equal(f.starts(),0);
 await f.client.execute(event,{},()=>f.start());
 assert.equal((await f.client.execute(event,{},()=>f.start())).isError,true);
 assert.equal(f.starts(),1);
});
test("tool exceptions and nested public values do not copy private authority",async()=>{
 const f=fixture();await f.client.evaluate(event,{},support);
 const result=await f.client.execute(event,{},()=>{throw new Error(receipt);});
 assert.equal(JSON.stringify(result).includes(receipt),false);
 assert.equal(JSON.stringify(publicValue({result:{text:receipt,pi_origin_receipt:receipt}})).includes(receipt),false);
});
test("untrusted endpoint postures fail before connection",async()=>{
 for(const endpoint of ["http://0.0.0.0:9000","http://example.test:9000","https://127.0.0.1:9000","http://127.0.0.1:9000/?next=remote"])
  await assert.rejects(connectAttestor({endpoint,credential:"fixture",brokerIncarnation:"epoch",launchEpoch:"launch"}));
});
test("bridge failures retain safe reasons and distinguish dispatch from delivery",async()=>{
 const missing=await fixture().client.bridge(event,0);
 assert.equal(missing.reason,"call_missing");
 assert.match(missing.error,/not dispatched/);
 const f=fixture({override:{rpc:async()=>{throw new Error(receipt);}}});
 await f.client.evaluate(event,{},support);
 const failed=await f.client.execute(event,{},()=>f.client.bridge(event,0));
 assert.equal(failed.reason,"rpc_delivery_failed");
 assert.match(failed.error,/may have run/);
 assert.equal(JSON.stringify(failed).includes(receipt),false);
 const g=fixture({override:{rpc:async()=>({error:receipt,reason:"rpc_refused"})}});
 await g.client.evaluate(event,{},support);
 const refused=await g.client.execute(event,{},()=>g.client.bridge(event,0));
 assert.equal(refused.reason,"rpc_refused");
 assert.match(refused.error,/not dispatched/);
 assert.equal(JSON.stringify(refused).includes(receipt),false);
});

test("in-flight replies survive a revision change while further dispatch remains fenced",async()=>{
 let deliver,calls=0;
 const state={status:"ready",revision:3};
 const f=fixture({state,override:{rpc:()=>{calls++;return new Promise(resolve=>deliver=resolve);}}});
 await f.client.evaluate(event,{},support);
 const pending=f.client.execute(event,{},()=>f.client.bridge(event,0));
 await new Promise(resolve=>setImmediate(resolve));
 state.revision=4;
 deliver({result:"completed"});
 assert.deepEqual(await pending,{result:"completed"});
 const denied=await f.client.bridge(event,0);
 assert.equal(denied.reason,"revision_changed");
 assert.equal(calls,1);
});

test("oversized HTTP and TCP replies report execution and preserve the size cap",async t=>{
 const http=require("node:http"),net=require("node:net");
 const large=JSON.stringify({result:"x".repeat(1024*1024+1)});
 let refusal=false;
 const server=http.createServer((req,res)=>{
  req.resume();
  if(req.url==="/pi/bootstrap/watch") {res.writeHead(200);res.write(JSON.stringify({incarnation:"epoch",launch_epoch:"launch",live:true,revoked_scopes:[]})+"\n");}
  else if(req.url==="/pi/rpc") {
   if(refusal){res.writeHead(403);res.end(JSON.stringify({error:receipt,reason:"rpc_refused"}));}
   else res.end(large);
  }
  else if(req.url==="/policies/evaluate") res.end(JSON.stringify({result:"POLICY_ACTION_ALLOW",permission_tier_binding:{call_id:"pi:"+crypto.createHash("sha256").update("scope\0A").digest("hex"),revision:3,arguments_digest:"0".repeat(64),pi_origin_receipt:receipt}}));
  else res.end(JSON.stringify({executed:true}));
 });
 const tcp=net.createServer(socket=>socket.once("data",chunk=>socket.end(JSON.stringify({id:JSON.parse(chunk).id,result:large})+"\n")));
 await Promise.all([new Promise(resolve=>server.listen(0,"127.0.0.1",resolve)),new Promise(resolve=>tcp.listen(0,"127.0.0.1",resolve))]);
 t.after(()=>{server.closeAllConnections();server.close();tcp.close();});
 const connection=await connectAttestor({endpoint:`http://127.0.0.1:${server.address().port}`,credential:"fixture",brokerIncarnation:"epoch",launchEpoch:"launch"});
 t.after(()=>connection.close());
 for(const port of [0,tcp.address().port]) {
  const client=connection.client({scope,credential:"fixture"},"handle",()=>({status:"ready",revision:3}),async()=>support);
  await client.evaluate(event,{},support);
  const result=await client.execute(event,{},()=>client.bridge(event,port));
  assert.equal(result.reason,"rpc_response_too_large");
  assert.match(result.error,/command ran/);
 }
 refusal=true;
 const client=connection.client({scope,credential:"fixture"},"handle",()=>({status:"ready",revision:3}),async()=>support);
 await client.evaluate(event,{},support);
 const denied=await client.execute(event,{},()=>client.bridge(event,0));
 assert.equal(denied.reason,"rpc_refused");
 assert.match(denied.error,/not dispatched/);
 assert.equal(JSON.stringify(denied).includes(receipt),false);
});
test("a sealed client records its first cause once and gives recovery guidance",async()=>{
 const f=fixture();f.lose();f.client.seal();
 const verdict=await f.client.evaluate(event,{},support);
 assert.equal(verdict.block,true);
 assert.match(verdict.reason,/transport_lost.*Restart the Pi session/);
 assert.deepEqual(f.diagnostics.filter(r=>r.event==="client_sealed"),[{event:"client_sealed",cause:"transport_lost"}]);
 assert.equal(f.diagnostics.at(-1).cause,"transport_lost");
});
test("state refusal and changed support are distinguishable without logging their values",async()=>{
 const state={status:"blocked",revision:3,private:receipt},f=fixture({state});
 assert.equal((await f.client.evaluate(event,{},support)).block,true);
 assert.equal(f.diagnostics.at(-1).cause,"state_not_ready");
 state.status="ready";
 await f.client.evaluate(event,{}, {status:"different",private:receipt});
 assert.equal((await f.client.execute(event,{},()=>f.start())).isError,true);
 assert.deepEqual(f.diagnostics.at(-1),{event:"client_sealed",cause:"support_changed"});
 assert.equal(f.starts(),0);
 assert.equal(JSON.stringify(f.diagnostics).includes(receipt),false);
});
test("continuity EOF records its cause before later denials and never logs a private frame",async t=>{
 const http=require("node:http"),records=[];let stream;
 const server=http.createServer((req,res)=>{
  req.resume();stream=res;res.writeHead(200);
  res.write(JSON.stringify({incarnation:"epoch",launch_epoch:"launch",live:true,revoked_scopes:[],private:receipt})+"\n");
 });
 await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
 t.after(()=>{server.closeAllConnections();server.close();});
 const connection=await connectAttestor({endpoint:`http://127.0.0.1:${server.address().port}`,
  credential:receipt,brokerIncarnation:"epoch",launchEpoch:"launch",report:r=>records.push(r)});
 t.after(()=>connection.close());
 const client=connection.client({scope,credential:receipt},receipt,()=>({status:"ready",revision:3}),async()=>support);
 stream.end();
 for(let i=0;i<100&&!records.some(r=>r.event==="continuity_lost");i++)await new Promise(resolve=>setTimeout(resolve,10));
 assert.equal((await client.evaluate(event,{},support)).block,true);
 assert.deepEqual(records.filter(r=>r.event==="continuity_lost"),[{event:"continuity_lost",cause:"watch_end"}]);
 assert.equal(records.at(-1).cause,"transport_lost");
 assert.equal(JSON.stringify(records).includes(receipt),false);
});
test("denial diagnostics distinguish state, attestation, policy and binding without private values",async()=>{
 for(const [options,branch,stage] of [
  [{state:{status:"blocked",revision:3}},"not_current","initial"],
  [{override:{attest:async()=>{throw new Error(receipt);}}},"transport_exception","attest"],
  [{override:{evaluate:async()=>{throw new Error(receipt);}}},"transport_exception","policy"],
  [{override:{evaluate:async()=>({result:"POLICY_ACTION_ALLOW",permission_tier_binding:{pi_origin_receipt:receipt}})}},"invalid_binding","policy"],
 ]) {
  const f=fixture(options);
  const verdict=await f.client.evaluate(event,{},support);
  assert.equal(verdict.block,true);assert.match(verdict.reason,/^Scoped Pi authority is unavailable/);
  const [record]=f.diagnostics;
  assert.equal(record.branch,branch);assert.equal(record.stage,stage);
  assert.ok(record.elapsed_ms>=0);
  assert.equal(JSON.stringify(record).includes(receipt),false);
  assert.deepEqual(Object.keys(record).sort(),branch==="not_current"?["branch","cause","elapsed_ms","event","stage"]:["branch","elapsed_ms","event","stage"]);
  assert.equal(f.starts(),0);
 }
});
test("a queued attestation timeout expires old calls and a new call still succeeds",async t=>{
 const http=require("node:http"),records=[];
 let attestRequests=0,recovered=false;
 const server=http.createServer((req,res)=>{
  if(req.url==="/pi/bootstrap/watch") {
   req.resume();
   res.writeHead(200,{"content-type":"application/x-ndjson"});
   res.write(JSON.stringify({incarnation:"epoch",launch_epoch:"launch",live:true,revoked_scopes:[]})+"\n");
   return;
  }
  let text="";req.on("data",chunk=>text+=chunk);
  req.on("end",()=>{
   const body=JSON.parse(text);
   if(req.url==="/pi/bootstrap") {
    attestRequests++;
    if(!recovered)return;
    res.end(JSON.stringify({attested:true}));
   } else if(req.url==="/policies/evaluate") {
    const id=body.event.data.call_id;
    res.end(JSON.stringify({result:"POLICY_ACTION_ALLOW",permission_tier_binding:{
     call_id:"pi:"+crypto.createHash("sha256").update("scope\0"+id).digest("hex"),
     revision:3,arguments_digest:"0".repeat(64),pi_origin_receipt:receipt}}));
   } else res.end(JSON.stringify({executed:true}));
  });
 });
 await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
 t.after(()=>{server.closeAllConnections();server.close();});
 const connection=await connectAttestor({
  endpoint:`http://127.0.0.1:${server.address().port}`,
  credential:"pi_attestor_"+"a".repeat(43),brokerIncarnation:"epoch",launchEpoch:"launch",
  report:record=>records.push(record),
 });
 t.after(()=>connection.close());
 const client=connection.client({scope,credential:"pi_scope_"+"s".repeat(43)},"private-handle",
  ()=>({status:"ready",revision:3}),async()=>support);
 const started=performance.now();
 const verdicts=await Promise.all([0,1,2].map(i=>client.evaluate({...event,toolCallId:String(i)},{},support)));
 assert.ok(verdicts.every(verdict=>verdict.block));
 assert.ok(performance.now()-started<68000,"queue wait must not multiply the deadline");
 await new Promise(resolve=>setTimeout(resolve,50));
 const request=records.find(record=>record.event==="request_failed");
 assert.equal(request.route,"/pi/bootstrap");
 assert.equal(request.outcome,"deadline");
 assert.ok(request.elapsed_ms>=64000);
 assert.equal(records.filter(record=>record.branch==="transport_exception"&&record.stage==="attest").length,3);
 assert.equal(attestRequests,1,"expired queued calls must not dispatch later");
 const output=JSON.stringify(records);
 for(const secret of [receipt,"private-handle","pi_attestor_"+"a".repeat(43),"pi_scope_"+"s".repeat(43)])
  assert.equal(output.includes(secret),false);
 recovered=true;
 const fresh={...event,toolCallId:"fresh"};
 assert.deepEqual(await client.evaluate(fresh,{},support),{block:false});
 let starts=0;
 const result={content:[{type:"text",text:"fresh result"}]};
 assert.deepEqual(await client.execute(fresh,{},()=>{starts++;return result;}),result);
 assert.equal(starts,1);
 assert.equal(attestRequests,3,"only the first stalled call and the fresh call's two attestations dispatch");
});
test("slow attestations, bind and revoke survive the old two second limit",async t=>{
 const http=require("node:http"),records=[],operations=[];
 const server=http.createServer((req,res)=>{
  if(req.url==="/pi/bootstrap/watch") {
   req.resume();res.writeHead(200);res.write(JSON.stringify({incarnation:"epoch",launch_epoch:"launch",live:true,revoked_scopes:[]})+"\n");return;
  }
  let text="";req.on("data",chunk=>text+=chunk);
  req.on("end",()=>{
   const body=JSON.parse(text);
   if(req.url==="/pi/bootstrap") {
    operations.push(body.op);
    setTimeout(()=>res.end(JSON.stringify(body.op==="bind"?{bound:true}:body.op==="revoke"?{revoked:true,scope_id:scope.scope_id,installation_id:scope.installation_id}:{attested:true})),2200);
   } else {
    const id=body.event.data.call_id;
    res.end(JSON.stringify({result:"POLICY_ACTION_ALLOW",permission_tier_binding:{
     call_id:"pi:"+crypto.createHash("sha256").update("scope\0"+id).digest("hex"),
     revision:3,arguments_digest:"0".repeat(64),pi_origin_receipt:receipt}}));
   }
  });
 });
 await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
 t.after(()=>{server.closeAllConnections();server.close();});
 const connection=await connectAttestor({endpoint:`http://127.0.0.1:${server.address().port}`,
  credential:"fixture",brokerIncarnation:"epoch",launchEpoch:"launch",report:r=>records.push(r)});
 t.after(()=>connection.close());
 const client=connection.client({scope,credential:"fixture"},"fixture",()=>({status:"ready",revision:3}),async()=>support);
 const verdicts=await Promise.all([0,1,2].map(i=>client.evaluate({...event,toolCallId:String(i)},{},support)));
 assert.deepEqual(verdicts,[{block:false},{block:false},{block:false}]);
 assert.deepEqual(await connection.bind({handle:"fixture"}),{bound:true});
 await client.close();
 assert.deepEqual(operations,["attest","attest","attest","bind","revoke"]);
 assert.equal(records.filter(r=>r.outcome==="timeout").length,0);
});
test("diagnostics persist to the session file without writing into the TUI",t=>{
 const fs=require("node:fs"),path=require("node:path");
 const root=process.env.SCOPED_PI_TEST_TMP||require("node:os").tmpdir();
 const directory=fs.mkdtempSync(path.join(root,"diagnostic-"));
 t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 let writes=0;
 t.mock.method(console,"error",()=>writes++);
 fileDiagnostic(directory)({event:"evaluate_denied",branch:"not_current",stage:"initial",elapsed_ms:0});
 const lines=fs.readFileSync(path.join(directory,"scoped-pi.log"),"utf8");
 assert.match(lines,/^\[scoped-pi\] /);
 const record=JSON.parse(lines.slice("[scoped-pi] ".length));
 assert.equal(record.branch,"not_current");assert.ok(!Number.isNaN(Date.parse(record.time)));
 assert.equal(writes,0);
});
test("the production handoff wires denial diagnostics to its verified bridge directory",async t=>{
 const fs=require("node:fs"),path=require("node:path"),http=require("node:http");
 const root=process.env.SCOPED_PI_TEST_TMP||require("node:os").tmpdir();
 const bridgeDir=fs.realpathSync(fs.mkdtempSync(path.join(root,"handoff-")));
 t.after(()=>fs.rmSync(bridgeDir,{recursive:true,force:true}));
 const directory=path.join(bridgeDir,"pi-bootstrap");
 fs.mkdirSync(directory,{mode:0o700});
 const locator=path.join(directory,"0".repeat(32)+".json");
 const server=http.createServer((req,res)=>{
  req.resume();res.writeHead(200);
  res.write(JSON.stringify({incarnation:"epoch",launch_epoch:"launch",live:true,revoked_scopes:[]})+"\n");
 });
 await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
 t.after(()=>{server.closeAllConnections();server.close();});
 setTimeout(()=>fs.writeFileSync(locator,JSON.stringify({protocol:1,pid:process.pid,bridgeDir,
  statePath:path.join(bridgeDir,"permission-state.json"),credential:"pi_attestor_"+"a".repeat(43),
  endpoint:`http://127.0.0.1:${server.address().port}`,brokerIncarnation:"epoch",launchEpoch:"launch"}),{mode:0o600}),8200);
 const {connection}=await connectFromHandoff(locator);
 t.after(()=>connection.close());
 let writes=0;t.mock.method(console,"error",()=>writes++);
 const client=connection.client({scope,credential:"fixture"},"fixture",()=>({status:"blocked",revision:3}),async()=>support);
 assert.equal((await client.evaluate(event,{},support)).block,true);
 const log=fs.readFileSync(path.join(bridgeDir,"scoped-pi.log"),"utf8");
 assert.match(log,/"branch":"not_current"/);
 assert.equal(log.includes("pi_attestor_"+"a".repeat(43)),false);
 assert.equal(writes,0);
});

test("a delayed first continuity frame survives the old one second limit",async t=>{
 const http=require("node:http");
 const server=http.createServer((req,res)=>{
  req.resume();
  setTimeout(()=>{res.writeHead(200);res.write(JSON.stringify({incarnation:"epoch",launch_epoch:"launch",live:true,revoked_scopes:[]})+"\n");},1250);
 });
 await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
 t.after(()=>{server.closeAllConnections();server.close();});
 const connection=await connectAttestor({endpoint:`http://127.0.0.1:${server.address().port}`,credential:"fixture",brokerIncarnation:"epoch",launchEpoch:"launch"});
 connection.close();
});

test("revocation, epoch replacement and process loss cancel queued controls immediately",async t=>{
 const http=require("node:http");
 for(const loss of ["revoked","epoch","process"]) {
  let watch,trigger;
  const entered=new Promise(resolve=>trigger=resolve);
  const frame={incarnation:"epoch",launch_epoch:"launch",live:true,revoked_scopes:[]};
  const server=http.createServer((req,res)=>{
   req.resume();
   if(req.url==="/pi/bootstrap/watch") {watch=res;res.writeHead(200);res.write(JSON.stringify(frame)+"\n");}
   else trigger();
  });
  await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
  try {
   const connection=await connectAttestor({endpoint:`http://127.0.0.1:${server.address().port}`,credential:"fixture",brokerIncarnation:"epoch",launchEpoch:"launch"});
   const client=connection.client({scope,credential:"fixture"},"handle",()=>({status:"ready",revision:3}),async()=>support);
   const pending=[0,1].map(i=>client.evaluate({...event,toolCallId:`loss-${i}`},{},support));
   await entered;
   if(loss==="process")watch.end();
   else watch.write(JSON.stringify({...frame,...(loss==="epoch"?{launch_epoch:"replacement"}:{revoked_scopes:[scope.scope_id]})})+"\n");
   const verdicts=await Promise.race([Promise.all(pending),new Promise((_,reject)=>{const timer=setTimeout(()=>reject(new Error("loss did not cancel controls")),1000);timer.unref();})]);
   assert.ok(verdicts.every(value=>value.block));
   connection.close();
  } finally {server.closeAllConnections();server.close();}
 }
});
