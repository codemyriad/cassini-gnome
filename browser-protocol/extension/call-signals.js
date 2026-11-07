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
  constructor(definitions = [{name:'whatsapp-web',version:'1-unverified',hostname:'web.whatsapp.com',remoteAudio:'mixed'}]) {
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
