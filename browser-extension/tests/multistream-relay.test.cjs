const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const vm=require('node:vm');
const {spawn}=require('node:child_process');
const {once}=require('node:events');
const {setup,event}=require('./support/app-control-harness.cjs');
test('seventy sources cross the production relay and worker into a framed native writer',async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'cassini-source-ledger-'));
 const child=spawn('python3',[process.env.CASSINI_TEST_NATIVE_HOST_FIXTURE || path.resolve(__dirname,'../../tests/browser_host_fixture.py'),root],{stdio:['pipe','pipe','pipe']});
 const exited=once(child,'exit');let inputError=null;let stderr='';child.stderr.on('data',b=>stderr+=b);let buffered=Buffer.alloc(0);
 const native={sent:[],closed:false,onMessage:event(),onDisconnect:event(),postMessage(p){if(this.closed)throw Error('Native fixture port is disconnected');this.sent.push(p);const b=Buffer.from(JSON.stringify(p)),header=Buffer.alloc(4);header.writeUInt32LE(b.length);child.stdin.write(Buffer.concat([header,b]))},disconnect(){if(!this.closed){this.closed=true;child.stdin.end();this.onDisconnect.emit()}}};
 // The production NativeHost already bounds each response by a failure-only
 // watchdog. A child lifetime timer can kill a healthy 70-request exchange.
 child.stdin.on('error',error=>{inputError=error;native.disconnect()});
 child.once('exit',()=>native.disconnect());
 async function bounded(promise,label){
  let timer;
  try{return await Promise.race([promise,new Promise((_,reject)=>{
   timer=setTimeout(()=>reject(Error('Missing fixture completion: '+label)),10000);
  })])}finally{clearTimeout(timer)}
 }
 child.stdout.on('data',b=>{buffered=Buffer.concat([buffered,b]);while(buffered.length>=4&&buffered.length>=4+buffered.readUInt32LE()){const n=buffered.readUInt32LE();const packet=JSON.parse(buffered.subarray(4,4+n));buffered=buffered.subarray(4+n);native.onMessage.emit(packet)}});
 let app;
 try{
  app=await setup(0,{},['source-outcomes-v1'],()=>native);
  const frame=app.frame(2,64,1),endpoint={onMessage:event(),onDisconnect:event(),postMessage:p=>frame.onMessage.emit(p)};
  frame.postMessage=p=>{frame.sent.push(p);endpoint.onMessage.emit(p)};
  const acknowledgments=new Map(),listeners=new Map(),controls=[],window={addEventListener:(name,fn)=>listeners.set(name,fn),postMessage:p=>{controls.push(p);if(p.type==='ack')acknowledgments.get(p.request)?.(p);if(p.type==='stop')queueMicrotask(()=>page({type:'stopped',token:p.token}))}};
  const page=p=>listeners.get('message')({isTrusted:true,source:window,data:{channel:'cassini-page',...p}});
  vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,'../relay.js'),'utf8'),{window,chrome:{runtime:{connect:()=>endpoint,onMessage:event(),getURL:p=>p}}});
  const results=new Map(),port=app.api.appControl.port,postResult=port.postMessage;
  port.postMessage=function(p){postResult.call(this,p);if(p.type==='result')results.get(p.requestId)?.(p)};
  async function command(requestId,action){
   const result=new Promise(resolve=>{results.set(requestId,resolve);port.onMessage.emit({type:'command',requestId,action,tabId:2,mode:'tracks',independent:true})});
   try{return await bounded(result,'command '+requestId)}finally{results.delete(requestId)}
  }
  assert.equal((await command('start','start')).ok,true);
  const token=controls.find(p=>p.type==='start').token;
  async function packet(request,p){
   const ack=new Promise(resolve=>{acknowledgments.set(request,resolve);page({type:'packet',token,request,packet:p})});
   try{return await bounded(ack,'packet '+request)}finally{acknowledgments.delete(request)}
  }
  const sources=Array.from({length:70},(_,i)=>({sourceTrackId:`receiver-${i}`,kind:'remote',label:`Voice ${i}`,...(i%2===0?{labelSource:"dom"}:{}),failed:false}));
  assert.equal((await packet(1,{type:'sources',discoveredSources:sources})).error,undefined);
  for(let i=0;i<70;i++){
   const ack=await packet(i+2,{type:'track',id:`span-${i}`,sourceTrackId:sources[i].sourceTrackId,kind:'remote',label:sources[i].label,...(sources[i].labelSource?{labelSource:sources[i].labelSource}:{}),sampleRate:48000,channels:1});
   assert.equal(Boolean(ack.error),i>=64);if(i>=64)assert.equal(ack.scope,'source');
  }
  assert.equal((await command('stop','stop')).ok,true);
  const directories=fs.readdirSync(root);assert.equal(directories.length,1);
  const folder=path.join(root,directories[0]),session=JSON.parse(fs.readFileSync(path.join(folder,'session.json')));
  assert.equal(session.tracks.length,64);assert.equal(fs.readdirSync(folder).filter(p=>p.endsWith('.wav')).length,64);
  assert.equal(session.captureOutcome.sources.length,70);
  assert.equal(session.tracks[0].labelSource,'dom');assert.equal(session.tracks[1].labelSource,'browser');
  assert.ok(session.captureOutcome.sources.filter((s,i)=>i%2===0).every(s=>s.labelSource==='dom'),'DOM provenance survives source batches, failures and Stop');
  assert.deepEqual(session.captureOutcome.sources.filter(s=>s.captureOutcome==='failed').map(s=>s.sourceTrackId),sources.slice(64).map(s=>s.sourceTrackId));
  const [code]=await bounded(exited,'exit after Stop/EOF');assert.equal(code,0,stderr);assert.equal(inputError,null);
 }finally{app?.close();native.disconnect();if(child.exitCode===null){child.kill('SIGKILL');await exited}fs.rmSync(root,{recursive:true,force:true})}
});
