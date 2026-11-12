// Test-only observation installed before the disposable Talk page creates peers.
// No media, SDP, track/device IDs, URLs or DOM text enter the numeric report.
export function installRtcTopologyProbe() {
  const Native = window.RTCPeerConnection;
  const peers = new Set(), captured = new WeakSet();
  const senders = Native.prototype.getSenders, receivers = Native.prototype.getReceivers;
  const stats = Native.prototype.getStats;
  const devices = navigator.mediaDevices, gum = devices.getUserMedia;
  devices.getUserMedia = function (...args) {
    return Reflect.apply(gum, this, args).then(stream => {
      stream.getAudioTracks().forEach(track => captured.add(track));
      return stream;
    });
  };
  // A fixture declares the ground-truth provenance of a track it derived from
  // the microphone by other means (WebAudio, MediaStream.clone), independently
  // of the packaged hook under test.
  window.cassiniRtcDeclareCaptured = track => captured.add(track);
  const clone = MediaStreamTrack.prototype.clone;
  MediaStreamTrack.prototype.clone = function (...args) {
    const result = Reflect.apply(clone, this, args);
    if (captured.has(this)) captured.add(result);
    return result;
  };
  window.RTCPeerConnection = new Proxy(Native, {
    construct(target, args, newTarget) {
      const pc = Reflect.construct(target, args, newTarget);
      peers.add(pc);
      return pc;
    }
  });
  if (window.webkitRTCPeerConnection === Native) window.webkitRTCPeerConnection = window.RTCPeerConnection;
  // Study only: export opaque equality classes of standardized native SDP
  // fields, never SDP/credentials/fingerprints/names themselves. Shared server
  // identity alone is explicitly NOT an admission/session-cohort rule.
  const descriptor=Object.getOwnPropertyDescriptor,apply=Reflect.apply;
  const local=descriptor(Native.prototype,'localDescription')?.get;
  const remote=descriptor(Native.prototype,'remoteDescription')?.get;
  const sdp=descriptor(RTCSessionDescription.prototype,'sdp')?.get;
  window.cassiniRtcCohortFacts=(groups=new Map())=>{
    // A same-origin disposable peer window may share this private table for
    // cross-room comparison. Only integer references enter the returned facts.
    const reference=(kind,value)=>{
      if(!value||value==='-')return null;
      let values=groups.get(kind);if(!values){values=new Map();groups.set(kind,values)}
      if(!values.has(value))values.set(value,values.size);
      return values.get(value);
    };
    const fields=description=>{
      const result={originSession:[],sessionName:[],iceUsername:[],fingerprint:[],rtcpCname:[],streamId:[]};
      let text;try{text=apply(sdp,description,[])}catch{return result}
      if(typeof text!=='string'||text.length>1048576)return result;
      for(const line of text.split(/\r?\n/)){
        let kind,value;
        if(line.startsWith('o=')){kind='originSession';value=line.slice(2).split(/\s+/)[1]}
        else if(line.startsWith('s=')){kind='sessionName';value=line.slice(2)}
        else if(line.startsWith('a=ice-ufrag:')){kind='iceUsername';value=line.slice(12)}
        else if(line.startsWith('a=fingerprint:')){kind='fingerprint';value=line.slice(14)}
        else if(/^a=ssrc:\d+ cname:/.test(line)){kind='rtcpCname';value=line.slice(line.indexOf(' cname:')+7)}
        else if(line.startsWith('a=msid:')){kind='streamId';value=line.slice(7).split(/\s+/)[0]}
        if(kind&&result[kind].length<32){const group=reference(kind,value);if(group!==null&&!result[kind].includes(group))result[kind].push(group)}
      }
      return result;
    };
    return {notAdmissionRule:true,scope:'native applied descriptions; integer equality classes only',
      peers:[...peers].slice(0,16).map((pc,peer)=>{
        let a=null,b=null;try{a=apply(local,pc,[]);b=apply(remote,pc,[])}catch{}
        return {peer,local:fields(a),remote:fields(b)};
      })};
  };
  window.cassiniRtcTopology = async () => Promise.all([...peers].map(async (pc, index) => {
    const live = track => track?.kind === 'audio' && track.readyState === 'live';
    const row = {peer:index, connected:pc.connectionState === 'connected',
      liveCapturedSenders:Reflect.apply(senders, pc, []).filter(sender =>
        live(sender.track) && sender.track.enabled && !sender.track.muted && captured.has(sender.track)).length,
      liveReceivers:Reflect.apply(receivers, pc, []).filter(receiver => live(receiver.track)).length,
      outboundPackets:0, inboundPackets:0};
    (await Reflect.apply(stats, pc, [])).forEach(report => {
      if ((report.kind ?? report.mediaType) !== 'audio') return;
      if (report.type === 'outbound-rtp') row.outboundPackets += report.packetsSent || 0;
      if (report.type === 'inbound-rtp') row.inboundPackets += report.packetsReceived || 0;
    });
    return row;
  }));
}

export function summarizeRtcTopology(before, after) {
  const progressing = after.map(row => {
    const previous = before.find(value => value.peer === row.peer);
    return {...row, sending:!!previous && row.connected && row.liveCapturedSenders > 0 && row.outboundPackets > previous.outboundPackets,
      receiving:!!previous && row.connected && row.liveReceivers > 0 && row.inboundPackets > previous.inboundPackets};
  });
  return {peers:progressing, samePeerConnection:progressing.some(row => row.sending && row.receiving),
    sendingPeers:progressing.filter(row => row.sending).length,
    receivingPeers:progressing.filter(row => row.receiving).length};
}

// getStats is asynchronous. Await each sample before testing it, and return
// that exact sample. An async waitForFunction predicate can resolve its first
// Promise to false instead of polling the eventual state.
export async function waitRtcTopology(page,{previous=null,sending='none',receiving=true}={}) {
  await (page.bringToFront ? page : page.page()).bringToFront(); // Disposable fixture only.
  return page.evaluate(async({previous,sending,receiving})=>{
    const deadline=performance.now()+30000;
    let latest=[];
    while(performance.now()<deadline){
      const rows=await cassiniRtcTopology();
      latest=rows;
      const grows=(row,key)=>row[key]>(previous?previous.find(value=>value.peer===row.peer)?.[key]??Infinity:0);
      const received=rows.some(row=>row.connected&&grows(row,'inboundPackets'));
      const sent=sending==='none'||rows.some(row=>row.connected&&grows(row,'outboundPackets')&&
        (sending!=='captured'||row.liveCapturedSenders>0));
      if((!receiving||received)&&sent)return rows;
      await new Promise(resolve=>requestAnimationFrame(resolve));
    }
    throw Error('Timed out waiting for real RTC packet progress: '+JSON.stringify(latest));
  },{previous,sending,receiving});
}

// Compare only opaque integer groups produced with one private in-browser table.
// Shared fields are candidate measurements, never proof of an interactive session.
export function summarizeRtcCohortComparison(first, second) {
  const fields = ['originSession', 'sessionName', 'iceUsername', 'fingerprint', 'rtcpCname', 'streamId'];
  const values = (facts, field) => new Set(facts.peers.flatMap(peer => peer.remote[field]));
  return {notAdmissionRule:true, scope:'same backend, different disposable rooms; shared private equality table',
    first, second, sharedRemoteGroups:Object.fromEntries(fields.map(field => {
      const other = values(second, field);
      return [field, [...values(first, field)].filter(value => other.has(value))];
    }))};
}
