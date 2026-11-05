const test=require("node:test"),assert=require("node:assert/strict"),crypto=require("node:crypto");
const {createScopedClient,publicValue,connectAttestor}=require("./scoped_client.cjs");
const receipt="pi_receipt_"+"r".repeat(43), scope={scope_id:"scope",installation_id:"install",runner_epoch:"runner",launch_epoch:"launch"};
const event={toolCallId:"A",toolName:"read",input:{path:"notes"}}, support={status:"attested_native"};
function fixture({hold=false}={}) {
 let live=true,loss,release,starts=0;
 const frames=[];
 const transport={isCurrent:()=>live,onLoss:fn=>loss=fn,
  attest:async()=>({attested:true}),
  evaluate:async(e,revision)=>({result:"POLICY_ACTION_ALLOW",permission_tier_binding:{call_id:"pi:"+crypto.createHash("sha256").update("scope\0"+e.toolCallId).digest("hex"),revision,arguments_digest:"0".repeat(64),pi_origin_receipt:receipt}}),
  execute:async(body,ack)=>{frames.push(body);if(hold)release=()=>ack({executed:true});else ack({executed:true});},
  finish:async()=>({finished:true}),revoke:async()=>({revoked:true,scope_id:"scope",installation_id:"install"})};
 const client=createScopedClient({scope,transport,readState:()=>({status:"ready",revision:3}),recheck:async()=>support});
 return {client,frames,lose(){live=false;loss();},release:()=>release?.(),start(){starts++;return {content:[{type:"text",text:"read"}],permission_tier_binding:{pi_origin_receipt:receipt}};},starts:()=>starts};
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
