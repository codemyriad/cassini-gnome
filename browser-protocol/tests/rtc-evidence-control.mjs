// Private fixture native-control sink, never an installed app or recorder.
export async function privateControl(worker){
 await worker.evaluate(()=>{
  const event=()=>{const listeners=new Set();return {addListener:fn=>listeners.add(fn),emit:message=>{for(const fn of listeners)fn(message)}}};
  globalThis.fixturePackets=[];globalThis.fixtureObservers=new Set();globalThis.fixtureCaptureAttempts=0;
  chrome.runtime.connectNative=name=>{
   if(name!=='io.codemyriad.cassini.control'){fixtureCaptureAttempts++;throw Error('Input/recording factory forbidden in evidence test')}
   const port={onMessage:event(),onDisconnect:event(),postMessage(packet){
    fixturePackets.push(structuredClone(packet));for(const notify of [...fixtureObservers])notify();
    if(packet.type==='hello')queueMicrotask(()=>port.onMessage.emit({type:'configure',capabilities:['call-signals-v1','verified-auto-admission-v1']}));
   },disconnect(){port.onDisconnect.emit()}};
   globalThis.fixtureControlPort=port;return port;
  };
 });
}
export async function packet(worker,{type='callSignals',tabId,state,after=0,callGeneration=null,sourceDocumentGeneration=null,sourceCount=null}={}){
 return worker.evaluate(options=>new Promise((resolve,reject)=>{
  let deadline;
  const check=()=>{
   const found=fixturePackets.slice(options.after).filter(value=>value.type===options.type).map(value=>({packet:value,row:(value.tabs||value.meetings||[]).find(row=>row.tabId===options.tabId)})).filter(value=>
    options.tabId==null||value.row&&(options.state==null||value.row.callEvidence?.state===options.state)&&(options.callGeneration==null||value.row.callGeneration===options.callGeneration)&&(options.sourceDocumentGeneration==null||value.row.sourceDocumentGeneration===options.sourceDocumentGeneration)&&(options.sourceCount==null||value.row.sourceCount===options.sourceCount)).at(-1);
   if(found){clearTimeout(deadline);fixtureObservers.delete(check);resolve(found)}
  };
  deadline=setTimeout(()=>{fixtureObservers.delete(check);reject(Error('Timed out waiting for acknowledged worker evidence: '+options.type+' '+options.state))},15000);
  fixtureObservers.add(check);check();
 }),{type,tabId,state,after,callGeneration,sourceDocumentGeneration,sourceCount});
}
export async function barrier(worker){
 await packet(worker,{type:'hello'});
 return worker.evaluate(()=>{const index=fixturePackets.length;fixtureControlPort.onMessage.emit({type:'configure',capabilities:['call-signals-v1','verified-auto-admission-v1']});return index});
}
