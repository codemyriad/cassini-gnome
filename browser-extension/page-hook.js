(() => {
  if (!window.RTCPeerConnection) return;
  const peers = new Set();
  const knownLocalTracks = new WeakSet();
  const sourceRoots = new WeakMap();
  const sourceRoot = track => sourceRoots.get(track) || track;
  // Remember microphone origins before they become senders, including local previews.
  const devices = navigator.mediaDevices;
  if (devices?.getUserMedia) {
    const getUserMedia = devices.getUserMedia;
    devices.getUserMedia = function (...args) {
      return Reflect.apply(getUserMedia, this, args).then(stream => {
        stream.getAudioTracks().forEach(track => knownLocalTracks.add(track));
        return stream;
      });
    };
    const clone = MediaStreamTrack.prototype.clone;
    MediaStreamTrack.prototype.clone = function (...args) {
      const track = Reflect.apply(clone, this, args);
      sourceRoots.set(track, sourceRoot(this));
      if (knownLocalTracks.has(sourceRoot(this))) knownLocalTracks.add(track);
      return track;
    };
  }
  const original = window.RTCPeerConnection;
  const remember = pc => {
    if (peers.has(pc)) return;
    peers.add(pc);
    pc.addEventListener("connectionstatechange", () => {
      if (pc.connectionState === "closed") peers.delete(pc);
      discover();
    });
    pc.addEventListener("iceconnectionstatechange", () => discover());
  };
  window.RTCPeerConnection = new Proxy(original, {
    construct(target, args, newTarget) { const pc = Reflect.construct(target, args, newTarget); remember(pc); return pc; }
  });
  if (window.webkitRTCPeerConnection === original) window.webkitRTCPeerConnection = window.RTCPeerConnection;
  const close = original.prototype.close;
  original.prototype.close = function (...args) {
    peers.delete(this);
    const value = Reflect.apply(close, this, args);
    discover();
    return value;
  };
  // Recovery for connections created by a cached constructor or before the extension loaded.
  for (const name of ["getStats", "createOffer", "createAnswer", "setLocalDescription", "setRemoteDescription", "addTrack", "addTransceiver"]) {
    const method = original.prototype[name];
    original.prototype[name] = function (...args) { remember(this); return Reflect.apply(method, this, args); };
  }
  // Discovery is independent of capture: no AudioContext, cloned stream or PCM
  // is created until an explicit recording command arrives.
  let lastSourceCount = -1, lastHeard = null, latched = false, zeroSince = null, orphanedSince = null;
  let previousStats = new WeakMap(), sampling = false;
  const evidenceTicks = [], owners = new Set();
  const connected = pc => pc.signalingState !== "closed" &&
    (pc.connectionState === "connected" || ["connected", "completed"].includes(pc.iceConnectionState));
  function discoverySources() {
    const sources = new Set(), receivers = new Map();
    for (const pc of peers) {
      if (pc.signalingState === "closed") { peers.delete(pc); continue; }
      if (!connected(pc)) continue;
      for (const transceiver of pc.getTransceivers()) {
        const track = transceiver.receiver?.track;
        if (["recvonly", "sendrecv"].includes(transceiver.currentDirection) && track?.kind === "audio" && track.readyState === "live") {
          sources.add(track);
          receivers.set(transceiver.receiver, pc);
        }
      }
    }
    return { sourceCount: Math.min(64, sources.size), receivers };
  }
  function resetEvidence() {
    latched = false;
    orphanedSince = null;
    owners.clear();
    evidenceTicks.length = 0;
    previousStats = new WeakMap();
  }
  function discover(force = false) {
    const { sourceCount, receivers } = discoverySources();
    const now = performance.now();
    if (sourceCount > 0) zeroSince = null;
    else if (zeroSince === null) zeroSince = now;
    else if (now - zeroSince >= 10000) resetEvidence();
    if (latched) {
      // Expiry is authoritative even if an owner reconnects before this snapshot.
      if (orphanedSince !== null && now - orphanedSince >= 5000) resetEvidence();
      else if ([...owners].some(connected)) orphanedSince = null;
      else if (orphanedSince === null) orphanedSince = now;
    }
    // Retain a short handoff opportunity, but never lend an old peer's latch
    // to new pre-join receivers that have not supplied their own packet vote.
    const heard = latched && [...receivers.values()].some(pc => owners.has(pc));
    if (force || sourceCount !== lastSourceCount || heard !== lastHeard) {
      lastSourceCount = sourceCount;
      lastHeard = heard;
      window.postMessage({ channel: "cassini-page", type: "discovery", sourceCount, heard }, "*");
    }
    return sourceCount;
  }
  // Forced snapshots cannot vote. Poll receivers even while latched so a new
  // peer may prove a real handoff before the old owners' grace expires.
  async function sampleEvidence() {
    const sourceCount = discover();
    const tick = { at: performance.now(), voters: new Set() };
    evidenceTicks.push(tick);
    if (evidenceTicks.length > 3) evidenceTicks.shift();
    if (sampling || !sourceCount) return;
    sampling = true;
    try {
      const { receivers } = discoverySources();
      const reports = await Promise.all([...receivers].map(([receiver, pc]) => new Promise(resolve => {
        const timer = setTimeout(() => resolve([receiver, pc, null]), 750);
        Promise.resolve().then(() => receiver.getStats()).then(
          stats => { clearTimeout(timer); resolve([receiver, pc, stats]); },
          () => { clearTimeout(timer); resolve([receiver, pc, null]); }
        );
      })));
      if (!evidenceTicks.includes(tick) || performance.now() - tick.at >= 1000) return;
      // Refresh grace/zero resets before consuming asynchronous reports.
      discover();
      if (!evidenceTicks.includes(tick)) return;
      const current = discoverySources();
      for (const [receiver, pc, stats] of reports) {
        if (current.receivers.get(receiver) !== pc || typeof stats?.forEach !== "function") continue;
        const previous = previousStats.get(receiver) || new Map();
        stats.forEach(report => {
          if (report?.type !== "inbound-rtp" || (report.kind ?? report.mediaType) !== "audio" ||
              typeof report.id !== "string" || !Number.isSafeInteger(report.packetsReceived) || report.packetsReceived < 0) return;
          const count = report.packetsReceived;
          if (previous.has(report.id) && count - previous.get(report.id) >= 10) tick.voters.add(pc);
          previous.set(report.id, count);
        });
        // Missing reports keep their last baseline; first appearance never votes.
        previousStats.set(receiver, previous);
      }
      const liveVoters = t => [...t.voters].filter(connected);
      if (!latched && evidenceTicks.filter(t => liveVoters(t).length).length >= 2) {
        latched = true;
        for (const t of evidenceTicks) for (const pc of liveVoters(t)) owners.add(pc);
      } else if (latched) {
        for (const pc of tick.voters) owners.add(pc);
      }
      discover();
    } finally { sampling = false; }
  }
  setInterval(sampleEvidence, 1000);
  let active = null;
  const emit = (token, value) => window.postMessage({ channel: "cassini-page", token, ...value }, "*");
  window.addEventListener("message", async event => {
    if (event.source !== window || event.data?.channel !== "cassini-control") return;
    const message = event.data;
    if (message.type === "discover") { discover(true); return; }
    if (message.type === "ack" && active?.token === message.token) {
      const pending = active.pending.get(message.request);
      if (pending) { clearTimeout(pending.timer); active.pending.delete(message.request); message.error ? pending.reject(new Error(message.error)) : pending.resolve(); }
      return;
    }
    if (message.type === "start" && !active) {
      const state = { token: message.token, pending: new Map(), tracks: new Map(), failedTracks: new WeakSet(), localMetadata: new Map(), next: 0, stopping: false, scanning: false };
      active = state;
      const send = packet => new Promise((resolve, reject) => {
        const request = ++state.next;
        const timer = setTimeout(() => { state.pending.delete(request); reject(new Error("Cassini did not confirm receiving audio.")); }, 10000);
        state.pending.set(request, { resolve, reject, timer });
        emit(state.token, { type: "packet", request, packet });
      });
      const fail = message => emit(state.token, { type: "error", message });
      const warn = () => emit(state.token, { type: "warning" });
      try {
        const { capture } = await import(message.moduleURL);
        if (state.stopping) return;
        const scan = async () => {
          if (state.stopping || state.scanning) return;
          state.scanning = true;
          try {
            const sources = new Map();
            for (const pc of peers) {
              if (pc.signalingState === "closed") { peers.delete(pc); continue; }
              for (const transceiver of pc.getTransceivers()) {
                if (["recvonly", "sendrecv"].includes(transceiver.currentDirection) && transceiver.receiver.track) {
                  sources.set(transceiver.receiver.track, { kind: "remote" });
                }
              }
              for (const sender of pc.getSenders()) if (sender.track) {
                knownLocalTracks.add(sender.track);
                knownLocalTracks.add(sourceRoot(sender.track));
                if (message.captureLocal === false) continue;
                const metadata = { kind: "local", sender, pc };
                sources.set(sender.track, metadata);
                state.localMetadata.set(sender.track, metadata);
              }
            }
            // Preserve the same source across replaceTrack(null)/restore mute cycles.
            for (const [track, metadata] of state.localMetadata) {
              if (track.readyState === "ended" || metadata.pc.signalingState === "closed" || (metadata.sender.track && metadata.sender.track !== track)) state.localMetadata.delete(track);
              else if (!sources.has(track)) sources.set(track, metadata);
            }
            const roots = new Set([...sources.keys()].map(sourceRoot));
            for (const media of document.querySelectorAll("audio,video")) {
              if (media.srcObject instanceof MediaStream) {
                for (const track of media.srcObject.getAudioTracks()) {
                  // A removed sender must not reappear as the page's raw microphone preview.
                  if (!roots.has(sourceRoot(track)) && !knownLocalTracks.has(sourceRoot(track))) {
                    sources.set(track, { kind: "media" });
                    roots.add(sourceRoot(track));
                  }
                }
              }
            }
            for (const [track, recorder] of state.tracks) {
              if (track.readyState === "ended" || !sources.has(track) || recorder.failed) { await recorder.stop().catch(warn); state.tracks.delete(track); }
            }
            for (const [track, metadata] of sources) {
              if (state.stopping) break;
              if (track.kind !== "audio" || track.readyState !== "live" || state.tracks.has(track) || state.failedTracks.has(track)) continue;
              const enabled = () => {
                if (!track.enabled || track.muted) return false;
                if (!metadata.sender) return true;
                if (metadata.sender.track !== track) return false;
                const transceiver = metadata.pc.getTransceivers().find(t => t.sender === metadata.sender);
                if (transceiver && !["sendonly", "sendrecv"].includes(transceiver.currentDirection)) return false;
                const encodings = metadata.sender.getParameters().encodings;
                return !encodings?.length || encodings.some(e => e.active !== false);
              };
              state.context ??= new AudioContext({ sampleRate: 48000 });
              try {
              const recorder = await capture({ stream: new MediaStream([track]), id: crypto.randomUUID(),
                label: `${metadata.kind}: ${track.label || track.id}`, kind: metadata.kind, enabled,
                origin: message.origin, workletURL: message.workletURL, send, sharedContext: state.context, warn,
                fail: () => { state.failedTracks.add(track); warn(); } });
              state.tracks.set(track, recorder);
              } catch { state.failedTracks.add(track); warn(); }
            }
          } catch (error) { fail(error.message); }
          finally { state.scanning = false; }
        };
        state.scan = scan;
        state.timer = setInterval(scan, 500);
        await scan();
        emit(state.token, { type: "ready" });
      } catch (error) { fail(error.message); }
    } else if (message.type === "stop" && active?.token === message.token) {
      const state = active;
      state.stopping = true;
      clearInterval(state.timer);
      while (state.scanning) await new Promise(resolve => setTimeout(resolve, 20));
      const results = await Promise.allSettled([...state.tracks.values()].map(recorder => recorder.stop()));
      const error = results.find(result => result.status === "rejected");
      if (error) emit(state.token, { type: "error", message: error.reason.message });
      await state.context?.close();
      emit(state.token, { type: "stopped" });
      active = null;
    }
  });
})();
