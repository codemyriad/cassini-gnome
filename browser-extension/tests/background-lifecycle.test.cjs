const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
function event() { const listeners=[]; return {addListener(fn){listeners.push(fn)},emit(...args){listeners.forEach(fn=>fn(...args))}}; }
function deferred() { let resolve; const promise=new Promise(done=>{resolve=done}); return {promise,resolve}; }
// Each boundary is observable before release; no elapsed time establishes order.
async function setup({hold,previous,grantError,storageGetError=false}={}) {
  const entered=new Map(), releases=new Map(), timers=new Map(), timerWaiters=new Map();
  let now=0, timerId=0;
  const signal=name=>{if(!entered.has(name))entered.set(name,deferred());return entered.get(name)};
  const boundary=async name=>{signal(name).resolve();if(hold===name){if(!releases.has(name))releases.set(name,deferred());await releases.get(name).promise}};
  const later=(fn,ms)=>{const id=++timerId;timers.set(id,{fn,ms});timerWaiters.get(ms)?.resolve();return id};
  const cancel=id=>timers.delete(id);
  async function scheduled(ms) {
    if(![...timers.values()].some(timer=>timer.ms===ms)) {
      const ready=deferred();timerWaiters.set(ms,ready);await ready.promise;timerWaiters.delete(ms);
    }
  }
  async function advance(ms) {
    await scheduled(ms);
    const [id,timer]=[...timers].find(([,value])=>value.ms===ms);
    timers.delete(id);now+=ms;timer.fn();
  }
  const clock=class extends Date { static now(){return now} };
  const connect=event(), messages=event(), stored=previous?{cassiniActive:previous}:{};
  let nativeConnections=0,badge='REC',offscreen=false; const ports=[];
  const chrome={runtime:{id:'test',onConnect:connect,onMessage:messages,getURL:name=>'chrome-extension://test/'+name,getContexts:async()=>offscreen?[{contextType:'OFFSCREEN_DOCUMENT'}]:[],sendMessage:async()=>({}),connectNative(){
    nativeConnections++; const port={onMessage:event(),onDisconnect:event(),closed:false,disconnect(){this.closed=true;this.onDisconnect.emit()},postMessage(packet){
      void (async()=>{if(packet.type==='start')await boundary('start-ack');
      this.onMessage.emit(packet.type==='start'?{type:'started',path:'/Recording-test'}:packet.type==='stop'?{type:'stopped',path:'/Recording-test'}:{type:'ack'});})();
    }};ports.push(port);return port;
  }},storage:{session:{async get(){if(storageGetError)throw Error('storage unavailable');return {...stored}},async set(value){if(value.cassiniActive){await boundary('storage-set')}Object.assign(stored,value)},async remove(key){delete stored[key]}}},
  tabs:{query:async()=>[{id:1,url:'https://meeting.test',title:'Test'}],onRemoved:event(),onUpdated:event(),async sendMessage(){
    signal('discovery').resolve();
  }},tabCapture:{async getMediaStreamId(){await boundary('grant');if(grantError)throw Error('No invocation');return 'stream'}},
  offscreen:{async closeDocument(){offscreen=false},async createDocument(){await boundary('offscreen-create');offscreen=true}},action:{async setBadgeText(value){badge=value.text},async setBadgeBackgroundColor(){}}};
  const context=vm.createContext({AppControl:class {start(){} publish(){} publishStatus(){}},chrome,TextEncoder,Date:clock,setTimeout:later,clearTimeout:cancel,crypto:{randomUUID:()=> 'token',subtle:require('node:crypto').webcrypto.subtle},queueMicrotask});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../call-signals.js'),'utf8').replace(/^export /gm,''),context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../background.js'),'utf8').replace(/^import .*\n/gm, '')+'\nglobalThis.api={start,stop,failure,status,recovery};',context);
  await context.api.recovery;
  return {api:context.api,stored,ports,advance,scheduled,entered:name=>signal(name).promise,release:name=>releases.get(name).resolve(),
    connectFrame(){connect.emit({name:'cassini-frame',sender:{id:'test',tab:{id:1}},onMessage:event(),onDisconnect:event(),postMessage(m){if(m.type==='relayHello')queueMicrotask(()=>{this.onMessage.emit({...m});this.onMessage.emit({...m,type:'relayReady'})})}})},get offscreen(){return offscreen},get connections(){return nativeConnections},get badge(){return badge}};
}
test('native host waits for relay discovery', {timeout:10000}, async()=>{
 const app=await setup();const starting=app.api.start('tracks');
 await app.entered('discovery');await app.scheduled(50);
 assert.equal(app.connections,0);
 app.connectFrame();await app.advance(50);await starting;
 assert.equal(app.connections,1);assert.equal(app.stored.cassiniActive.path,'/Recording-test');
});
test('missing relay and missing tab invocation do not create a native session', {timeout:10000}, async()=>{
 const app=await setup();const starting=app.api.start('tracks');const rejected=assert.rejects(starting);
 await app.entered('discovery');
 // Drive the product's discovery expiry with virtual time, not a real wait.
 for(let i=0;i<10;i++)await app.advance(50);
 await rejected;assert.equal(app.connections,0);
 const denied=await setup({grantError:true});await assert.rejects(denied.api.start('tab'));assert.equal(denied.connections,0);
});
test('worker recovery reports interruption with saved path and clears stale badge/state',{timeout:10000},async()=>{
 const app=await setup({previous:{path:'/Recording-saved',recording:true}});
 assert.equal(app.api.status().recording,false);assert.match(app.api.status().message,/interrupted/);
 assert.equal(app.api.status().path,'/Recording-saved');assert.equal(app.badge,'');assert.equal(app.stored.cassiniActive,undefined);
});

test('failure during discovery or tab grant creates no host and leaves no stale state', {timeout:10000}, async()=>{
 for(const phase of ['discovery','grant']){
  const app=await setup({hold:phase});const starting=app.api.start(phase==='discovery'?'tracks':'tab');
  const rejected=assert.rejects(starting);
  await app.entered(phase);if(phase==='discovery')await app.scheduled(50);assert.equal(app.connections,0);
  app.api.failure('Cancelled while starting');
  if(phase==='discovery')await app.advance(50);else app.release(phase);
  await rejected;await app.api.stop();
  assert.equal(app.connections,0);assert.equal(app.stored.cassiniActive,undefined);assert.equal(app.api.status().recording,false);assert.equal(app.badge,'');
 }
});
test('failure at host ACK, storage persistence or offscreen creation joins startup before cleanup', {timeout:10000}, async()=>{
 for(const phase of ['start-ack','storage-set','offscreen-create']){
  const app=await setup({hold:phase});const starting=app.api.start('tab');const rejected=assert.rejects(starting);
  await app.entered(phase);assert.equal(app.connections,1);assert.equal(app.ports[0].closed,false);
  app.api.failure('Cancelled while starting');
  assert.equal(app.ports[0].closed,false,'cleanup must join the held startup operation');
  app.release(phase);
  await rejected;await app.api.stop();
  assert.equal(app.connections,1);assert.equal(app.ports[0].closed,true);assert.equal(app.stored.cassiniActive,undefined);assert.equal(app.offscreen,false);assert.equal(app.api.status().recording,false);assert.equal(app.badge,'');
 }
});
test('a storage read error does not make all popup status requests fail',{timeout:10000},async()=>{
 const app=await setup({storageGetError:true});assert.equal(app.api.status().recording,false);assert.match(app.api.status().message,/could not check/);assert.equal(app.badge,'');
});
