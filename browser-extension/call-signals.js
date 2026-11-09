// SPDX-License-Identifier: GPL-3.0-or-later
// Trusted extension-side site definitions. No page messages or audio observation.
const signalToken = value => typeof value === 'string' && value.length > 0 && new TextEncoder().encode(value).length <= 200;
export async function browserProfileIdentity(storage, uuid, timers = {later:(fn,ms)=>setTimeout(fn,ms),cancel:id=>clearTimeout(id)}) {
  let deadline;
  try {
    return await Promise.race([
      (async () => {
        const saved = await storage.get('cassiniBrowserInstance');
        if (signalToken(saved.cassiniBrowserInstance)) return saved.cassiniBrowserInstance;
        const candidate = uuid();
        if (!signalToken(candidate)) return null;
        await storage.set({cassiniBrowserInstance:candidate});
        const persisted = await storage.get('cassiniBrowserInstance');
        return signalToken(persisted.cassiniBrowserInstance) ? persisted.cassiniBrowserInstance : null;
      })(),
      new Promise(resolve => { deadline = timers.later(() => resolve(null), 2500); })
    ]);
  } catch { return null; } // Never advertise an ephemeral connection ID as a profile.
  finally { try { timers.cancel(deadline); } catch {} }
}

export class SiteSignalAdapters {
  constructor(definitions = []) {
    if (!Array.isArray(definitions) || definitions.length > 32) throw Error('Invalid site definitions.');
    this.definitions = definitions.map(definition => {
      if (!definition || !['name','version','hostname'].every(key => typeof definition[key] === 'string' && definition[key].length > 0 && definition[key].length <= 80) ||
          !['separated','mixed','unknown'].includes(definition.remoteAudio) ||
          (definition.evidence !== undefined && typeof definition.evidence !== 'function')) throw Error('Invalid site definition.');
      return Object.freeze({...definition});
    });
  }
  resolve(address) {
    try {
      const url = new URL(address);
      if (url.protocol !== 'https:' || url.port) return null;
      return this.definitions.find(definition => definition.hostname === url.hostname) || null;
    } catch { return null; }
  }
  // Only trusted extension definitions may read their own validated evidence cache.
  // No MAIN-world message, source count or page-supplied adapter can enter here.
  evidence(adapter, document = {}) {
    const unknown = reason => ({state:'unknown',reason,adapter:{name:adapter.name,version:adapter.version},callGeneration:null});
    if (!adapter.evidence) return unknown('unverified');
    if (!signalToken(document.documentGeneration)) return unknown('evidenceConflict');
    let facts;
    try { facts = adapter.evidence(document); } catch { return unknown('evidenceConflict'); }
    if (!facts || facts.documentGeneration !== document.documentGeneration ||
        !['unknown','inactive','active'].includes(facts.state)) return unknown('evidenceConflict');
    const reasons = {unknown:['unverified','unsupported','evidenceConflict'],inactive:['callEnded','nonCall'],active:['activeCall']};
    if (!reasons[facts.state].includes(facts.reason) ||
        ((facts.state === 'active' || facts.reason === 'callEnded') && !signalToken(facts.callGeneration)) ||
        (facts.callGeneration != null && !signalToken(facts.callGeneration))) return unknown('evidenceConflict');
    return {state:facts.state,reason:facts.reason,adapter:{name:adapter.name,version:adapter.version},callGeneration:facts.callGeneration ?? null};
  }
}

export class CallSignalProducer {
  constructor({identity, tabs, frames, current, adapters = new SiteSignalAdapters(), timers = {later:(fn,ms)=>setTimeout(fn,ms),cancel:id=>clearTimeout(id)}}) {
    Object.assign(this,{identity,tabs,frames,current,adapters,timers});
  }
  async snapshot() {
    const browserInstance = this.identity();
    if (!signalToken(browserInstance)) return null;
    const candidates = [...this.frames()].filter(frame => frame.frameId === 0 && signalToken(frame.browserDocumentId));
    let tabs, deadline;
    try {
      tabs = await Promise.race([this.tabs.query({}),new Promise(resolve=>{
        deadline=this.timers.later(()=>resolve(null),2500);
      })]);
    }
    catch { return {browserInstance,tabs:[]}; }
    finally { try { this.timers.cancel(deadline); } catch {} } // Withdraw facts, never replay cached active.
    if (!Array.isArray(tabs)) return {browserInstance,tabs:[]};
    const byId = new Map(tabs.map(tab => [tab.id,tab]));
    const rows = [];
    for (const frame of candidates.sort((a,b) => a.tabId-b.tabId)) {
      if (rows.length === 100) break;
      const tab = byId.get(frame.tabId), adapter = tab && this.adapters.resolve(tab.url);
      if (!adapter || !this.current(frame)) continue;
      try {
        // Sender URL/documentId are Chrome-owned, not MAIN-world discovery hints.
        if (new URL(frame.browserUrl).origin !== new URL(tab.url).origin || tab.url.length > 2048) continue;
      } catch { continue; }
      const callEvidence = this.adapters.evidence(adapter,{tabId:frame.tabId,documentGeneration:frame.browserDocumentId});
      if (!this.current(frame)) continue;
      rows.push({tabId:frame.tabId,url:tab.url,documentGeneration:frame.browserDocumentId,remoteAudio:adapter.remoteAudio,callEvidence});
    }
    return {browserInstance,tabs:rows};
  }
}

// Generic interactive-media producer. Browser-owned results and trusted
// document-start relays bind the private observer; page assertions never qualify.
export class GenericCallEvidenceProducer {
  constructor({identity,tabs,frames,current,currentSource=current,executeScript,callIdentity,onNativeRead=()=>{},
    now=()=>globalThis.performance?.now?.() ?? Date.now(),
    timers={later:(fn,ms)=>setTimeout(fn,ms),cancel:id=>clearTimeout(id)}}) {
    Object.assign(this,{identity,tabs,frames,current,currentSource,executeScript,callIdentity,onNativeRead,now,timers});
    this.publications=new WeakMap();this.lastWire=null;this.epoch=null;this.lastPublishedAt=-Infinity;
    this.prepared=new WeakMap();this.activePublications=new Map();
  }
  async bounded(read) {
    let deadline;
    try { return await Promise.race([Promise.resolve().then(read),new Promise(resolve=>{
      deadline=this.timers.later(()=>resolve(null),750);
    })]); } catch { return null; }
    finally { this.timers.cancel(deadline); }
  }
  finalize(snapshot) {
    const prepared=this.prepared.get(snapshot);
    if (!prepared) return {...snapshot,tabs:[]};
    const tabs=snapshot.tabs.filter(row=>{
      const {frame,binding}=prepared.get(row.tabId)||{};
      return frame&&this.current(frame)&&frame.browserDocumentId===binding.browser&&
        frame.documentGeneration===binding.hook&&frame.callGeneration===binding.generation&&
        frame.obsoleteEpoch===binding.epoch&&this.callIdentity(frame)===binding.call;
    }).map(row=>{
      if (row.callEvidence.state==='active'&&this.now()>prepared.get(row.tabId).binding.validUntil)
        return {...row,callEvidence:{...row.callEvidence,state:'unknown',reason:'unverified',callGeneration:null}};
      return row;
    });
    const result={...snapshot,tabs};
    for(const [id] of this.activePublications)if(!tabs.some(row=>row.tabId===id&&row.callEvidence.state==='active'))this.activePublications.delete(id);
    this.lastWire=JSON.stringify(result);return result;
  }
  async read(frame) {
    if (!this.currentSource(frame) || !Number.isInteger(frame.frameId) || frame.frameId<0 || !signalToken(frame.browserDocumentId) || frame.observerDocumentStart!==true) return null;
    const browser=frame.browserDocumentId,epoch=frame.obsoleteEpoch,frameId=frame.frameId;
    const results=await this.bounded(()=>this.executeScript({
      target:{tabId:frame.tabId,documentIds:[browser]},world:'MAIN',
      // This identifier is a non-configurable own global installed at
      // document_start. No Object/globalThis/prototype or page message lookup.
      func:()=>__cassiniCallEvidenceV1.read()
    }));
    if (frame.observerDocumentStart!==true || !this.currentSource(frame) || frame.browserDocumentId!==browser || frame.obsoleteEpoch!==epoch || frame.frameId!==frameId ||
        !Array.isArray(results) || results.length!==1 || results[0]?.documentId!==browser || results[0]?.frameId!==frameId) return null;
    const value=results[0].result;
    frame.nativeCollectorUnavailable=value===null;
    if(value===null)frame.nativeSourceVerified=false;
    if (!value || value.version!==2 || !signalToken(value.documentGeneration) || !signalToken(value.hookGeneration) ||
        !Number.isInteger(value.sourceCount) || value.sourceCount<0 || value.sourceCount>64 || typeof value.heard!=='boolean') return null;
    this.onNativeRead(frame,value);
    if (frame.observerDocumentStart!==true || !this.currentSource(frame) || frame.browserDocumentId!==browser || frame.obsoleteEpoch!==epoch || frame.frameId!==frameId) return null;
    return value;
  }
  async observe(frame) {
    const started=this.now(),value=await this.read(frame);
    if (!value || value.version !== 2 || value.documentGeneration !== frame.documentGeneration || value.hookGeneration !== frame.callGeneration ||
        typeof value.ended!=='boolean' || typeof value.cohortQualified!=='boolean' ||
        (value.cohortQualified&&(value.receivedPeers===0||value.capturedSendingPeers===0)) ||
        !['receivedVersion','sentVersion'].every(key=>Number.isSafeInteger(value[key])&&value[key]>=0) ||
        (!value.ended && (!Number.isSafeInteger(value.revision) || value.revision <= 0 || typeof value.ageMs !== 'number' || !Number.isFinite(value.ageMs) || value.ageMs < 0 || value.ageMs+Math.max(0,this.now()-started) > 2500)) ||
        !['receivedPeers','capturedSendingPeers','samePeerCount'].every(key=>Number.isSafeInteger(value[key])&&value[key]>=0&&value[key]<=64) ||
        value.samePeerCount > Math.min(value.receivedPeers,value.capturedSendingPeers)) return null;
    return {facts:value,validUntil:value.ended?Infinity:started+2500-value.ageMs};
  }
  async snapshot({verified=false,epoch=0,configurationEpoch=0,preview=false,tabId=null}={}) {
    if (!preview && epoch!==this.epoch) {this.epoch=epoch;this.publications=new WeakMap();this.activePublications.clear();this.lastWire=null;this.lastPublishedAt=-Infinity;}
    if (!preview && configurationEpoch!==this.configurationEpoch) {this.configurationEpoch=configurationEpoch;this.lastWire=null;}
    const currentPublication=()=>preview||this.epoch===epoch&&this.configurationEpoch===configurationEpoch;
    const browserInstance=this.identity();
    if (!signalToken(browserInstance)) return null;
    if (!verified) return {browserInstance,tabs:[]};
    const frames=[...this.frames()].filter(frame=>frame.frameId===0&&this.current(frame)&&(tabId===null||frame.tabId===tabId)).slice(0,100);
    const tabs=await this.bounded(()=>this.tabs.query({}));
    if (!currentPublication()) return null;
    if (!Array.isArray(tabs)) return {browserInstance,tabs:[]};
    const bindings=new Map();
    const currentBinding=frame=>{
      const binding=bindings.get(frame);
      return binding && this.current(frame) && frame.browserDocumentId===binding.browser &&
        frame.documentGeneration===binding.hook && frame.callGeneration===binding.generation &&
        frame.obsoleteEpoch===binding.epoch && this.callIdentity(frame)===binding.call;
    };
    const rows=await Promise.all(frames.map(async frame=>{
      const tab=tabs.find(tab=>tab.id===frame.tabId);
      try {
        if (!tab || !/^https?:$/.test(new URL(tab.url).protocol) || tab.url.length>2048 ||
            new URL(frame.browserUrl).origin!==new URL(tab.url).origin || !signalToken(frame.browserDocumentId)) return null;
      } catch { return null; }
      bindings.set(frame,{browser:frame.browserDocumentId,hook:frame.documentGeneration,
        generation:frame.callGeneration,epoch:frame.obsoleteEpoch,call:this.callIdentity(frame)});
      const observation=await this.observe(frame),observed=observation?.facts;
      bindings.get(frame).validUntil=observation?.validUntil ?? 0;
      if (!currentPublication() || !currentBinding(frame)) return null;
      bindings.get(frame).call=this.callIdentity(frame);
      const generation=this.callIdentity(frame);
      const active=observed?.cohortQualified===true && observed.receivedVersion>0 && observed.sentVersion>0 && signalToken(generation);
      bindings.get(frame).proof=observed;
      bindings.get(frame).active=active;
      const previous=this.publications.get(frame);
      bindings.get(frame).renewed=active&&(!previous||generation!==previous.call||observed.receivedVersion>previous.received&&observed.sentVersion>previous.sent);
      const foreignSources=[...this.frames()].some(other=>other!==frame&&other.tabId===frame.tabId&&
        other.nativeSourceVerified===true&&other.sourceCount>0&&this.currentSource(other));
      const captureReady=observed?.sourceCount>0 && (!active || !foreignSources);
      return {tabId:frame.tabId,url:tab.url,documentGeneration:frame.browserDocumentId,
        sourceDocumentGeneration:observed?.documentGeneration ||
          (frame.observerDocumentStart===true&&frame.nativeSourceVerified===true&&signalToken(frame.documentGeneration)?frame.documentGeneration:null),
        remoteAudio:captureReady?'separated':'unknown',
        callEvidence:{state:active?'active':observed?.ended?'inactive':'unknown',reason:active?'activeCall':observed?.ended?'callEnded':'unverified',
          adapter:{name:'webrtc-interactive',version:'2'},callGeneration:active||observed?.ended?generation:null}};
    }));
    // All reads/Chrome lookups have completed: fence documents retired while a
    // different tab was still pending, before any snapshot can leave the worker.
    const snapshot={browserInstance,tabs:rows.flatMap((row,index)=>{
      const frame=frames[index];
      if (!row || !currentBinding(frame)) return [];
      if (row.callEvidence.state==='active' && this.now()>bindings.get(frame).validUntil)
        row.callEvidence={...row.callEvidence,state:'unknown',reason:'unverified',callGeneration:null};
      return [row];
    })};
    if (!currentPublication()) return null;
    if (preview) return snapshot;
    const stale=snapshot.tabs.filter(row=>row.callEvidence.state==='active'&&!bindings.get(frames.find(frame=>frame.tabId===row.tabId)).renewed);
    const raw=JSON.stringify(snapshot);
    if (stale.length && this.lastWire!==null && this.activePublications.size &&
        !snapshot.tabs.some(row=>row.callEvidence.state==='inactive') &&
        [...this.activePublications].every(([id,previous])=>{
          const row=snapshot.tabs.find(row=>row.tabId===id),frame=frames.find(frame=>frame.tabId===id),binding=bindings.get(frame);
          return row?.callEvidence.state==='active' && frame===previous.frame && binding &&
            binding.browser===previous.browser && binding.hook===previous.hook && binding.generation===previous.generation &&
            binding.epoch===previous.epoch && binding.call===previous.call && this.now()-previous.publishedAt<2000;
        })) return null;
    if (!stale.length && raw===this.lastWire && !snapshot.tabs.some(row=>row.callEvidence.state==='active')) return null;
    // Coalesce normal refreshes; actual withdrawal/navigation bypasses this.
    if (!stale.length && raw===this.lastWire && this.now()-this.lastPublishedAt<1000) return null;
    const activePublications=new Map();
    for (const row of snapshot.tabs) {
      const frame=frames.find(frame=>frame.tabId===row.tabId),binding=bindings.get(frame);
      if (row.callEvidence.state==='active' && !binding.renewed)
        row.callEvidence={...row.callEvidence,state:'unknown',reason:'unverified',callGeneration:null};
      if (row.callEvidence.state==='active') {
        const publication={frame,browser:binding.browser,hook:binding.hook,generation:binding.generation,epoch:binding.epoch,
          call:binding.call,received:binding.proof.receivedVersion,sent:binding.proof.sentVersion,publishedAt:this.now()};
        this.publications.set(frame,publication);activePublications.set(row.tabId,publication);
      }
    }
    if (JSON.stringify(snapshot)===this.lastWire&&!snapshot.tabs.some(row=>row.callEvidence.state==='active')) return null;
    this.activePublications=activePublications;
    this.lastWire=JSON.stringify(snapshot);this.lastPublishedAt=this.now();
    this.prepared.set(snapshot,new Map(frames.filter(frame=>bindings.has(frame)).map(frame=>[frame.tabId,{frame,binding:bindings.get(frame)}])));
    return snapshot;
  }
}
