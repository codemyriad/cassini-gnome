const {test}=require('node:test');
const assert=require('node:assert/strict');
const {setup}=require('./support/app-control-harness.cjs');
const discover=(frame,generation='0',document='live-document')=>frame.onMessage.emit({type:'discovery',sourceCount:1,heard:true,documentGeneration:document,callGeneration:generation});
async function savedCall(run){
 const old=await setup(1,{},['source-outcomes-v1'],null,'old-worker-');
 try{
  discover(old.pages[0]);await old.command({type:'command',requestId:'start',action:'start',tabId:2,mode:'tracks',independent:true});
  const row=(await old.api.meetings()).meetings[0];
  const stored=JSON.parse(JSON.stringify(await old.chrome.storage.session.get()));
  await run({row,stored});
 }finally{await old.api.stop();old.close()}
}

test('worker restart preserves the live call identity and hook baseline before inventory',async()=>savedCall(async({row,stored})=>{
 const fresh=await setup(1,stored,[],null,'new-worker-');
 try{
  discover(fresh.pages[0]);const current=(await fresh.api.meetings()).meetings[0];
  assert.equal(current.callGeneration,row.callGeneration);assert.equal(current.documentGeneration,row.documentGeneration);
  assert.equal(current.url,row.url);assert.equal(current.pageState,'present');assert.equal(current.heard,true);
  assert.equal(fresh.audio.length,0,'restoring metadata must not start audio');
 }finally{fresh.close()}
}));

test('a fresh reload document at the same address keeps the call identity',async()=>savedCall(async({row,stored})=>{
 const fresh=await setup(1,stored,[],null,'new-worker-');
 try{discover(fresh.pages[0],'0','reloaded-document');const current=(await fresh.api.meetings()).meetings[0];assert.equal(current.callGeneration,row.callGeneration);assert.equal(current.documentGeneration,'reloaded-document')}
 finally{fresh.close()}
}));

for(const order of ['browser-first','hook-first'])test(`a real boundary during worker recovery rotates once (${order})`,async()=>savedCall(async({row,stored})=>{
 const fresh=await setup(0,stored,[],null,'new-worker-',({frame,chrome,tabs})=>{
  const early=frame(2,0);tabs.get(2).url='https://meet.example/new-call';
  if(order==='browser-first'){chrome.tabs.onUpdated.emit(2,{url:'https://meet.example/new-call'});discover(early,'1')}
  else{discover(early,'1');chrome.tabs.onUpdated.emit(2,{url:'https://meet.example/new-call'})}
 });
 try{
  const current=(await fresh.api.meetings()).meetings[0];assert.notEqual(current.callGeneration,row.callGeneration);
  const generation=current.callGeneration;assert.equal(current.sourceCount,1);assert.equal(current.heard,true);assert.equal(current.pageState,'present');
  const early=fresh.pages[0];discover(early,'1');fresh.chrome.tabs.onUpdated.emit(2,{url:'https://meet.example/new-call'});
  assert.equal((await fresh.api.meetings()).meetings[0].callGeneration,generation);
  discover(early,'0');assert.equal((await fresh.api.meetings()).meetings[0].callGeneration,generation);
  assert.equal((await fresh.api.meetings()).meetings[0].heard,true);
 }finally{fresh.close()}
}));

test('call identities are persisted after Stop and malformed saved identities are ignored',async()=>savedCall(async({row,stored})=>{
 stored.cassiniCallGenerations.push({tabId:3,generation:{bad:true},url:'https://meet.example/bad'});
 const fresh=await setup(1,stored,[],null,'new-worker-');
 try{discover(fresh.pages[0]);assert.equal((await fresh.api.meetings()).meetings[0].callGeneration,row.callGeneration);assert.equal(typeof fresh.api.callInfo(3).generation,'string')}
 finally{fresh.close()}
}));

test('browser-first boundary fences the persisted baseline before the first returning hook snapshot',async()=>savedCall(async({row,stored})=>{
 const fresh=await setup(0,stored,[],null,'new-worker-');
 try{
  fresh.tabs.get(2).url='https://meet.example/B';fresh.chrome.tabs.onUpdated.emit(2,{url:'https://meet.example/B'});
  const once=fresh.api.callInfo(2).generation;assert.notEqual(once,row.callGeneration);
  discover(fresh.pages[0],'0');assert.equal(fresh.api.callInfo(2).generation,once);
  assert.equal((await fresh.api.meetings()).meetings.length,0,'queued old evidence cannot vote or permit Record');
  const refused=await fresh.command({type:'command',requestId:'old-snapshot',action:'start',tabId:2,mode:'tracks',independent:true,callGeneration:once});
  assert.equal(refused.ok,false);assert.equal(fresh.audio.length,0);
  discover(fresh.pages[0],'1');const current=(await fresh.api.meetings()).meetings[0];
  assert.equal(current.callGeneration,once);assert.equal(current.heard,true);assert.equal(current.sourceCount,1);
 }finally{fresh.close()}
}));

test('a long address is fingerprinted consistently and retains its identity after restart',async()=>{
 const url='https://meet.example/call?synthetic='+ 'a'.repeat(2132);
 const old=await setup(1,{},['source-outcomes-v1'],null,'old-worker-');let fresh;
 try{
  old.tabs.get(2).url=url;discover(old.pages[0]);
  await old.command({type:'command',requestId:'start',action:'start',tabId:2,mode:'tracks',independent:true});
  const generation=old.api.callInfo(2).generation,stored=JSON.parse(JSON.stringify(await old.chrome.storage.session.get()));
  const record=stored.cassiniCallGenerations.find(r=>r.tabId===2);assert.match(record.addressHash,/^[0-9a-f]{64}$/);
  assert.equal('url' in record,false);assert.ok(JSON.stringify(record).length<1000);
  fresh=await setup(1,stored,[],null,'new-worker-',({tabs})=>tabs.get(2).url=url);
  discover(fresh.pages[0]);assert.equal((await fresh.api.meetings()).meetings[0].callGeneration,generation);
 }finally{await old.api.stop();old.close();fresh?.close()}
});
