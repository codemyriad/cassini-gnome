// Real MAIN-hook label snapshots; generated DOM/RTC objects, no devices or UI.
const {test}=require('node:test');const assert=require('node:assert/strict');
const fixture=require('./support/page-capture-harness.cjs');
function card(id,name){const c={id:'container_'+id+'_video_incoming'};c.names=[{textContent:name,closest:()=>c}];c.querySelectorAll=()=>c.names;return c;}
async function prove(f,...slots){await f.tick();for(const s of slots)s.packets=10;await f.tick();}
function packets(f,type){return f.messages.filter(m=>m.type==='packet'&&m.packet.type===type).map(m=>m.packet);}

test('Talk names follow exact track objects, never repeated janus labels, IDs or tile order',()=>fixture(async f=>{
 const a=f.remote('janus0'),b=f.remote('janus0');a.receiver.track.id=b.receiver.track.id='janus0';
 f.render(b.receiver.track,card('peer-b','Ibn al-Haytham'));f.render(a.receiver.track,card('peer-a','Hypatia'));
 await prove(f,a,b);await f.start('named',{sourceOutcomes:true,captureLocal:false});
 assert.deepEqual(f.captures.map(c=>[c.stream.getAudioTracks()[0],c.label,c.labelSource]),[
  [a.receiver.track,'Hypatia','dom'],[b.receiver.track,'Ibn al-Haytham','dom']]);
 assert.deepEqual(packets(f,'track').map(p=>[p.label,p.labelSource]),[['Hypatia','dom'],['Ibn al-Haytham','dom']]);
 const sources=packets(f,'sources').flatMap(p=>p.discoveredSources);
 assert.deepEqual(sources.map(s=>[s.label,s.labelSource]),[['Hypatia','dom'],['Ibn al-Haytham','dom']]);
 assert.notEqual(sources[0].sourceTrackId,sources[1].sourceTrackId);
}));

for(const reason of ['different participants','conflicting names','missing name','unrelated card','only equal ID'])
test('ambiguous or missing Talk mapping keeps generic label: '+reason,()=>fixture(async f=>{
 const a=f.remote('janus0'),first=card('peer-a','Hypatia');
 if(reason==='different participants'){f.render(a.receiver.track,first);f.render(a.receiver.track,card('peer-b','Hypatia'));}
 if(reason==='conflicting names'){f.render(a.receiver.track,first);f.render(a.receiver.track,card('peer-a','Ibn al-Haytham'));}
 if(reason==='missing name')f.render(a.receiver.track,card('peer-a','  '));
 if(reason==='unrelated card'){first.id='unrelated-tile';f.render(a.receiver.track,first);}
 if(reason==='only equal ID')f.render({...a.receiver.track},first);
 await prove(f,a);await f.start('generic',{sourceOutcomes:true,captureLocal:false});
 assert.equal(f.captures[0].label,'remote: janus0');assert.notEqual(f.captures[0].labelSource,'dom');
 assert.notEqual(packets(f,'sources')[0].discoveredSources[0].labelSource,'dom');
}));

test('promoted copies of the same Talk participant and name are unambiguous',()=>fixture(async f=>{
 const a=f.remote('janus0'),duplicate=card('peer-a','Hypatia');duplicate.id='placeholder-'+duplicate.id;
 f.render(a.receiver.track,card('peer-a','Hypatia'));f.render(a.receiver.track,duplicate);
 await prove(f,a);await f.start();assert.equal(f.captures[0].label,'Hypatia');assert.equal(f.captures[0].labelSource,'dom');
}));

test('DOM names are sanitized, bounded by code points and frozen at registration',()=>fixture(async f=>{
 const a=f.remote('janus0'),tile=card('peer-a','  Hypatia \n \u0000\u202e'+ '🌙'.repeat(210));
 f.render(a.receiver.track,tile);await prove(f,a);await f.start('snapshot',{sourceOutcomes:true});
 const label=f.captures[0].label;assert.equal([...label].length,200);assert.ok(label.startsWith('Hypatia 🌙'));
 assert.ok(!/[\p{Cc}\p{Cf}]/u.test(label));
 tile.names[0].textContent='Renamed after capture began';await f.scan();
 assert.equal(f.captures.length,1);assert.equal(packets(f,'track')[0].label,label);
 assert.equal(packets(f,'sources').length,1,'no invented live label update');
}));

test('participant DOM cannot grant capture before RTP proof',()=>fixture(async f=>{
 const a=f.remote('janus0');f.render(a.receiver.track,card('peer-a','Hypatia'));
 await f.start('no-proof',{captureLocal:false});assert.equal(f.captures.length,0);
 await prove(f,a);await f.scan();assert.equal(f.captures[0].label,'Hypatia');
}));
