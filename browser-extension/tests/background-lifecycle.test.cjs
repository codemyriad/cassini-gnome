const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
function event() { const listeners=[]; return {addListener(fn){listeners.push(fn)},emit(...args){listeners.forEach(fn=>fn(...args))}}; }
async function setup({delay,previous,grantError,grantDelay=0,startDelay=0,storageDelay=0,createDelay=0,storageGetError=false}={}) {
  const connect=event(), messages=event(), stored=previous?{cassiniActive:previous}:{};
  let nativeConnections=0,badge='REC',phase='',offscreen=false; const ports=[];
  const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const chrome={runtime:{id:'test',onConnect:connect,onMessage:messages,getURL:name=>'chrome-extension://test/'+name,getContexts:async()=>offscreen?[{contextType:'OFFSCREEN_DOCUMENT'}]:[],sendMessage:async()=>({}),connectNative(){
    nativeConnections++; const port={onMessage:event(),onDisconnect:event(),closed:false,disconnect(){this.closed=true;this.onDisconnect.emit()},postMessage(packet){
      if(packet.type==='start')phase='start-ack';
      setTimeout(()=>this.onMessage.emit(packet.type==='start'?{type:'started',path:'/Recording-test'}:packet.type==='stop'?{type:'stopped',path:'/Recording-test'}:{type:'ack'}),packet.type==='start'?startDelay:0);
    }};ports.push(port);return port;
  }},storage:{session:{async get(){if(storageGetError)throw Error('storage unavailable');return {...stored}},async set(value){phase='storage-set';await wait(storageDelay);Object.assign(stored,value)},async remove(key){delete stored[key]}}},
  tabs:{query:async()=>[{id:1,url:'https://meeting.test',title:'Test'}],onRemoved:event(),onUpdated:event(),async sendMessage(){
    if(delay!==undefined)setTimeout(()=>connect.emit({name:'cassini-frame',sender:{id:'test',tab:{id:1}},onMessage:event(),onDisconnect:event(),postMessage(){}}),delay);
  }},tabCapture:{async getMediaStreamId(){phase='grant';await wait(grantDelay);if(grantError)throw Error('No invocation');return 'stream'}},
  offscreen:{async closeDocument(){offscreen=false},async createDocument(){phase='offscreen-create';await wait(createDelay);offscreen=true}},action:{async setBadgeText(value){badge=value.text},async setBadgeBackgroundColor(){}}};
  const context=vm.createContext({AppControl:class {start(){} publish(){} publishStatus(){}},chrome,setTimeout,clearTimeout,crypto:{randomUUID:()=> 'token'},queueMicrotask});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../background.js'),'utf8').replace(/^import .*\n/, '')+'\nglobalThis.api={start,stop,failure,status,recovery};',context);
  await context.api.recovery;
  return {api:context.api,stored,ports,get phase(){return phase},get offscreen(){return offscreen},get connections(){return nativeConnections},get badge(){return badge}};
}
test('300ms delayed relay is discovered before native host creation',async()=>{
 const app=await setup({delay:300});const started=Date.now();await app.api.start('tracks');
 assert.ok(Date.now()-started>=250);assert.equal(app.connections,1);assert.equal(app.stored.cassiniActive.path,'/Recording-test');
});
test('missing relay and missing tab invocation do not create a native session',async()=>{
 for(const options of [{},{grantError:true}]){const app=await setup(options);await assert.rejects(app.api.start(options.grantError?'tab':'tracks'));assert.equal(app.connections,0);}
});
test('worker recovery reports interruption with saved path and clears stale badge/state',async()=>{
 const app=await setup({previous:{path:'/Recording-saved',recording:true}});
 assert.equal(app.api.status().recording,false);assert.match(app.api.status().message,/interrupted/);
 assert.equal(app.api.status().path,'/Recording-saved');assert.equal(app.badge,'');assert.equal(app.stored.cassiniActive,undefined);
});

async function until(check) {
 const deadline=Date.now()+2000;
 while(!check()){assert.ok(Date.now()<deadline,'test phase did not arrive');await new Promise(resolve=>setTimeout(resolve,1));}
}
test('failure during discovery or tab grant creates no host and leaves no stale state',async()=>{
 for(const options of [{delay:300},{grantDelay:100}]){
  const app=await setup(options);const starting=app.api.start(options.delay?'tracks':'tab');
  setTimeout(()=>app.api.failure('Cancelled while starting'),10);
  await assert.rejects(starting);await app.api.stop();
  assert.equal(app.connections,0);assert.equal(app.stored.cassiniActive,undefined);assert.equal(app.api.status().recording,false);assert.equal(app.badge,'');
 }
});
test('failure at host ACK, storage persistence or offscreen creation joins startup before cleanup',async()=>{
 for(const [phase,options] of [['start-ack',{startDelay:100}],['storage-set',{storageDelay:100}],['offscreen-create',{createDelay:100}]]){
  const app=await setup(options);const starting=app.api.start('tab');
  await until(()=>app.phase===phase);app.api.failure('Cancelled while starting');
  await assert.rejects(starting);await app.api.stop();
  assert.equal(app.connections,1);assert.equal(app.ports[0].closed,true);assert.equal(app.stored.cassiniActive,undefined);assert.equal(app.offscreen,false);assert.equal(app.api.status().recording,false);assert.equal(app.badge,'');
 }
});
test('a storage read error does not make all popup status requests fail',async()=>{
 const app=await setup({storageGetError:true});assert.equal(app.api.status().recording,false);assert.match(app.api.status().message,/could not check/);assert.equal(app.badge,'');
});
