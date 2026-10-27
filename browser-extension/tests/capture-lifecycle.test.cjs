const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../capture.js'),'utf8');
async function mocked(run) {
 const saved=new Map(['AudioContext','AudioWorkletNode','performance'].map(key=>[key,Object.getOwnPropertyDescriptor(globalThis,key)]));
 const nodes=[],contexts=[];let now=1000;
 class Graph {connect(value){return value}disconnect(){}}
 class Context {
  sampleRate=48000;state='running';currentTime=0;destination=new Graph();listeners=new Set();audioWorklet={addModule:async()=>{}};
  constructor(){contexts.push(this)}createMediaStreamSource(){return new Graph()}createGain(){return Object.assign(new Graph(),{gain:{value:0}})}
  addEventListener(_event,fn){this.listeners.add(fn)}removeEventListener(_event,fn){this.listeners.delete(fn)}
  getOutputTimestamp(){return {contextTime:1,performanceTime:1000}}
  async resume(){if(this.state==='suspended'){now+=500;this.state='running';this.listeners.forEach(fn=>fn())}}
  async close(){this.state='closed'}
 }
 class Worklet extends Graph {constructor(){super();nodes.push(this);this.port={postMessage:()=>this.port.onmessage({data:{stopped:true}})}}}
 globalThis.AudioContext=Context;globalThis.AudioWorkletNode=Worklet;
 Object.defineProperty(globalThis,'performance',{configurable:true,value:{timeOrigin:100000,now:()=>now}});
 try {const module=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));await run({...module,nodes,contexts,Context,setNow:value=>{now=value}})}
 finally {for(const [key,descriptor] of saved){if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key]}}
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('transient suspension resumes without source failure and preserves the elapsed gap',async()=>mocked(async({capture,nodes,contexts,setNow})=>{
 const packets=[],failures=[];const recorder=await capture({stream:{},id:'original',label:'Audio',kind:'local',origin:100000,workletURL:'',send:async p=>packets.push(p),fail:m=>failures.push(m)});
 nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:0}});await tick();
 const context=contexts[0];context.currentTime=8192/48000;setNow(1500);context.state='suspended';context.listeners.forEach(fn=>fn());await tick();await tick();
 nodes[0].port.onmessage({data:{pcm:new ArrayBuffer(8192*2),frame:8192}});await recorder.stop();
 const chunks=packets.filter(p=>p.type==='audio');assert.equal(chunks.length,2);assert.equal(packets.filter(p=>p.type==='track').length,1);
 assert.ok(Math.abs(chunks[1].offsetMs-chunks[0].offsetMs-8192/48-500)<0.001);assert.deepEqual(failures,[]);
}));
test('source errors re-admit under fresh IDs at most three times, then become terminal',async()=>mocked(async({capture,nodes,Context})=>{
 const packets=[],failures=[];const context=new Context();
 const recorder=await capture({stream:{},id:'original',label:'Audio',kind:'local',origin:100000,workletURL:'',sharedContext:context,retryDelayMs:1,send:async p=>packets.push(p),fail:m=>failures.push(m)});
 for(let index=0;index<3;index++){
  nodes[index].onprocessorerror();
  const deadline=Date.now()+1000;while(nodes.length<index+2){assert.ok(Date.now()<deadline);await new Promise(resolve=>setTimeout(resolve,5))}
  assert.equal(recorder.failed,false);
 }
 nodes[3].onprocessorerror();assert.equal(recorder.failed,true);assert.equal(failures.length,1);
 const tracks=packets.filter(p=>p.type==='track');assert.equal(tracks.length,4);assert.equal(new Set(tracks.map(p=>p.id)).size,4);
 assert.ok(tracks.slice(1).every(p=>p.label.endsWith('(resumed)')));await assert.rejects(recorder.stop());
}));
