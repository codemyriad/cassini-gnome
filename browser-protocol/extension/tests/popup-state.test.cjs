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
  assert.equal(view.title,'Interactive session is unverified');assert.match(view.description,/Listen-only.*manual/);
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
  assert.equal(view.note, 'Recording is managed in Cassini.');
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
