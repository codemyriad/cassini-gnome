// The popup's call verdict comes from the page alone: it must not depend on the Cassini app being connected,
// open or running, nor on the worker having already read the page before the popup opened.
const {test}=require('node:test');const assert=require('node:assert/strict');
const {setup,until}=require('./support/app-control-harness.cjs');
const rpc=(app,message)=>new Promise(resolve=>app.chrome.runtime.onMessage.emit({target:'background',type:'status',...message},
  {id:'extension',url:app.chrome.runtime.getURL('popup.html')},resolve));
const raw=()=>({version:2,cohortQualified:true,documentGeneration:'hook-A',hookGeneration:'0',revision:2,ageMs:0,
  receivedPeers:1,capturedSendingPeers:1,samePeerCount:1,receivedVersion:3,sentVersion:3,ended:false,sourceCount:3,heard:true});

async function coldTab({appPresent,gateFirstRead}){
  const app=await setup(0);let release;const gate=new Promise(resolve=>release=resolve);let reads=0;
  app.pages[0].onDisconnect.emit();
  if(!appPresent)app.ports[0].onDisconnect.emit();      // the Cassini app is not running: no control port
  app.chrome.scripting={async executeScript(request){
    if(request.world==='MAIN'){if(gateFirstRead&&reads++===0)await gate;return [{frameId:0,documentId:'chrome-A',result:raw()}]}
    return [{frameId:0,documentId:'chrome-A'}];
  }};
  const port=app.frame(2,1,0,'chrome-A');const hello=port.sent.find(m=>m.type==='relayHello');
  // Only the handshake: the worker has not yet learned the page's document/call generations.
  port.onMessage.emit({...hello,type:'relayReady',observerDocumentStart:true});
  return {app,release,port};
}

for(const appPresent of [true,false])test(`a call in progress is reported with the app ${appPresent?'connected':'absent'}`,async()=>{
  const {app,release}=await coldTab({appPresent,gateFirstRead:false});
  try{
    const result=await rpc(app,{tabId:2,fresh:true});
    assert.equal(result.tabEvidence.callEvidence.state,'active');
    assert.equal(result.tabEvidence.captureCapability.kind,'separated');
    assert.equal(result.browserHealth.nativeControl,appPresent?'connected':'disconnected');
  }finally{release();app.close()}
});

test('the popup-open check is not lost while the handshake read of a freshly started worker is still in flight',async()=>{
  const {app,release}=await coldTab({appPresent:false,gateFirstRead:true});
  try{
    await until(()=>true);
    const check=rpc(app,{tabId:2,fresh:true});
    await new Promise(resolve=>setTimeout(resolve,20));release();
    const result=await check;
    assert.equal(result.tabEvidence.callEvidence.state,'active','an active call must not show as unverified because the worker was cold');
  }finally{release();app.close()}
});

test('the local verdict does not wait for the profile identity used only to publish to the app',async()=>{
  const {app,release}=await coldTab({appPresent:false,gateFirstRead:false});
  try{
    app.api.genericProducer.identity=()=>null;           // identity lookup failed or timed out
    const result=await rpc(app,{tabId:2,fresh:true});
    assert.equal(result.tabEvidence.callEvidence.state,'active');
  }finally{release();app.close()}
});

test('a remembered active verdict is not shown as current once the tab has no meeting sources',async()=>{
  const {app,release,port}=await coldTab({appPresent:false,gateFirstRead:false});
  try{
    port.onMessage.emit({type:'discovery',sourceCount:3,heard:true,documentGeneration:'hook-A',callGeneration:'0'});
    assert.equal((await rpc(app,{tabId:2,fresh:true})).tabEvidence.callEvidence.state,'active');
    assert.equal((await rpc(app,{tabId:2})).tabEvidence.callEvidence.state,'active','a refresh keeps the checked verdict while sources remain');
    for(const frame of app.api.frames)if(frame.tabId===2){frame.sourceCount=0;frame.heard=false}   // the page reports no sources
    const later=await rpc(app,{tabId:2});
    assert.notEqual(later.tabEvidence.callEvidence.state,'active','the call ended: a refresh must not repeat "live audio is flowing"');
  }finally{release();app.close()}
});

const ended=()=>({...raw(),cohortQualified:false,receivedPeers:0,capturedSendingPeers:0,samePeerCount:0,ended:true,sourceCount:0,heard:false});
async function scriptedTab(value){
  const app=await setup(0);const page={value,reads:0};
  app.pages[0].onDisconnect.emit();app.ports[0].onDisconnect.emit();
  app.chrome.scripting={async executeScript(request){
    if(request.world==='MAIN'){page.reads++;return [{frameId:0,documentId:'chrome-A',result:page.value()}]}
    return [{frameId:0,documentId:'chrome-A'}];
  }};
  const port=app.frame(2,1,0,'chrome-A');const hello=port.sent.find(m=>m.type==='relayHello');
  port.onMessage.emit({...hello,type:'relayReady',observerDocumentStart:true});
  return {app,port,page};
}

test('a remembered "no call" is not repeated once the same document joins a new call',async()=>{
  const {app,port,page}=await scriptedTab(ended);
  try{
    assert.equal((await rpc(app,{tabId:2,fresh:true})).tabEvidence.callEvidence.state,'inactive');
    assert.equal((await rpc(app,{tabId:2})).tabEvidence.callEvidence.state,'inactive','a refresh keeps the checked verdict for the same call');
    // A rejoin or reconnect in the same document: the page's collector starts a new hook generation with live sources.
    page.value=()=>({...raw(),hookGeneration:'1'});const before=page.reads;
    port.onMessage.emit({type:'discovery',sourceCount:3,heard:true,documentGeneration:'hook-A',callGeneration:'1'});
    await until(()=>page.reads>before);await until(()=>true);
    const later=await rpc(app,{tabId:2});
    assert.equal(later.tabEvidence.transport.detail.sources,3);
    assert.notEqual(later.tabEvidence.callEvidence.state,'inactive','the popup must not say "No call in progress" during the new call');
  }finally{app.close()}
});

test('only the popup preview reads before binding; a publishing round reads an unlearned page once',async()=>{
  const {app,page}=await scriptedTab(()=>null);   // the page's collector is unavailable: generations stay unknown
  try{
    const unlearn=()=>{for(const frame of app.api.frames)if(frame.tabId===2){frame.documentGeneration=undefined;frame.callGeneration=undefined}};
    unlearn();app.api.genericProducer.identity=()=>'profile-1';
    let before=page.reads;
    await app.api.genericProducer.snapshot({verified:true,epoch:1,configurationEpoch:1});
    assert.equal(page.reads-before,1,'a publishing round must not read the page twice');
    unlearn();before=page.reads;
    await app.api.genericProducer.snapshot({verified:true,preview:true,tabId:2});
    assert.equal(page.reads-before,2,'the popup check still learns the generations before binding');
  }finally{app.close()}
});
