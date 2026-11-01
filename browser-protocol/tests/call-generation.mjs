// D52: real same-document call switches, late peers and receiver reuse.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdir,readFile,writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {connectReceiveSlots,switchReceiveSlot} from '../scenarios/product-topologies.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const server=createServer(async(request,response)=>{
 if(['/capture.js','/pcm-worklet.js'].includes(request.url)){
  response.setHeader('Content-Type','text/javascript');
  response.end(await readFile(path.join(root,'extension',request.url.slice(1))));
 }else response.end('<!doctype html><title>Call generations</title>');
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const address=`http://127.0.0.1:${server.address().port}/`;
const packets=[],notices=[];let browser;
async function until(check,label){
 const deadline=Date.now()+15000;
 while(!await check()){
  assert.ok(Date.now()<deadline,`Timed out: ${label}; notices=${JSON.stringify(notices)}`);
  await new Promise(resolve=>setTimeout(resolve,25));
 }
}
function pcm(id){return Buffer.concat(packets.filter(p=>p.type==='audio'&&p.id===id).map(p=>Buffer.from(p.pcm,'base64')));}
function power(data,frequency){
 const count=12000,start=data.length/2-count;if(start<0)return 0;
 let real=0,imaginary=0;
 for(let i=0;i<count;i++){const v=data.readInt16LE((start+i)*2)/32768;real+=v*Math.cos(2*Math.PI*frequency*i/48000);imaginary+=v*Math.sin(2*Math.PI*frequency*i/48000);}
 return(real**2+imaginary**2)/count**2;
}
try{
 browser=await chromium.launch({headless:true,args:['--autoplay-policy=no-user-gesture-required','--allow-loopback-in-peer-connection','--disable-features=WebRtcHideLocalIpsWithMdns']});
 const context=await browser.newContext(),page=await context.newPage(),remoteA=await context.newPage(),remoteB=await context.newPage();
 await page.addInitScript({path:path.join(root,'extension/page-hook.js')});
 await page.exposeFunction('acceptPacket',packet=>packets.push(packet));
 await page.exposeFunction('acceptNotice',notice=>notices.push(notice));
 await page.goto(address);await remoteA.goto(address);await remoteB.goto(address);
 await page.evaluate(()=>addEventListener('message',async event=>{
  if(event.source!==window||event.data?.channel!=='cassini-page')return;
  const message=event.data;
  if(message.type==='packet'){
   await acceptPacket({...message.packet,token:message.token});
   postMessage({channel:'cassini-control',type:'ack',token:message.token,request:message.request},'*');
  }else await acceptNotice(message);
 }));
 async function command(type,token){
  await page.evaluate(({type,token})=>postMessage({channel:'cassini-control',type,token,captureLocal:false,sourceOutcomes:true,origin:Date.now(),moduleURL:location.origin+'/capture.js',workletURL:location.origin+'/pcm-worklet.js'},'*'),{type,token});
  await until(()=>notices.some(n=>n.type===(type==='start'?'ready':'stopped')&&n.token===token),`${type} ${token}`);
 }
 async function track(token,frequency,oldFrequency){
  let item;
  await until(()=>{
   item=packets.find(p=>p.type==='track'&&p.token===token);
   if(!item)return false;
   const data=pcm(item.id),wanted=power(data,frequency);
   return data.length>=96000&&wanted>0.001&&(!oldFrequency||power(data,oldFrequency)<wanted*0.01);
  },`actual ${frequency} Hz under token ${token}`);
  return item;
 }
 await connectReceiveSlots(page,remoteA,[[440]]);await command('start','A');const a=await track('A',440);
 await page.evaluate(()=>{window.oldPeer=topologyPeer;history.pushState({},'','/call-B');});
 await connectReceiveSlots(page,remoteB,[[880]]);
 await until(()=>packets.some(p=>p.type==='trackEnd'&&p.id===a.id),'A source closes at generation boundary');
 await command('stop','A');await command('start','B');const b=await track('B',880,440);
 assert.equal(await page.evaluate(()=>oldPeer.signalingState==='closed'),false,'old A deliberately closes late');
 const receiver=await page.evaluate(()=>topologyPeer.getReceivers()[0].track.id);
 await page.evaluate(()=>history.pushState({},'','/call-C'));
 await switchReceiveSlot(remoteB,0,[990]);
 // Re-negotiate the same peer and receiver for C; no object replacement can
 // accidentally make the generation test pass.
 const offer=await page.evaluate(async()=>{await topologyPeer.setLocalDescription(await topologyPeer.createOffer());return topologyPeer.localDescription.toJSON();});
 const answer=await remoteB.evaluate(async offer=>{const p=topologyRemote.peer;await p.setRemoteDescription(offer);await p.setLocalDescription(await p.createAnswer());return p.localDescription.toJSON();},offer);
 await page.evaluate(answer=>topologyPeer.setRemoteDescription(answer),answer);
 await until(()=>packets.some(p=>p.type==='trackEnd'&&p.id===b.id),'B source closes before C');
 await command('stop','B');await command('start','C');const c=await track('C',990,880);await command('stop','C');
 assert.equal(await page.evaluate(()=>topologyPeer.getReceivers()[0].track.id),receiver);
 assert.notEqual(a.sourceTrackId,b.sourceTrackId);assert.equal(b.sourceTrackId,c.sourceTrackId);
 assert.equal(new Set([a.id,b.id,c.id]).size,3);
 assert.deepEqual(packets.filter(p=>p.type==='track').map(p=>p.token),['A','B','C']);
 for(const item of [a,b])assert.equal(packets.find(p=>p.type==='trackEnd'&&p.id===item.id).reason,'callChanged');
 assert.deepEqual(notices.filter(n=>n.type==='error'||n.type==='warning'),[],'intentional call change is not a source failure');
 console.log('PASS real call generations: late A stays fenced, reused B receiver records C, no false capture failure');
 await mkdir(path.join(root,'artifacts'),{recursive:true});
 await writeFile(path.join(root,'artifacts/call-generation.json'),JSON.stringify({tracks:[a,b,c],ends:packets.filter(p=>p.type==='trackEnd'),notices},null,2));
}catch(error){
 await mkdir(path.join(root,'artifacts'),{recursive:true});
 await writeFile(path.join(root,'artifacts/call-generation-failure.json'),JSON.stringify({error:error.stack,packets,notices},null,2));
 throw error;
}finally{await browser?.close();server.close();}
