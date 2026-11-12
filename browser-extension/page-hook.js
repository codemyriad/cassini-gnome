(() => {
  if (!window.RTCPeerConnection || globalThis.__cassiniPageHook) return;
  // Revision 2 excludes display-share audio from Me and gates sender mute on the render clock.
  // Revision 3 (0.2.28) follows microphone and display lineage through WebAudio, here and in the
  // document-start collector, so automatic recording works on pages that process the microphone.
  // The relay refuses an older hook still alive in a page opened before an update (reload required).
  const hookRevision = 3;
  globalThis.__cassiniPageHook = {protocol:1, revision:hookRevision};
  const peers = new Set(), closedPeers = new WeakSet();
  const original = window.RTCPeerConnection;
  // Capture before wrapping: hook reads must never register peers recursively.
  const methods = Object.fromEntries(["close", "getStats", "getReceivers", "getSenders", "getTransceivers", "createOffer", "createAnswer", "setLocalDescription", "setRemoteDescription", "addTrack", "addTransceiver"]
    .map(name => [name, original.prototype[name]]));
  const getTransceivers = pc => Reflect.apply(methods.getTransceivers, pc, []);
  const getSenders = pc => Reflect.apply(methods.getSenders, pc, []);
  const closed = pc => closedPeers.has(pc) || pc.signalingState === "closed" || pc.connectionState === "closed";
  function retirePeer(pc) {
    if (!closedPeers.has(pc)) {
      closedPeers.add(pc);
      // Preserve receiver roots so DOM aliases cannot bypass terminal closure.
      rememberReceivers(pc);
    }
    peers.delete(pc);
  }
  const peerGenerations = new WeakMap();
  const knownLocalTracks = new WeakSet();
  const knownDisplayTracks = new WeakSet();
  const localGroups = new WeakMap(); // sender -> its Me source identity, across capture restarts
  const knownReceiverRoots = new WeakSet();
  const sourceRoots = new WeakMap();
  const sourceKeys = new WeakMap();
  const receiverGenerations = new WeakMap(), trackGenerations = new WeakMap();
  const nativeRead = globalThis.__cassiniCallEvidenceV1?.read;
  const nativeIdentity = nativeRead?.();
  const documentGeneration = nativeIdentity?.documentGeneration || globalThis.crypto?.randomUUID?.() || "doc-" + Math.random().toString(36).slice(2);
  let nextSource = 0, callGeneration = 0, lastCallGeneration = -1;
  let callAddress = window.location?.href?.split("#", 1)[0];
  function refreshCall() {
    if (nativeRead) {
      const value = nativeRead();
      if (value) {
        const generation = Number(value.hookGeneration);
        if (generation !== callGeneration) { callGeneration = generation; resetEvidence(); }
        return;
      }
    }
    const next = window.location?.href?.split("#", 1)[0];
    if (next !== callAddress) { callAddress = next; callGeneration++; resetEvidence();  }
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
  function sending(sender, pc) {
    const track = sender.track;
    if (!track || track.readyState !== "live" || !track.enabled || track.muted || closed(pc)) return false;
    const transceiver = getTransceivers(pc).find(t => t.sender === sender);
    if (transceiver && !["sendonly", "sendrecv"].includes(transceiver.currentDirection)) return false;
    const encodings = sender.getParameters().encodings;
    return !encodings?.length || encodings.some(e => e.active !== false);
  }
  function rememberReceivers(pc) {
    for (const {receiver} of getTransceivers(pc)) if (receiver?.track) {
      knownReceiverRoots.add(sourceRoot(receiver.track));
      receiverGeneration(receiver, pc);
    }
  }
  // Talk's participant videos carry the receiver's actual track objects, even
  // when every Janus track has the same label/id. DOM text is an untrusted,
  // registration-time display snapshot; it never grants capture eligibility.
  function talkLabel(track) {
    const participants = new Set(), names = new Set();
    for (const media of document.querySelectorAll("audio,video")) {
      if (!(media.srcObject instanceof MediaStream)) continue;
      const tracks = media.srcObject.getAudioTracks();
      if (tracks.length !== 1 || tracks[0] !== track) continue;
      const card = media.closest?.(".video-container[id]");
      const participant = card?.id?.match(/^(?:placeholder-)?container_(.+)_video_incoming$/)?.[1];
      if (!participant) continue;
      participants.add(participant);
      for (const node of card.querySelectorAll(".participant-name")) {
        if (node.closest(".video-container[id]") !== card) continue;
        const name = node.textContent.replace(/\s+/gu, " ").replace(/[\p{Cc}\p{Cf}]/gu, "").trim();
        if (name) names.add(name);
      }
    }
    if (participants.size !== 1 || names.size !== 1) return null;
    return {label:[...names.values().next().value].slice(0, 200).join(""), labelSource:"dom"};
  }
  // Remember microphone and display-share origins before they become senders or previews.
  function isDisplayTrack(track, depth = 0, memo = undefined) {
    if (!track) return false;
    if (capturedDisplay(track)) return true;
    // A WebAudio output is display audio while shared-screen audio reaches it and no captured
    // microphone does; a processed (or mixed) microphone stays Me. Its graph can change: no caching.
    const reach = audioReach(track, depth, memo);
    return !!reach && reach.display && !reach.microphone;
  }
  // getDisplayMedia audio, its clones, or a track reporting a display surface.
  function capturedDisplay(track) {
    const root = sourceRoot(track);
    if (knownDisplayTracks.has(track) || knownDisplayTracks.has(root)) return true;
    try {
      for (const candidate of [track, root]) {
        if (typeof candidate.getSettings === "function" && candidate.getSettings()?.displaySurface) {
          knownDisplayTracks.add(track);
          knownDisplayTracks.add(root);
          return true;
        }
      }
    } catch {}
    return false;
  }
  // WebAudio lineage, the same rules as the native collector's (rtc-evidence.js): a
  // MediaStreamAudioDestinationNode output carries what its graph's sources reach through
  // successful AudioNode connections. AudioParam modulation carries nothing.
  const capturedMicrophones = new WeakSet();
  const audioSources = new WeakMap(), audioInputs = new WeakMap(), audioOutputs = new WeakMap(), audioOutputTracks = new WeakMap();
  const isAudioNode = value => typeof AudioNode === "function" && value instanceof AudioNode;
  // A microphone overrides display exclusion only while it is live, enabled and unmuted, its
  // context renders, and it reaches the output through port 0 (output 0 into input 0), as in
  // the collector. Shared-screen audio counts through any port: excluding it errs toward safety.
  const audibleMicrophone = track => track?.readyState === "live" && track.enabled === true && track.muted !== true;
  function audioReach(track, depth = 0, memo = new Map()) {
    const output = audioOutputTracks.get(sourceRoot(track)) || audioOutputTracks.get(track);
    if (!output) return null;
    // A graph too deep or too wide to walk is unknown: treated as shared-screen audio, never Me.
    if (depth > 8) return { microphone: false, display: true };
    if (memo.has(output)) return memo.get(output) || { microphone: false, display: false };
    memo.set(output, null);
    const reach = { microphone: false, display: false };
    let running = false;
    try { running = output.context?.state === "running"; } catch {}
    // Walk twice: through every edge for display audio, through port-0 edges for microphones.
    for (const micPath of [false, true]) {
      const queue = [output], seen = new Set(queue);
      for (let i = 0; i < queue.length; i++) {
        if (i >= 512) { if (!micPath) reach.display = true; break; }
        const source = audioSources.get(queue[i]);
        if (source) {
          let tracks = source.tracks || [];
          if (source.element) try { tracks = source.element.srcObject?.getAudioTracks?.() || []; } catch { tracks = []; }
          // A stream source renders one of its tracks: it is a microphone only when every
          // track is an audible captured microphone, and display when any track is display.
          if (micPath) {
            if (running && !source.element && tracks.length && tracks.every(input => audibleMicrophone(input) &&
                (capturedMicrophones.has(sourceRoot(input)) || audioReach(input, depth + 1, memo)?.microphone))) reach.microphone = true;
          // Raw display reachability: a nested output's own microphone must not hide
          // shared-screen audio that a later splitter or port routes on alone.
          } else if (tracks.some(input => capturedDisplay(input) || audioReach(input, depth + 1, memo)?.display)) reach.display = true;
        }
        for (const edge of audioInputs.get(queue[i]) || []) {
          if (micPath && (edge.output !== 0 || edge.input !== 0)) continue;
          if (!seen.has(edge.from)) { seen.add(edge.from); queue.push(edge.from); }
        }
      }
    }
    memo.set(output, reach);
    return reach;
  }
  const audioContextPrototype = globalThis.AudioContext?.prototype;
  const rememberAudioSource = node => {
    try {
      if (node?.mediaStream) audioSources.set(node, { tracks: node.mediaStream.getAudioTracks() });
      else if (node?.mediaElement) audioSources.set(node, { element: node.mediaElement });
    } catch {}
    return node;
  };
  const rememberAudioOutput = node => {
    try { for (const track of node.stream.getAudioTracks()) audioOutputTracks.set(track, node); } catch {}
    return node;
  };
  for (const [factory, Node, remember] of [["createMediaStreamSource", "MediaStreamAudioSourceNode", rememberAudioSource],
    ["createMediaElementSource", "MediaElementAudioSourceNode", rememberAudioSource],
    ["createMediaStreamDestination", "MediaStreamAudioDestinationNode", rememberAudioOutput]]) {
    const create = audioContextPrototype?.[factory];
    if (typeof create === "function") audioContextPrototype[factory] = function (...args) { return remember(Reflect.apply(create, this, args)); };
    if (typeof globalThis[Node] === "function") globalThis[Node] = new Proxy(globalThis[Node], {
      construct(target, args, newTarget) { return remember(Reflect.construct(target, args, newTarget)); }
    });
  }
  const audioNodePrototype = globalThis.AudioNode?.prototype;
  // WebIDL unsigned long; anything but a number is unknown (-1, matching every index).
  const audioIndex = (value, fallback) => value === undefined ? fallback : typeof value === "number" ? value >>> 0 : -1;
  if (typeof audioNodePrototype?.connect === "function" && typeof audioNodePrototype?.disconnect === "function") {
    const connect = audioNodePrototype.connect, disconnect = audioNodePrototype.disconnect;
    const edges = (map, node) => { if (!map.has(node)) map.set(node, []); return map.get(node); };
    audioNodePrototype.connect = function (...args) {
      const result = Reflect.apply(connect, this, args);
      try {
        // Only supplied slots: an omitted index never reads a page prototype property.
        const slot = i => i < args.length ? args[i] : undefined;
        const to = slot(0), output = audioIndex(slot(1), 0), input = audioIndex(slot(2), 0);
        if (isAudioNode(to) && output >= 0 && input >= 0 && !edges(audioOutputs, this).some(e => e.to === to && e.output === output && e.input === input)) {
          edges(audioOutputs, this).push({ to, output, input });
          edges(audioInputs, to).push({ from: this, output, input });
        }
      } catch {}
      return result;
    };
    audioNodePrototype.disconnect = function (...args) {
      const result = Reflect.apply(disconnect, this, args);
      try {
        let to = null, output = -1, input = -1;
        const slot = i => i < args.length ? args[i] : undefined;
        if (args.length && isAudioNode(slot(0))) { to = slot(0); if (args.length > 1) output = audioIndex(slot(1), -1); if (args.length > 2) input = audioIndex(slot(2), -1); }
        else if (args.length && typeof AudioParam === "function" && slot(0) instanceof AudioParam) return result;
        else if (args.length) output = audioIndex(slot(0), -1);
        const removed = e => (to === null || e.to === to) && (output < 0 || e.output === output) && (input < 0 || e.input === input);
        const outputs = audioOutputs.get(this) || [];
        for (const edge of outputs.filter(removed)) {
          const inputs = audioInputs.get(edge.to) || [];
          audioInputs.set(edge.to, inputs.filter(e => !(e.from === this && e.output === edge.output && e.input === edge.input)));
        }
        audioOutputs.set(this, outputs.filter(e => !removed(e)));
      } catch {}
      return result;
    };
  }
  const devices = navigator.mediaDevices;
  if (devices?.getUserMedia) {
    const getUserMedia = devices.getUserMedia;
    devices.getUserMedia = function (...args) {
      return Reflect.apply(getUserMedia, this, args).then(stream => {
        stream.getAudioTracks().forEach(track => { knownLocalTracks.add(track); capturedMicrophones.add(track); });
        return stream;
      });
    };
  }
  if (devices?.getDisplayMedia) {
    const getDisplayMedia = devices.getDisplayMedia;
    devices.getDisplayMedia = function (...args) {
      return Reflect.apply(getDisplayMedia, this, args).then(stream => {
        stream.getAudioTracks().forEach(track => knownDisplayTracks.add(track));
        return stream;
      });
    };
  }
  // MediaStream.clone() clones its tracks natively, past the track clone wrapper below.
  const streamPrototype = globalThis.MediaStream?.prototype;
  if (streamPrototype?.clone && streamPrototype?.getAudioTracks) {
    const cloneStream = streamPrototype.clone;
    streamPrototype.clone = function (...args) {
      const stream = Reflect.apply(cloneStream, this, args);
      try {
        const from = this.getAudioTracks(), to = stream.getAudioTracks();
        if (from.length === to.length) from.forEach((track, index) => inherit(to[index], track));
        // Unpaired clones of a stream holding display audio must never become Me.
        else if (from.some(isDisplayTrack)) to.forEach(track => knownDisplayTracks.add(track));
      } catch {}
      return stream;
    };
  }
  function inherit(track, from) {
    sourceRoots.set(track, sourceRoot(from));
    if (knownLocalTracks.has(sourceRoot(from))) knownLocalTracks.add(track);
    if (knownDisplayTracks.has(sourceRoot(from)) || knownDisplayTracks.has(from)) knownDisplayTracks.add(track);
  }
  // Sender-level mute keeps feeding the microphone into capture; recorders note each change on
  // the render clock at once, so audio buffered while muted never passes after unmuting.
  const gateChanged = () => { for (const recorder of active?.tracks.values() || []) try { recorder.observe?.(); } catch {} };
  const senderPrototype = globalThis.RTCRtpSender?.prototype;
  for (const name of ["replaceTrack", "setParameters"]) {
    const method = senderPrototype?.[name];
    if (typeof method !== "function") continue;
    senderPrototype[name] = function (...args) {
      gateChanged();
      const value = Reflect.apply(method, this, args);
      gateChanged();
      if (typeof value?.then === "function") value.then(gateChanged, gateChanged);
      return value;
    };
  }
  const trackPrototype = globalThis.MediaStreamTrack?.prototype;
  if (trackPrototype?.clone) {
    const clone = trackPrototype.clone;
    trackPrototype.clone = function (...args) {
      const track = Reflect.apply(clone, this, args);
      inherit(track, this);
      return track;
    };
  }
  const remember = (pc, created = false) => {
    refreshCall();
    if (closed(pc)) { retirePeer(pc); return; }
    if (peers.has(pc) || peerGenerations.has(pc)) return;
    peers.add(pc);
    // Read-only observation cannot prove an unknown peer belongs to a later
    // in-page call. A new constructor or fresh applied remote description can.
    peerGenerations.set(pc, created || callGeneration === 0 ? callGeneration : null);
    pc.addEventListener("connectionstatechange", () => {
      if (closed(pc)) retirePeer(pc);
      discover();
    });
    pc.addEventListener("iceconnectionstatechange", () => discover());
  };
  window.RTCPeerConnection = new Proxy(original, {
    construct(target, args, newTarget) { const pc = Reflect.construct(target, args, newTarget); remember(pc, true); return pc; }
  });
  if (window.webkitRTCPeerConnection === original) window.webkitRTCPeerConnection = window.RTCPeerConnection;
  original.prototype.close = function (...args) {
    retirePeer(this);
    const value = Reflect.apply(methods.close, this, args);
    discover();
    return value;
  };
  // Recovery for connections created by a cached constructor or before the extension loaded.
  for (const name of ["getStats", "getReceivers", "getSenders", "getTransceivers", "createOffer", "createAnswer", "setLocalDescription", "setRemoteDescription", "addTrack", "addTransceiver"]) {
    const method = methods[name];
    original.prototype[name] = function (...args) {
      remember(this);
      const generation = callGeneration;
      const value = Reflect.apply(method, this, args);
      if (name === "setRemoteDescription" && typeof value?.then === "function") {
        value.then(() => {
          refreshCall();
          // A new call may reuse the same peer/receiver, but an old delayed
          // negotiation and read-only stats cannot carry A's sources into B.
          if (closed(this) || generation !== callGeneration || peerGenerations.get(this) === generation) return;
          peerGenerations.set(this, generation);
          for (const { receiver } of getTransceivers(this)) {
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
  const connected = pc => !closed(pc) &&
    (pc.connectionState === "connected" || ["connected", "completed"].includes(pc.iceConnectionState));
  function discoverySources() {
    const sources = new Set(), receivers = new Map();
    for (const pc of peers) {
      if (closed(pc)) { retirePeer(pc); continue; }
      rememberReceivers(pc);
      if (!connected(pc)) continue;
      for (const transceiver of getTransceivers(pc)) {
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
  let active = null, waitingHandshake = null;
  const emit = (token, value) => window.postMessage({ channel: "cassini-page", token, ...value }, "*");
  window.addEventListener("message", async event => {
    if (event.isTrusted !== true || event.source !== window || event.data?.channel !== "cassini-control") return;
    const message = event.data;
    if (message.type === "handshake") {
      if (message.protocol === 1 && typeof message.instanceId === "string" && message.instanceId.length > 0 && message.instanceId.length <= 128) {
        // A replacement relay cannot start a new capture until the departing
        // relay's token has drained. Only the newest instance needs the reply.
        if (active) waitingHandshake = message.instanceId;
        else emit(null, {type:"hookReady", protocol:1, revision:hookRevision, instanceId:message.instanceId});
      }
      return;
    }
    if (message.type === "discover") { discover(true); return; }
    if (message.type === "ack" && active?.token === message.token) {
      const pending = active.pending.get(message.request);
      if (pending) { clearTimeout(pending.timer); active.pending.delete(message.request); message.error ? pending.reject(Object.assign(new Error(message.error), {code: message.code, scope: message.scope, ...(message.retired === true ? {retired:true} : {})})) : pending.resolve(); }
      return;
    }
    if (message.type === "start" && !active) {
      refreshCall();
      const state = { token: message.token, callGeneration, sourceOutcomes: message.sourceOutcomes === true, pending: new Map(), sourceUpdates: new Set(), announcedSources: new Set(), tracks: new Map(), recordsByTrack: new WeakMap(), warnings: new Map(), refusedTracks: new WeakSet(), sources: new Map(), spanIds: new Set(), localGroups: new Set(), senderGroups: new Map(), groupTracks: new Map(), next: 0, stopping: false, scanning: false };
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
            const sources = new Map(), roots = new Set(), localSenders = [];
            for (const pc of peers) {
              rememberReceivers(pc);
              for (const {receiver} of getTransceivers(pc)) if (receiver?.track) roots.add(sourceRoot(receiver.track));
              if (closed(pc)) { retirePeer(pc); continue; }
              for (const transceiver of getTransceivers(pc)) {
                const receiver = transceiver.receiver;
                const proof = receiver && packetProof.get(receiver);
                if (connected(pc) && ["recvonly", "sendrecv"].includes(transceiver.currentDirection) && receiver?.track &&
                    proof?.pc === pc && proof.track === receiver.track && receiverGeneration(receiver, pc) === callGeneration) {
                  sources.set(receiver.track, { kind: "remote", root: receiver });
                }
              }
              for (const sender of getSenders(pc)) if (sender.track) {
                if (isDisplayTrack(sender.track)) {
                  roots.add(sourceRoot(sender.track));
                  continue;
                }
                knownLocalTracks.add(sender.track);
                knownLocalTracks.add(sourceRoot(sender.track));
                if (message.captureLocal === false || peerGenerations.get(pc) !== callGeneration) continue;
                localSenders.push({ sender, pc });
              }
            }
            // One Me source per microphone: the same track or clones of it sent on several
            // peers is one voice, also after a replaceTrack makes two senders converge.
            // A sender keeps its source across replaceTrack (mute, device) and capture restarts,
            // unless it switched to a microphone its group's other senders do not send.
            const assign = ({ sender, pc }, group) => {
              if (!state.localGroups.has(group)) { group.owners = new Map(); state.localGroups.add(group); }
              const current = state.senderGroups.get(sender);
              if (current && current !== group) current.owners.delete(sender);
              state.senderGroups.set(sender, group);
              localGroups.set(sender, group);
              group.owners.set(sender, { pc, track: sender.track });
            };
            const previousGroup = sender => state.senderGroups.get(sender) || localGroups.get(sender);
            // Senders still on their source's microphone claim it first, so the order of
            // senders never decides which one keeps the identity.
            const switched = [];
            for (const entry of localSenders) {
              const previous = previousGroup(entry.sender);
              if (previous && previous.mic === sourceRoot(entry.sender.track)) assign(entry, previous);
              else switched.push(entry);
            }
            for (const entry of switched) {
              const { sender } = entry, mic = sourceRoot(sender.track);
              // The microphone each other sender of a group sends now (or resumes after a null mute).
              const otherMics = candidate => [...candidate.owners].flatMap(([other, owner]) => {
                const track = other.track || owner.track;
                return other === sender || !track || track.readyState === "ended" || isDisplayTrack(track) || closed(owner.pc) ? [] : [sourceRoot(track)];
              });
              let group = null;
              for (const candidate of state.localGroups) {
                const others = otherMics(candidate);
                // A microphone handed to another peer after its last sender closed keeps its Me source.
                if (others.includes(mic) || (!others.length && candidate.mic === mic)) { group = candidate; break; }
              }
              const previous = previousGroup(sender);
              if (!group && previous && !(state.localGroups.has(previous) && otherMics(previous).length)) group = previous;
              if (!group) { group = { kind: "local" }; group.root = group; }
              if (!state.localGroups.has(group) || !otherMics(group).length) group.mic = mic;
              assign(entry, group);
            }
            // Preserve the same source across replaceTrack(null)/restore mute cycles; a sender
            // leaves its source only when its peer closes or its last track ends.
            for (const group of state.localGroups) {
              for (const [sender, owner] of group.owners)
                if (owner.track.readyState === "ended" || isDisplayTrack(owner.track) || closed(owner.pc) || state.senderGroups.get(sender) !== group) {
                  group.owners.delete(sender);
                  if (state.senderGroups.get(sender) === group) state.senderGroups.delete(sender);
                }
              if (!group.owners.size) { state.localGroups.delete(group); state.groupTracks.delete(group); continue; }
              const candidates = [...new Set([...group.owners.values()].map(owner => owner.track))].filter(track => track.readyState === "live");
              const transmitting = track => [...group.owners].some(([sender, owner]) => sender.track === track && sending(sender, owner.pc));
              let track = state.groupTracks.get(group);
              if (!candidates.includes(track) || (!transmitting(track) && candidates.some(transmitting))) track = candidates.find(transmitting) || candidates[0];
              if (!track) continue;
              state.groupTracks.set(group, track);
              sources.set(track, group);
              for (const owner of group.owners.values()) roots.add(sourceRoot(owner.track));
            }
            for (const track of sources.keys()) roots.add(sourceRoot(track));
            for (const media of document.querySelectorAll("audio,video")) {
              if (media.srcObject instanceof MediaStream) {
                for (const track of media.srcObject.getAudioTracks()) {
                  // A removed sender or display-share preview must not reappear as standalone page media.
                  if (!roots.has(sourceRoot(track)) && !knownReceiverRoots.has(sourceRoot(track)) && !knownLocalTracks.has(sourceRoot(track)) && !isDisplayTrack(track) &&
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
              // A track whose source changed (page media now sent as Me, a microphone adopted by
              // another Me source) restarts under that source: an old recorder keeps its old identity.
              const owner = sources.get(track);
              const moved = owner && state.recordsByTrack.get(track)?.sourceTrackId !== sourceKey(owner.root || sourceRoot(track));
              if (track.readyState === "ended" || !owner || recorder.failed || moved) { await recorder.stop(track.readyState === "ended" ? "ended" : "replaced").catch(error => { if (!recorder.failed) warn(error); }); state.tracks.delete(track); }
            }
            for (const [track, metadata] of sources) {
              if (state.stopping) break;
              if (track.kind !== "audio" || track.readyState !== "live" || state.tracks.has(track) || state.refusedTracks.has(track)) continue;
              // Me is audible while any of its current senders transmits this microphone.
              const enabled = () => {
                if (!track.enabled || track.muted) return false;
                if (!metadata.owners) return true;
                for (const [sender, owner] of metadata.owners)
                  if (sender.track && sourceRoot(sender.track) === sourceRoot(track) && sending(sender, owner.pc)) return true;
                return false;
              };
              const sourceTrackId = sourceKey(metadata.root || sourceRoot(track));
              const record = state.sources.get(sourceTrackId) || { sourceTrackId, kind: metadata.kind,
                label: `${metadata.kind}: ${track.label || track.id}`,
                ...(metadata.kind === "remote" ? talkLabel(track) : null), failed: false };
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
                label: record.label, labelSource: record.labelSource, kind: metadata.kind, enabled,
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
    } else if (message.type === "stop" && active?.token === message.token && !active.stopping) {
      const state = active;
      state.stopping = true;
      clearInterval(state.timer);
      await state.scanPromise;
      for (const [track, recorder] of state.tracks) if (recorder.state === "retrying" || recorder.failed) state.sourceFailed(state.recordsByTrack.get(track));
      const results = await Promise.allSettled([...state.tracks.values()].map(recorder => recorder.stop()));
      const error = results.find(result => result.status === "rejected");
      if (error) emit(state.token, { type: "error", message: error.reason.message });
      await Promise.allSettled([...state.sourceUpdates]);
      const context = state.context; state.context = null;
      await context?.close();
      emit(state.token, { type: "stopped" });
      active = null;
      if (waitingHandshake) {
        emit(null, {type:"hookReady", protocol:1, revision:hookRevision, instanceId:waitingHandshake});
        waitingHandshake = null;
      }
    }
  });
})();
