const test = require('node:test');
const assert = require('node:assert/strict');
const state = import('../popup-state.mjs');
const tab = {id: 7, title: 'Generated meeting', url: 'https://example.test/call/private'};
const status = (tabs = [], extra = {}) => ({browserHealth: {nativeControl: 'connected', filesOk: true, tabs}, ...extra});

test('healthy source transport and unknown call evidence explain the listen-only limitation',async()=>{
  const {popupState}=await state;
  const result=status([{tabId:7,state:'ready'}],{tabEvidence:{tabId:7,
    callEvidence:{state:'unknown',reason:'unverified'},captureCapability:{kind:'separated',assurance:'observed'}}});
  const view=popupState(result,tab);
  assert.equal(view.title,'Interactive session is unverified');assert.match(view.description,/microphone is not sending.*listen-only.*record manually/);
  assert.equal(view.reloadTabId,null);assert.equal(view.connected,true);
  result.tabEvidence.callEvidence={state:'active',reason:'activeCall'};
  assert.equal(popupState(result,tab).title,'Interactive media session detected');
  result.recording=true;result.tabId=7;result.tracks=2;
  assert.equal(popupState(result,tab).title,'Browser capture is active','actual capture stays distinct from the evidence state');
});

test('a connected worker with no sources never claims that detection or recording works', async () => {
  const {popupState} = await state;
  const view = popupState(status(), tab);
  assert.equal(view.connected, true);
  assert.equal(view.title, 'No meeting sources detected');
  assert.match(view.description, /does not confirm that the page connection is working/);
  assert.equal(view.reloadTabId, null);
});
test('another tab needing repair cannot replace the current tab status or reload target', async () => {
  const {popupState} = await state;
  const result = status([{tabId: 4, state: 'reloadRequired'}, {tabId: 7, state: 'ready'}]);
  assert.equal(popupState(result, tab).title, 'Meeting sources detected');
  assert.equal(popupState(result, tab).reloadTabId, null);
  assert.equal(popupState(result, {...tab, id: 4}).reloadTabId, 4);
});
test('another tab recording is never presented as this tab recording', async () => {
  const {popupState} = await state;
  const view = popupState(status([], {recording: true, tabId: 8, tracks: 3}), tab);
  assert.equal(view.title, 'No meeting sources detected');
  assert.equal(view.note, 'Start recording asks the Cassini app on this computer to record this tab.');
  assert.equal(view.record.action, 'start', 'another tab recording never offers Stop here');
});
test('recording controls and stream identities stay separate from capture availability', async () => {
  const {popupState} = await state;
  const view = popupState(status([{tabId: 7, state: 'ready'}]), tab);
  assert.equal(view.title, 'Meeting sources detected');
  assert.doesNotMatch(view.title + view.description, /is recording|participants|voices/);
  const empty = popupState(status([], {recording: true, tabId: 7, tracks: 0}), tab);
  assert.match(empty.description, /No audio tracks/);
  assert.equal(empty.title, 'Waiting for browser audio');
  const capture = popupState(status([], {recording: true, tabId: 7, tracks: 2}), tab);
  assert.equal(capture.title, 'Browser capture is active');
  assert.match(capture.note, /2 browser audio tracks/);
});
test('an active capture suppresses reload even if page health suggests repair', async () => {
  const {popupState} = await state;
  const view = popupState(status([{tabId: 7, state: 'reloadRequired'}], {recording: true, stopping: true, tabId: 7, tracks: 1}), tab);
  assert.equal(view.title, 'Finishing browser capture');
  assert.equal(view.reloadTabId, null);
});
test('health checking does not become a reload instruction or successful detection', async () => {
  const {popupState} = await state;
  const view = popupState(status([{tabId: 7, state: 'checking'}]), tab);
  assert.equal(view.title, 'Checking the page connection…');
  assert.equal(view.reloadTabId, null);
});
test('file self-check remains diagnostic and missing health never implies readiness', async () => {
  const {popupState} = await state;
  const broken = status([{tabId: 7, state: 'ready'}]);broken.browserHealth.filesOk = false;
  assert.equal(popupState(broken, tab).title, 'Meeting sources detected');
  assert.equal(popupState(broken, tab).fileIssue, true);
  assert.equal(popupState({}, tab).title, 'Meeting detection is unavailable');
});
test('unsupported pages and absent tabs are described without diagnosing a broken meeting', async () => {
  const {popupState} = await state;
  for(const active of [undefined, {...tab, url:'chrome://extensions/'}, {...tab, url:'file:///meeting.html'}])
    assert.equal(popupState(status(), active).title, 'This page isn’t supported');
});
test('source failures remain visible during browser capture', async () => {
  const {popupState} = await state;
  const view = popupState(status([], {recording: true, tabId: 7, tracks: 2, failedSources:['source']}), tab);
  assert.equal(view.tone, 'warning');
  assert.match(view.description, /sources have a problem/);
});
test('meeting detection remains visible when the native connection is unavailable', async () => {
  const {popupState} = await state;
  const result = status([{tabId:7,state:'ready'}]);result.browserHealth.nativeControl = 'disconnected';
  const view = popupState(result,tab);
  assert.equal(view.connected,false);assert.equal(view.title,'Meeting sources detected');
  assert.match(view.description,/Open Cassini to connect/);
});

test('a pending Start is not active capture and cannot offer reload', async () => {
  const {popupState} = await state;
  const view = popupState(status([{tabId:7,state:'reloadRequired'}], {recording:false,starting:true,tabId:7}), tab);
  assert.equal(view.title,'Preparing browser capture…');
  assert.equal(view.reloadTabId,null);
  assert.match(view.note,/not yet confirmed/);
  const cancelling = popupState(status([{tabId:7,state:'reloadRequired'}], {recording:false,starting:true,stopping:true,tabId:7}), tab);
  assert.equal(cancelling.title,'Cancelling browser capture…');
  assert.equal(cancelling.reloadTabId,null);
});


test('post-reload guidance stays specific to the same tab until new evidence arrives',async()=>{
 const {popupState}=await state;
 const waiting=popupState(status(),tab,7);
 assert.equal(waiting.title,'Tab reloaded · rejoin your call');assert.equal(waiting.reloadTabId,null);
 for(const readiness of ['checking','ready','reloadRequired'])
  assert.notEqual(popupState(status([{tabId:7,state:readiness}]),tab,7).title,waiting.title);
 for(const extra of [{recording:true,tabId:7,tracks:1},{recording:false,starting:true,tabId:7}])
  assert.notEqual(popupState(status([],extra),tab,7).title,waiting.title);
 assert.notEqual(popupState(status(),{...tab,id:8},7).title,waiting.title);
 assert.equal(popupState({},tab,7).title,'Meeting detection is unavailable');
});

test('popup record control: Start only with an opted-in app and a ready tab, with a reason otherwise', async () => {
  const {popupState} = await state;
  const ready = extra => status([{tabId: 7, state: 'ready'}], {tabId: 7, recording: false, starting: false, appRecording: 'available', ...extra});
  assert.deepEqual(popupState(ready(), tab).record, {action: 'start', enabled: true, hint: '', label: 'Start recording'});
  const reasons = [
    [ready({appRecording: 'disconnected'}), /Open Cassini/],
    [ready({appRecording: 'unsupported'}), /cannot start recordings from the browser/],
    [ready({appRecording: undefined}), /cannot start recordings from the browser/],
    [status([{tabId: 7, state: 'reloadRequired'}], {tabId: 7, appRecording: 'available'}), /Reload the tab/],
    [status([{tabId: 7, state: 'checking'}], {tabId: 7, appRecording: 'available'}), /page check/],
    [status([], {tabId: 7, appRecording: 'available'}), /Join the call first/],
    [status([{tabId: 4, state: 'ready'}], {tabId: 7, appRecording: 'available'}), /Join the call first/],
  ];
  for (const [result, hint] of reasons) {
    const {record} = popupState(result, tab);
    assert.equal(record.action, 'start'); assert.equal(record.enabled, false); assert.match(record.hint, hint);
  }
  // Manual Start stays available for an unverified (listen-only) call with sources.
  const unverified = ready({tabEvidence: {tabId: 7, callEvidence: {state: 'unknown', reason: 'unverified'}, captureCapability: {kind: 'separated', assurance: 'observed'}}});
  assert.equal(popupState(unverified, tab).title, 'Interactive session is unverified');
  assert.equal(popupState(unverified, tab).record.enabled, true);
  for (const active of [undefined, {...tab, url: 'chrome://extensions/'}]) assert.equal(popupState(ready(), active).record, null);
  assert.equal(popupState({}, tab).record, null);
});

test('popup record control: Stop for this tab only, with saving and pending feedback', async () => {
  const {popupState} = await state;
  const active = extra => status([{tabId: 7, state: 'ready'}], {tabId: 7, recording: true, tracks: 2, appRecording: 'available', ...extra});
  assert.deepEqual(popupState(active(), tab).record, {action: 'stop', enabled: true, hint: '', label: 'Stop recording'});
  assert.deepEqual(popupState(active({recording: false, starting: true}), tab).record.action, 'stop', 'a preparing capture can be stopped');
  assert.deepEqual(popupState(active({stopping: true}), tab).record, {action: 'stop', enabled: false, hint: '', label: 'Saving…'});
  const offline = popupState(active({appRecording: 'disconnected'}), tab).record;
  assert.equal(offline.enabled, false); assert.match(offline.hint, /Stop the recording in the Cassini app/);
  assert.equal(popupState(active({tabId: 8}), tab).record.action, 'start', 'another tab’s capture never offers Stop here');
  const starting = popupState(active({recording: false, recordRequest: {action: 'start', state: 'pending'}}), tab);
  assert.deepEqual(starting.record, {action: 'start', enabled: false, hint: '', label: 'Starting…'});
  assert.equal(starting.title, 'Asking Cassini to start recording…');
  assert.match(starting.note, /not yet confirmed/);
  const stopping = popupState(active({recordRequest: {action: 'stop', state: 'accepted'}}), tab);
  assert.deepEqual(stopping.record, {action: 'stop', enabled: false, hint: '', label: 'Stopping…'});
  assert.equal(stopping.title, 'Asking Cassini to stop recording…');
  assert.equal(popupState(active({tabId: 8, recordRequest: {action: 'stop', state: 'pending'}}), tab).record.label, 'Start recording');
});
test('without a health row, this tab’s reload/checking evidence reaches the record control; evidence-only ready does not enable Start', async () => {
  const {popupState} = await state;
  const evidence = page => status([], {tabId: 7, appRecording: 'available', tabEvidence: {tabId: 7, transport: {page},
    callEvidence: {state: 'unknown', reason: 'unverified'}, captureCapability: {kind: 'unknown', assurance: 'unknown'}}});
  let view = popupState(evidence('reloadRequired'), tab);
  assert.equal(view.title, 'This tab needs to reload');
  assert.deepEqual({...view.record}, {action: 'start', enabled: false, hint: 'Reload the tab before recording.', label: 'Start recording'});
  view = popupState(evidence('checking'), tab);
  assert.equal(view.title, 'Checking the page connection…');
  assert.equal(view.record.enabled, false); assert.equal(view.record.hint, 'Wait until the page check finishes.');
  view = popupState(evidence('ready'), tab);
  assert.equal(view.record.enabled, false);
  assert.match(view.record.hint, /Join the call first/);
  // Evidence for another tab never stands in for this tab.
  const other = evidence('reloadRequired'); other.tabEvidence.tabId = 8;
  assert.match(popupState(other, tab).record.hint, /Join the call first/);
});

test('a tab whose relay was injected after the page loaded asks for a reload instead of claiming only "unverified"',async()=>{
  const {popupState}=await state;
  const evidence=documentStart=>status([{tabId:7,state:'ready'}],{tabEvidence:{tabId:7,transport:{page:'ready',documentStart},
    callEvidence:{state:'unknown',reason:'unverified'},captureCapability:{kind:'separated',assurance:'observed'}}});
  const repaired=popupState(evidence(false),tab);
  assert.equal(repaired.title,'Reload this tab for automatic detection');
  assert.equal(repaired.reloadTabId,7,'the popup offers the Reload button for this tab');
  assert.equal(repaired.tone,'warning');assert.match(repaired.description,/manually/);
  for(const trusted of [true,null])assert.equal(popupState(evidence(trusted),tab).title,'Interactive session is unverified',
    'a document-start relay with no two-way audio is genuinely listen-only');
  const active=evidence(false);active.tabEvidence.callEvidence={state:'active',reason:'activeCall'};
  assert.equal(popupState(active,tab).title,'Interactive media session detected','verified active evidence is never replaced by the hint');
});

test('listen-only copy names the cause only when meeting sources are really there',async()=>{
  const {popupState}=await state;
  const evidence=tabs=>status(tabs,{tabEvidence:{tabId:7,callEvidence:{state:'unknown',reason:'unverified'},captureCapability:{kind:'unknown',assurance:'unknown'}}});
  assert.match(popupState(evidence([{tabId:7,state:'ready'}]),tab).description,/Incoming meeting audio is available/);
  assert.doesNotMatch(popupState(evidence([]),tab).description,/Incoming meeting audio is available/);
});

test('the whole-tab fallback is an explicit browser action offered beside Start, never during a capture',async()=>{
  const {popupState}=await state;
  const idle=popupState(status([]),tab);
  assert.equal(idle.fallback.enabled,true);assert.equal(idle.fallback.label,'Record the whole tab instead');
  assert.match(idle.fallback.hint,/one stereo track, not separate participants/);
  assert.equal(popupState(status([]),{...tab,url:'chrome://extensions'}).fallback,null,'only web pages');
  assert.equal(popupState({},tab).fallback,null,'no worker answer, no action');
  for(const busy of [{recording:true,tabId:7,tracks:1},{starting:true,tabId:7},{recordRequest:{action:'start',state:'pending'},tabId:7}])
    assert.equal(popupState(status([],busy),tab).fallback,null);
  assert.equal(popupState(status([],{recording:true,tabId:8,tracks:1}),tab).fallback.enabled,true,'another tab capture does not hide it here');
});

test('a whole-tab capture started from the popup stops directly, even with no app link; a tracks capture still asks the app',async()=>{
  const {popupState}=await state;
  const wholeTab=popupState({browserHealth:{nativeControl:'disconnected',filesOk:true,tabs:[]},recording:true,tabId:7,tracks:1,mode:'tab',appRecording:'disconnected'},tab);
  assert.equal(wholeTab.record.action,'stop');assert.equal(wholeTab.record.enabled,true);assert.equal(wholeTab.record.direct,true);
  const tracks=popupState({browserHealth:{nativeControl:'disconnected',filesOk:true,tabs:[]},recording:true,tabId:7,tracks:1,mode:'tracks',appRecording:'disconnected'},tab);
  assert.equal(tracks.record.enabled,false);assert.equal(tracks.record.direct,undefined);
  const saving=popupState({browserHealth:{nativeControl:'connected',filesOk:true,tabs:[]},recording:true,stopping:true,tabId:7,tracks:1,mode:'tab'},tab);
  assert.equal(saving.record.enabled,false,'no second stop while saving');
});

// PR #108 review: the card follows what the worker really has for the tab.
const evidenceFor = (extra = {}) => ({tabEvidence: {tabId: 7, transport: {page: 'ready', documentStart: false}, callEvidence: {state: 'unknown', reason: 'unverified'}, captureCapability: {kind: 'separated', assurance: 'observed'}}, ...extra});

test('the repaired-tab reload prompt and the listen-only explanation need real meeting sources, not a bare relay handshake',async()=>{
  const {popupState}=await state;
  const bare=popupState(status([],evidenceFor()),tab);
  assert.equal(bare.reloadTabId,null,'no meeting row, no reload prompt');
  assert.doesNotMatch(bare.description,/Incoming meeting audio is available/);
  const sourced=popupState(status([{tabId:7,state:'ready'}],evidenceFor()),tab);
  assert.equal(sourced.reloadTabId,7);assert.equal(sourced.title,'Reload this tab for automatic detection');
  const trusted=popupState(status([{tabId:7,state:'ready'}],{tabEvidence:{...evidenceFor().tabEvidence,transport:{page:'ready',documentStart:true}}}),tab);
  assert.match(trusted.description,/Incoming meeting audio is available/);
  const handshakeOnly=popupState(status([],{tabEvidence:{...evidenceFor().tabEvidence,transport:{page:'ready',documentStart:true}}}),tab);
  assert.doesNotMatch(handshakeOnly.description,/Incoming meeting audio is available/,'a relay handshake alone is not incoming audio');
});

test('no reload is offered while the app answers a Start or Stop ask',async()=>{
  const {popupState}=await state;
  for(const action of ['start','stop']){
    const view=popupState(status([{tabId:7,state:'ready'}],{...evidenceFor(),tabId:7,recordRequest:{action,state:'accepted'}}),tab);
    assert.equal(view.reloadTabId,null,action);
  }
});

test('the reload guidance survives the worker push that follows the click, and the old roster does not',async()=>{
  const {popupState}=await state;
  const afterClick=status([],{tabEvidence:{tabId:7,transport:{page:'unavailable'},callEvidence:{state:'unknown',reason:'unverified'},captureCapability:{kind:'unknown',assurance:'unknown'}}});
  assert.equal(popupState(afterClick,tab,7).title,'Tab reloaded · rejoin your call','an evidence-only fallback row is not fresh meeting sources');
  assert.equal(popupState(status([{tabId:7,state:'ready'}],evidenceFor()),tab,7).title,'Reload this tab for automatic detection','real sources end the waiting guidance');
});

test('names are shown only while the worker has meeting sources for this tab',async()=>{
  const {popupState}=await state;
  const withNames=tabs=>status(tabs,{...evidenceFor(),participants:['Hypatia']});
  assert.deepEqual(popupState(withNames([]),tab).participants,[]);
  assert.deepEqual(popupState(withNames([{tabId:7,state:'ready'}]),tab).participants,['Hypatia']);
});

test('the call verdict does not depend on the Cassini app being connected, open or running',async()=>{
  const {popupState}=await state;
  const make=(connection,call)=>{const result=status([{tabId:7,state:'ready'}],{appRecording:'disconnected',tabEvidence:{tabId:7,callEvidence:call,
    captureCapability:{kind:'separated',assurance:'observed'}}});result.browserHealth.nativeControl=connection;return popupState(result,tab)};
  for (const connection of ['connected','disconnected']) {
    const active=make(connection,{state:'active',reason:'activeCall'});
    assert.equal(active.title,'Interactive media session detected',connection);assert.equal(active.tone,'good');
    assert.equal(make(connection,{state:'unknown',reason:'unverified'}).title,'Interactive session is unverified',connection);
  }
  const off=make('disconnected',{state:'active',reason:'activeCall'});
  assert.match(off.description,/app link is not connected, so the popup cannot ask it to record/);   // what the missing app means, not that the call is gone
  assert.doesNotMatch(off.title,/not connected|unavailable/i);
  // ...and says nothing about the app while it is connected.
  assert.doesNotMatch(make('connected',{state:'active',reason:'activeCall'}).description,/not connected/);
});
test('an ended call says no call is in progress instead of offering to record it',async()=>{
  const {popupState}=await state;
  for (const connection of ['connected','disconnected']) {
    const result=status([],{tabEvidence:{tabId:7,transport:{page:'ready'},callEvidence:{state:'inactive',reason:'callEnded'},
      captureCapability:{kind:'unknown',assurance:'unknown'}}});result.browserHealth.nativeControl=connection;
    const view=popupState(result,tab);
    assert.equal(view.title,'No call in progress',connection);assert.notEqual(view.tone,'good');
    assert.doesNotMatch(view.description,/start recording this tab/i);
  }
});
