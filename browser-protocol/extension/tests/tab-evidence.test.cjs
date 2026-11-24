const {test}=require('node:test');
const assert=require('node:assert/strict');

test('copied diagnostics allow only enums, versions and counts even with poisoned status fields',async()=>{
  const {copyDiagnostics}=await import('../tab-evidence.mjs');
  const poison='https://private.invalid/person secret device SDP contact /Users/private';
  const result={tabId:7,recording:true,tracks:2,failedSources:[{label:poison,path:poison}],url:poison,
    browserHealth:{extensionVersion:'0.2.26',nativeControl:'connected',filesOk:true,extensionInstance:poison,
      browserInstance:poison,tabs:[{tabId:7,state:'ready',url:poison}]},
    tabEvidence:{tabId:7,browserDocumentId:poison,sourceDocumentGeneration:poison,callGeneration:poison,
      callEvidence:{state:'active',reason:'activeCall',adapter:{name:poison}},
      captureCapability:{kind:'separated',assurance:'observed',detail:poison}}};
  const report=copyDiagnostics(result,7);
  assert.equal(report.callEvidence.state,'active');assert.equal(report.captureCapability.kind,'separated');
  assert.equal(report.execution.tracks,2);assert.equal(report.execution.failedSources,1);
  assert.doesNotMatch(JSON.stringify(report),/private|secret|SDP|contact|Users|Document|Generation|Instance|tabId/);
  result.browserHealth.extensionVersion=poison;result.browserHealth.nativeControl=poison;
  result.tabEvidence.callEvidence.state=poison;result.tabEvidence.callEvidence.reason=poison;
  result.tabEvidence.captureCapability.kind=poison;result.tabEvidence.captureCapability.assurance=poison;
  result.tracks=poison;
  const refused=copyDiagnostics(result,7);
  assert.equal(refused.extensionVersion,null);assert.equal(refused.callEvidence.state,'unknown');
  assert.equal(refused.captureCapability.kind,'unknown');assert.equal(refused.execution.tracks,0);
  assert.doesNotMatch(JSON.stringify(refused),/private|secret/);
  assert.equal(copyDiagnostics(undefined,undefined).callEvidence.state,'unknown');
  assert.equal(copyDiagnostics({browserHealth:{tabs:'not an inventory'}},7).transport.page,'unavailable');
});

test('tab projection never borrows another tab evidence or capture and distinguishes transport',async()=>{
  const {tabEvidenceView}=await import('../tab-evidence.mjs');
  const result={tabId:3,recording:true,starting:true,tracks:8,
    browserHealth:{nativeControl:'connected',tabs:[{tabId:7,state:'reloadRequired'}]},
    tabEvidence:{tabId:3,callEvidence:{state:'active',reason:'activeCall'},captureCapability:{kind:'separated',assurance:'observed'}}};
  const view=tabEvidenceView(result,7);
  assert.equal(view.callEvidence.state,'unknown');assert.equal(view.captureCapability.kind,'unknown');
  assert.equal(view.transport.app,'connected');assert.equal(view.transport.page,'reloadRequired');
  assert.equal(view.execution.recording,false);assert.equal(view.execution.starting,false);assert.equal(view.execution.tracks,0);
});

test('healthy transport without source media stays distinct from unavailable capture and unknown admission',async()=>{
  const {tabEvidenceView}=await import('../tab-evidence.mjs');
  const view=tabEvidenceView({browserHealth:{nativeControl:'connected',tabs:[]},tabEvidence:{tabId:7,
    transport:{page:'ready'},callEvidence:{state:'unknown',reason:'unverified'},captureCapability:{kind:'unavailable',assurance:'observed'}}},7);
  assert.equal(view.transport.page,'ready');assert.equal(view.callEvidence.state,'unknown');
  assert.equal(view.captureCapability.kind,'unavailable');assert.equal(view.execution.recording,false);
});

test('documentStart is a strict boolean-or-null fact in the content-free projection',async()=>{
  const {tabEvidenceView}=await import('../tab-evidence.mjs');
  const view=value=>tabEvidenceView({browserHealth:{tabs:[]},tabEvidence:{tabId:3,transport:{page:'ready',documentStart:value}}},3).transport.documentStart;
  assert.equal(view(false),false);assert.equal(view(true),true);
  for(const hostile of ['false',0,1,{},undefined,null])assert.equal(view(hostile),null);
});

test('connection details carry only bounded flags, counts and enums about the worker view of a tab',async()=>{
  const {tabEvidenceView,copyDiagnostics}=await import('../tab-evidence.mjs');
  const detail={frames:2,main:true,obsolete:false,relayAlive:true,hookReady:false,checking:true,sources:3,nativeVerified:false,pageState:'present',repair:'none'};
  const view=raw=>tabEvidenceView({browserHealth:{tabs:[]},tabEvidence:{tabId:3,transport:{page:'unavailable',detail:raw}}},3).transport.detail;
  assert.equal(JSON.stringify(view(detail)),JSON.stringify(detail));
  assert.equal(view(undefined),null);assert.equal(view('x'),null);
  const hostile=view({frames:1e9,main:'yes',obsolete:1,relayAlive:{},hookReady:null,checking:'true',sources:-1,nativeVerified:[],pageState:'https://private.invalid/x',repair:'<img>',url:'https://private.invalid',title:'Secret meeting'});
  assert.equal(JSON.stringify(hostile),JSON.stringify({frames:0,main:null,obsolete:null,relayAlive:null,hookReady:null,checking:null,sources:null,nativeVerified:null,pageState:null,repair:null}));
  assert.doesNotMatch(JSON.stringify(copyDiagnostics({browserHealth:{tabs:[]},tabEvidence:{tabId:3,transport:{page:'ready',detail:{url:'https://private.invalid',title:'Secret'}}}},3)),/private|Secret/);
});
