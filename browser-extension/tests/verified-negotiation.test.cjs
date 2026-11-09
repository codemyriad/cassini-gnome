const {test}=require('node:test');const assert=require('node:assert/strict');
const {setup,until}=require('./support/app-control-harness.cjs');
const pair=['call-signals-v1','verified-auto-admission-v1'];
const plain=value=>JSON.parse(JSON.stringify(value));

test('verified shape requires implemented capability and acknowledgement, and revocation restores legacy',async()=>{
  const app=await setup(0);
  try{
    const control=app.api.appControl,port=control.port;
    control.capabilities.splice(control.capabilities.indexOf('verified-auto-admission-v1'),1);
    const seen=[];
    control.signalSnapshot=async options=>{seen.push(options.verified);return {browserInstance:'profile',tabs:[]}};
    const configure=capabilities=>port.onMessage.emit({type:'configure',capabilities});
    configure(pair);
    assert.deepEqual(plain(port.sent.filter(m=>m.type==='configured').at(-1).capabilities),['call-signals-v1']);
    await until(()=>seen.length);assert.equal(seen.at(-1),false);
    if(!control.capabilities.includes('verified-auto-admission-v1'))control.capabilities.push('verified-auto-admission-v1');
    configure(pair);
    const ack=port.sent.findLastIndex(m=>m.type==='configured');
    assert.deepEqual(plain(port.sent[ack].capabilities),pair);
    await until(()=>port.sent.some((m,index)=>index>ack&&m.type==='callSignals'));
    assert.equal(seen.at(-1),true);
    configure([]);
    assert.equal(control.verifiedConfigured,false);assert.equal(control.signalsConfigured,false);
    assert.deepEqual(plain(port.sent.filter(m=>m.type==='configured').at(-1).capabilities),[]);
  }finally{app.close()}
});

test('revocation fences a held alias inventory; duplicate/standalone features cannot enable it',async()=>{
  const app=await setup(0);let release;
  try{
    const control=app.api.appControl,port=control.port;
    await control.publishing;
    for(const capabilities of [['verified-auto-admission-v1'],['call-signals-v1','call-signals-v1']]){
      const before=port.sent.length;port.onMessage.emit({type:'configure',capabilities});
      assert.equal(port.sent.length,before);assert.equal(control.verifiedConfigured,false);
    }
    if(!control.capabilities.includes('verified-auto-admission-v1'))control.capabilities.push('verified-auto-admission-v1');
    const shapes=[];
    control.snapshot=async({verified})=>{
      shapes.push(verified);
      if(verified)await new Promise(resolve=>release=resolve);
      return [{type:'meetings',meetings:verified?[{browserDocumentId:'retired-alias'}]:[]}];
    };
    port.onMessage.emit({type:'configure',capabilities:pair});await until(()=>release);
    const before=port.sent.length;
    port.onMessage.emit({type:'configure',capabilities:[]});release();
    await until(()=>shapes.includes(false));await control.publishing;
    assert.equal(port.sent.slice(before).some(m=>m.type==='meetings'&&m.meetings.some(row=>row.browserDocumentId)),false);
    assert.equal(port.sent.filter(m=>m.type==='meetings').at(-1).meetings.length,0);
  }finally{release?.();app.close()}
});
