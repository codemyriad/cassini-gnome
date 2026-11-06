const {test}=require('node:test');
const assert=require('node:assert/strict');
const {setup,until}=require('./support/app-control-harness.cjs');
const start={type:'command',requestId:'start',action:'start',tabId:2,mode:'tracks',independent:true};
const stop={type:'command',requestId:'stop',action:'stop',tabId:2,independent:true};
const plain=value=>JSON.parse(JSON.stringify(value));
for(const missing of [null,'capture.js','pcm-worklet.js'])test('hello reports version, installation and file self-check: '+(missing||'healthy'),async()=>{
  const reads=[];
  const app=await setup(1,{},[],null,'diagnostic-',null,{fetch:async(url,options)=>{
    reads.push({url,options});return {ok:!url.endsWith('/'+missing),async text(){return '// English sample script';}};
  }});
  try {
    const hello=app.ports[0].sent[0];
    assert.equal(hello.extensionVersion,require('../manifest.json').version);
    assert.equal(hello.installType,'development');assert.equal(hello.filesOk,missing===null);
    assert.deepEqual(reads.map(r=>r.url).sort(),['chrome-extension://extension/capture.js','chrome-extension://extension/pcm-worklet.js']);
    assert.ok(reads.every(r=>r.options.cache==='no-store'));
    assert.equal(app.api.browserHealth().filesOk,hello.filesOk);
    // These are diagnostics. Readiness and capability-based recording admission
    // remain independent of version strings or a failed file self-check.
    assert.equal((await app.command(start)).ok,true);
    await app.command(stop);
    const next=await app.reconnect();
    assert.deepEqual(plain(next.sent[0]),plain(hello));assert.equal(reads.length,2,'control reconnect reuses the completed worker check');
  } finally {app.close()}
});
test('unavailable install metadata is unknown; empty or unreadable packaged scripts fail the file check',async()=>{
  const app=await setup(0);
  try {
    const runtime={getURL:name=>'extension://'+name};
    const metadata={async getSelf(){throw Error('Metadata unavailable')}};
    let result=await app.api.checkCompanion(runtime,metadata,async()=>({ok:true,async text(){return '// readable'}}));
    assert.equal(result.installType,'unknown');assert.equal(result.filesOk,true);
    for(const fetch of [async()=>({ok:true,async text(){return ''}}),async()=>{throw Error('Missing packaged script')}]) {
      result=await app.api.checkCompanion(runtime,metadata,fetch);
      assert.equal(result.filesOk,false);
    }
  } finally {app.close()}
});
test('the self-check deadline aborts stuck file reads and never blocks hello indefinitely',async()=>{
  const app=await setup(0),signals=[],timers=new Map();let id=0;
  try {
    const checking=app.api.checkCompanion({getURL:name=>'extension://'+name},{async getSelf(){return {installType:'normal'}}},
      (_url,options)=>{signals.push(options.signal);return new Promise(()=>{})},
      {later(fn,ms){const key=++id;timers.set(key,{fn,ms});return key},cancel:key=>timers.delete(key)});
    await until(()=>signals.length===2);
    assert.equal([...timers.values()][0].ms,2500);
    await new Promise(resolve=>setImmediate(()=>{[...timers.values()][0].fn();resolve()}));
    const result=await checking;
    assert.equal(result.filesOk,false);assert.equal(result.installType,'normal');
    assert.ok(signals.every(signal=>signal.aborted));assert.equal(timers.size,0);
  } finally {app.close()}
});
for(const [label,cause,trigger] of [
  ['frame error','frameError',app=>app.pages[0].onMessage.emit({type:'error',cause:'nativeHostError'})],
  ['frame close','frameClosed',app=>app.pages[0].onDisconnect.emit()],
  ['tab close','tabClosed',app=>app.chrome.tabs.onRemoved.emit(2)],
  ['native error','nativeHostError',app=>app.audio[0].onMessage.emit({type:'error',scope:'session',code:'io',message:'Generated writer error'})],
  ['unsolicited frame stop','lifecycle',app=>app.pages[0].onMessage.emit({type:'stopped'})]
])test('lost Stop identifies '+label,async()=>{
  const app=await setup(1);
  try {
    assert.equal((await app.command(start)).ok,true);trigger(app);
    await until(()=>app.audio[0].closed);
    const packet=app.audio[0].sent.find(p=>p.type==='stop');
    assert.equal(packet.failureReason,'lost');assert.equal(packet.cause,cause);assert.ok(packet.problem);
    assert.equal(app.audio.length,1);
  } finally {app.close()}
});
test('the first terminal loss cause survives later failures and is separate from earlier source warnings',async()=>{
  const app=await setup(1);
  try {
    await app.command(start);
    app.pages[0].onMessage.emit({type:'warning',message:'Generated partial source warning'});
    app.pages[0].onMessage.emit({type:'error'});
    app.audio[0].onMessage.emit({type:'error',scope:'session',code:'io',message:'Generated later writer error'});
    await until(()=>app.audio[0].closed);
    const packet=app.audio[0].sent.find(p=>p.type==='stop');
    assert.equal(packet.cause,'frameError');
    assert.equal(packet.failureReason,'lost');
  } finally {app.close()}
});
test('an unexpected post-acknowledgement startup failure is identified as startupError',async()=>{
  const app=await setup(1);
  try {
    const post=app.pages[0].postMessage;
    app.pages[0].postMessage=function(message){if(message.type==='start')throw Error('Generated startup error');return post.call(this,message)};
    assert.equal((await app.command(start)).ok,false);
    const packet=app.audio[0].sent.find(p=>p.type==='stop');
    assert.equal(packet.failureReason,'lost');assert.equal(packet.cause,'startupError');
  } finally {app.close()}
});
test('native transport closure has its own cause even when the closed pipe cannot receive a Stop',async()=>{
  const app=await setup(0),causes=[];
  try {
    const host=new app.api.NativeHost((_message,cause)=>causes.push(cause));
    app.audio[0].onDisconnect.emit();
    assert.deepEqual(causes,['transportClosed']);host.close();
  } finally {app.close()}
});
test('a user Stop and a source warning add neither failureReason nor cause',async()=>{
  const app=await setup(1);
  try {
    await app.command(start);
    app.pages[0].onMessage.emit({type:'warning'});
    assert.equal((await app.command(stop)).ok,true);
    const packet=app.audio[0].sent.find(p=>p.type==='stop');
    assert.equal(packet.failureReason,undefined);assert.equal(packet.cause,undefined);
  } finally {app.close()}
});

test('stalled install metadata cannot turn readable files into a false missing-file diagnostic',async()=>{
  const app=await setup(0);let deadline;
  try {
    const checking=app.api.checkCompanion({getURL:name=>'extension://'+name},{getSelf:()=>new Promise(()=>{})},
      async()=>({ok:true,async text(){return '// Readable script'}}),
      {later(fn){deadline=fn;return 1},cancel(){}});
    await new Promise(resolve=>setImmediate(()=>{deadline();resolve()}));
    const result=await checking;
    assert.equal(result.installType,'unknown');assert.equal(result.filesOk,true);
  } finally {app.close()}
});
