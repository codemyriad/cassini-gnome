(() => {
  if (!window.RTCPeerConnection) return;
  const peers = new Set();
  const peerGenerations = new WeakMap();
  const knownLocalTracks = new WeakSet();
  const knownReceiverRoots = new WeakSet();
  const sourceRoots = new WeakMap();
  const sourceKeys = new WeakMap();
  const receiverGenerations = new WeakMap(), trackGenerations = new WeakMap();
  const documentGeneration = globalThis.crypto?.randomUUID?.() || "doc-" + Math.random().toString(36).slice(2);
  let nextSource = 0, callGeneration = 0, lastCallGeneration = -1;
  let callAddress = window.location?.href?.split("#", 1)[0];
  function refreshCall() {
    const next = window.location?.href?.split("#", 1)[0];
    if (next !== callAddress) { callAddress = next; callGeneration++; resetEvidence(); }
  }
  function receiverGeneration(receiver, pc) {
    let value = receiverGenerations.get(receiver);
    if (!value || value.track !== receiver.track) {
      const generation = peerGenerations.get(pc);
      value = { track: receiver.track, generation };
      receiverGenerations.set(receiver, value);
      trackGenerations.set(sourceRoot(receiver.track), generation);
    }
    return value.generation;
  }
  function sourceKey(root) {
    if (!sourceKeys.has(root)) sourceKeys.set(root, documentGeneration + ":source:" + (++nextSource));
    return sourceKeys.get(root);
  }
  const sourceRoot = track => sourceRoots.get(track) || track;
  function rememberReceivers(pc) {
    for (const {receiver} of pc.getTransceivers()) if (receiver?.track) {
      knownReceiverRoots.add(sourceRoot(receiver.track));
      receiverGeneration(receiver, pc);
    }
  }
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
  }
  const trackPrototype = globalThis.MediaStreamTrack?.prototype;
  if (trackPrototype?.clone) {
    const clone = trackPrototype.clone;
    trackPrototype.clone = function (...args) {
      const track = Reflect.apply(clone, this, args);
      sourceRoots.set(track, sourceRoot(this));
      if (knownLocalTracks.has(sourceRoot(this))) knownLocalTracks.add(track);
      return track;
    };
  }
  const original = window.RTCPeerConnection;
  const remember = pc => {
    refreshCall();
    if (peers.has(pc)) return;
    peers.add(pc);
    peerGenerations.set(pc, callGeneration);
    pc.addEventListener("connectionstatechange", () => {
      if (pc.connectionState === "closed") { rememberReceivers(pc); peers.delete(pc); }
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
    rememberReceivers(this);
    peers.delete(this);
    const value = Reflect.apply(close, this, args);
    discover();
    return value;
  };
  // Recovery for connections created by a cached constructor or before the extension loaded.
  for (const name of ["getStats", "createOffer", "createAnswer", "setLocalDescription", "setRemoteDescription", "addTrack", "addTransceiver"]) {
    const method = original.prototype[name];
    original.prototype[name] = function (...args) {
      remember(this);
      const generation = callGeneration;
      const value = Reflect.apply(method, this, args);
      if (name === "setRemoteDescription" && typeof value?.then === "function") {
        value.then(() => {
          refreshCall();
          // A new call may reuse the same peer/receiver, but an old delayed
          // negotiation and read-only stats cannot carry A's sources into B.
          if (generation !== callGeneration || peerGenerations.get(this) === generation) return;
          peerGenerations.set(this, generation);
          for (const { receiver } of this.getTransceivers()) {
            receiverGenerations.delete(receiver);
            previousStats.delete(receiver);
            packetProof.delete(receiver);
          }
          discover();
        }, () => {}).catch(() => {});
      }
      return value;
    };
  }
  // Discovery is independent of capture: no AudioContext, cloned stream or PCM
  // is created until an explicit recording command arrives.
  let lastSourceCount = -1, lastHeard = null, latched = false, zeroSince = null, orphanedSince = null;
  let previousStats = new WeakMap(), packetProof = new WeakMap(), sampling = false;
  const evidenceTicks = [], owners = new Set();
  const connected = pc => pc.signalingState !== "closed" &&
    (pc.connectionState === "connected" || ["connected", "completed"].includes(pc.iceConnectionState));
  function discoverySources() {
    const sources = new Set(), receivers = new Map();
    for (const pc of peers) {
      rememberReceivers(pc);
      if (pc.signalingState === "closed") { peers.delete(pc); continue; }
      if (!connected(pc)) continue;
      for (const transceiver of pc.getTransceivers()) {
        const track = transceiver.receiver?.track;
        if (["recvonly", "sendrecv"].includes(transceiver.currentDirection) && track?.kind === "audio" && track.readyState === "live") {
          if (receiverGeneration(transceiver.receiver, pc) !== callGeneration) continue;
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
    packetProof = new WeakMap();
  }
  function discover(force = false) {
    refreshCall();
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
    if (force || sourceCount !== lastSourceCount || heard !== lastHeard || callGeneration !== lastCallGeneration) {
      lastSourceCount = sourceCount;
      lastHeard = heard;
      lastCallGeneration = callGeneration;
      window.postMessage({ channel: "cassini-page", type: "discovery", sourceCount, heard, documentGeneration, callGeneration: String(callGeneration) }, "*");
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
          if (previous.has(report.id) && count > previous.get(report.id)) {
            packetProof.set(receiver, { pc, track: receiver.track });
            if (count - previous.get(report.id) >= 10) tick.voters.add(pc);
          }
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
      if (pending) { clearTimeout(pending.timer); active.pending.delete(message.request); message.error ? pending.reject(Object.assign(new Error(message.error), {code: message.code, scope: message.scope, ...(message.retired === true ? {retired:true} : {})})) : pending.resolve(); }
      return;
    }
    if (message.type === "start" && !active) {
      refreshCall();
      const state = { token: message.token, callGeneration, sourceOutcomes: message.sourceOutcomes === true, pending: new Map(), sourceUpdates: new Set(), announcedSources: new Set(), tracks: new Map(), recordsByTrack: new WeakMap(), warnings: new Map(), refusedTracks: new WeakSet(), sources: new Map(), spanIds: new Set(), localMetadata: new Map(), next: 0, stopping: false, scanning: false };
      active = state;
      const send = packet => new Promise((resolve, reject) => {
        refreshCall();
        if (state.callGeneration !== callGeneration && packet.type !== "trackEnd") {
          const error = Object.assign(new Error("This audio belongs to a different call."), { code: "callChanged" });
          discover(true); reject(error); return;
        }
        const request = ++state.next;
        const timer = setTimeout(() => { state.pending.delete(request); reject(new Error("Cassini did not confirm receiving audio.")); }, 10000);
        state.pending.set(request, { resolve: () => { if (packet.type === "track") state.spanIds.add(packet.id); resolve(); }, reject, timer });
        emit(state.token, { type: "packet", request, packet });
      });
      const fail = message => emit(state.token, { type: "error", message });
      const intentional = error => error.code === "callChanged" ||
        error.code === "state" && ["source", "command"].includes(error.scope);
      const announce = record => {
        if (!state.sourceOutcomes) return Promise.resolve();
        const pending = send({type: "sources", discoveredSources: [{...record, label: [...record.label].slice(0, 200).join("")}]}).then(() => { state.announcedSources.add(record.sourceTrackId); });
        state.sourceUpdates.add(pending);
        pending.then(() => state.sourceUpdates.delete(pending), () => state.sourceUpdates.delete(pending));
        return pending;
      };
      const warn = (transient=false, sourceTrackId, message) => {
        const details = {};
        if (sourceTrackId) {
          const previous = state.warnings.get(sourceTrackId);
          message ||= previous?.message || "One track was interrupted and has a gap. The other tracks continue.";
          if (previous?.message === message && previous.transient === transient) return;
          state.warnings.set(sourceTrackId, {message, transient});
          Object.assign(details, {sourceTrackId, message});
        }
        emit(state.token, { type: "warning", transient: transient === true, ...details });
      };
      const recovered = sourceTrackId => {
        if (state.warnings.delete(sourceTrackId)) emit(state.token, {type:"warning", sourceTrackId, recovered:true, transient:true});
      };
      const sourceFailed = (record, message) => {
        if (!record.failed) {
          record.failed = true;
          void announce(record).catch(error => { if (!intentional(error)) fail(error.message); });
          warn(false, record.sourceTrackId, message);
        }
      };
      state.sourceFailed = sourceFailed;
      try {
        const { capture } = await import(message.moduleURL);
        if (state.stopping) return;
        const scan = async () => {
          if (state.stopping || state.scanning) return;
          state.scanning = true;
          try {
            refreshCall();
            if (state.callGeneration !== callGeneration) {
              await Promise.allSettled([...state.tracks.values()].map(recorder => recorder.stop("callChanged")));
              state.tracks.clear(); discover(true); return;
            }
            const sources = new Map(), roots = new Set();
            for (const pc of peers) {
              rememberReceivers(pc);
              for (const {receiver} of pc.getTransceivers()) if (receiver?.track) roots.add(sourceRoot(receiver.track));
              if (pc.signalingState === "closed") { peers.delete(pc); continue; }
              for (const transceiver of pc.getTransceivers()) {
                const receiver = transceiver.receiver;
                const proof = receiver && packetProof.get(receiver);
                if (connected(pc) && ["recvonly", "sendrecv"].includes(transceiver.currentDirection) && receiver?.track &&
                    proof?.pc === pc && proof.track === receiver.track && receiverGeneration(receiver, pc) === callGeneration) {
                  sources.set(receiver.track, { kind: "remote", root: receiver });
                }
              }
              for (const sender of pc.getSenders()) if (sender.track) {
                knownLocalTracks.add(sender.track);
                knownLocalTracks.add(sourceRoot(sender.track));
                if (message.captureLocal === false || peerGenerations.get(pc) !== callGeneration) continue;
                const metadata = { kind: "local", sender, pc, root: sender };
                sources.set(sender.track, metadata);
                state.localMetadata.set(sender.track, metadata);
              }
            }
            // Preserve the same source across replaceTrack(null)/restore mute cycles.
            for (const [track, metadata] of state.localMetadata) {
              if (track.readyState === "ended" || metadata.pc.signalingState === "closed" || (metadata.sender.track && metadata.sender.track !== track)) state.localMetadata.delete(track);
              else if (!sources.has(track)) sources.set(track, metadata);
            }
            for (const track of sources.keys()) roots.add(sourceRoot(track));
            for (const media of document.querySelectorAll("audio,video")) {
              if (media.srcObject instanceof MediaStream) {
                for (const track of media.srcObject.getAudioTracks()) {
                  // A removed sender must not reappear as the page's raw microphone preview.
                  if (!roots.has(sourceRoot(track)) && !knownReceiverRoots.has(sourceRoot(track)) && !knownLocalTracks.has(sourceRoot(track)) &&
                      (!trackGenerations.has(sourceRoot(track)) || trackGenerations.get(sourceRoot(track)) === callGeneration)) {
                    trackGenerations.set(sourceRoot(track), callGeneration);
                    sources.set(track, { kind: "media", root: sourceRoot(track) });
                    roots.add(sourceRoot(track));
                  }
                }
              }
            }
            for (const [track, recorder] of state.tracks) {
              if (recorder.failed) state.refusedTracks.add(track);
              if (track.readyState === "ended" || !sources.has(track) || recorder.failed) { await recorder.stop(track.readyState === "ended" ? "ended" : "replaced").catch(error => { if (!recorder.failed) warn(error); }); state.tracks.delete(track); }
            }
            for (const [track, metadata] of sources) {
              if (state.stopping) break;
              if (track.kind !== "audio" || track.readyState !== "live" || state.tracks.has(track) || state.refusedTracks.has(track)) continue;
              const enabled = () => {
                if (!track.enabled || track.muted) return false;
                if (!metadata.sender) return true;
                if (metadata.sender.track !== track) return false;
                const transceiver = metadata.pc.getTransceivers().find(t => t.sender === metadata.sender);
                if (transceiver && !["sendonly", "sendrecv"].includes(transceiver.currentDirection)) return false;
                const encodings = metadata.sender.getParameters().encodings;
                return !encodings?.length || encodings.some(e => e.active !== false);
              };
              const sourceTrackId = sourceKey(metadata.root || sourceRoot(track));
              const record = state.sources.get(sourceTrackId) || { sourceTrackId, kind: metadata.kind,
                label: `${metadata.kind}: ${track.label || track.id}`, failed: false };
              state.sources.set(sourceTrackId, record);
              state.recordsByTrack.set(track, record);
              const nextSpanId = () => state.spanIds.size < 64 ? crypto.randomUUID() : null;
              try {
              if (!state.announcedSources.has(sourceTrackId)) await announce(record);
              if (state.stopping) break;
              const id = nextSpanId();
              if (!id) { state.refusedTracks.add(track); if (!record.failed) sourceFailed(record); continue; }
              const recorder = await capture({ stream: new MediaStream([track]), id, sourceTrackId,
                reportOutcomes: message.sourceOutcomes === true, nextSpanId, live: () => track.readyState === "live",
                label: `${metadata.kind}: ${track.label || track.id}`, kind: metadata.kind, enabled,
                origin: message.origin, workletURL: message.workletURL, send, getSharedContext: () => state.context ??= new AudioContext({ sampleRate: 48000 }), warn: (message, details) => details?.kind === "recovered" ? recovered(record.sourceTrackId) : warn(details?.kind === "retry", record.sourceTrackId, message),
                fail: message => sourceFailed(record, message) });
              state.tracks.set(track, recorder);
              } catch (error) { if (!intentional(error)) { state.refusedTracks.add(track); sourceFailed(record); } }
            }
          } catch (error) { fail(error.message); }
          finally { state.scanning = false; }
        };
        state.scan = () => {
          if (state.scanning || state.stopping) return state.scanPromise || Promise.resolve();
          state.scanPromise = scan(); return state.scanPromise;
        };
        state.timer = setInterval(state.scan, 500);
        await state.scan();
        emit(state.token, { type: "ready" });
      } catch (error) { fail(error.message); }
    } else if (message.type === "stop" && active?.token === message.token) {
      const state = active;
      state.stopping = true;
      clearInterval(state.timer);
      await state.scanPromise;
      for (const [track, recorder] of state.tracks) if (recorder.state === "retrying" || recorder.failed) state.sourceFailed(state.recordsByTrack.get(track));
      const results = await Promise.allSettled([...state.tracks.values()].map(recorder => recorder.stop()));
      const error = results.find(result => result.status === "rejected");
      if (error) emit(state.token, { type: "error", message: error.reason.message });
      await Promise.allSettled([...state.sourceUpdates]);
      await state.context?.close();
      emit(state.token, { type: "stopped" });
      active = null;
    }
  });
})();
