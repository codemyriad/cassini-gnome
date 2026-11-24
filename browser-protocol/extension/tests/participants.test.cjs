const {test}=require('node:test');const assert=require('node:assert/strict');
const lib=import('../participants.mjs'),state=import('../popup-state.mjs');
// Generated Talk-shaped DOM; no devices, no page.
const track=readyState=>({readyState});
function card(id,names){const c={id:'container_'+id+'_video_incoming'};c.nodes=names.map(text=>({textContent:text,closest:()=>c}));c.querySelectorAll=()=>c.nodes;return c;}
function media(c,tracks=[track('live')]){return {srcObject:{getAudioTracks:()=>tracks},closest:()=>c};}
const withDocument=async(medias,run)=>{const {readTalkParticipants}=await lib;globalThis.document={querySelectorAll:()=>medias};try{return run(readTalkParticipants)}finally{delete globalThis.document}};

test('reads one name per remote participant card that carries live audio',()=>withDocument([
  media(card('a',['Hypatia'])),media(card('b',['  Ibn al-Haytham \n'])),
  media(card('c',['Ended']),[track('ended')]),media(card('d',['Video only']),[]),
  media({id:'unrelated',querySelectorAll:()=>[]}),{srcObject:null,closest:()=>null}],
  read=>assert.deepEqual(read(),['Hypatia','Ibn al-Haytham'])));
test('promoted copies of one participant count once; conflicting or missing names stay out',()=>{
  const copy=card('a',['Hypatia']);copy.id='placeholder-'+copy.id;
  return withDocument([media(card('a',['Hypatia'])),media(copy),media(card('x',['One','Two'])),media(card('y',['   '])),media(card('z',[]))],
    read=>assert.deepEqual(read(),['Hypatia']));
});
test('the roster is bounded at 64 participants',()=>withDocument(Array.from({length:80},(_,i)=>media(card('p'+i,['Name '+i]))),
  read=>assert.equal(read().length,64)));
test('page text is stripped and bounded before it reaches the popup',async()=>{
  const {cleanParticipants}=await lib;
  assert.deepEqual(cleanParticipants(['  Hy\u0000pa‮tia \n x ',7,null,{},'',' ','🌙'.repeat(300)]),['Hypatia x','🌙'.repeat(200)]);
  assert.deepEqual(cleanParticipants('Hypatia'),[]);assert.deepEqual(cleanParticipants(undefined),[]);
  assert.equal(cleanParticipants(Array.from({length:100},()=>'n')).length,64);
});
test('the popup lists names only for its own tab and never in the copied diagnostics',async()=>{
  const {popupState}=await state,{copyDiagnostics}=await import('../tab-evidence.mjs');
  const tab={id:7,title:'Meeting',url:'https://example.test/call/x'};
  const result=id=>({browserHealth:{nativeControl:'connected',filesOk:true,tabs:[{tabId:7,state:'ready'}]},participants:['Hypatia','Ibn al-Haytham'],
    tabEvidence:{tabId:id,callEvidence:{state:'unknown',reason:'unverified'},captureCapability:{kind:'separated',assurance:'observed'}}});
  assert.deepEqual(popupState(result(7),tab).participants,['Hypatia','Ibn al-Haytham']);
  assert.deepEqual(popupState(result(8),tab).participants,[],'another tab evidence never lends its roster');
  assert.deepEqual(popupState({browserHealth:result(7).browserHealth},tab).participants,[]);
  assert.doesNotMatch(JSON.stringify(copyDiagnostics(result(7),7)),/Hypatia|Haytham/);
  const request=popupState({...result(7),tabId:7,recordRequest:{action:'start',state:'pending'}},tab);
  assert.deepEqual(request.participants,['Hypatia','Ibn al-Haytham'],'the roster survives the pending-start card');
});

test('an oversized page name is bounded inside the page world, before anything is transferred',()=>withDocument([
  media(card('a',['A'.repeat(4096)])),media(card('b',['🌙'.repeat(900)]))],
  read=>{const names=read();assert.equal([...names[0]].length,200);assert.equal([...names[1]].length,200);}));
