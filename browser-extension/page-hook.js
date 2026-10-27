(() => {
  if (!window.RTCPeerConnection) return;
  const peers = new Set();
  const knownLocalTracks = new WeakSet();
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
      if (knownLocalTracks.has(this)) knownLocalTracks.add(track);
      return track;
    };
  }
  const original = window.RTCPeerConnection;
  const remember = pc => {
    if (peers.has(pc)) return;
    peers.add(pc);
    pc.addEventListener("connectionstatechange", () => {
      if (pc.connectionState === "closed") peers.delete(pc);
    });
  };
  window.RTCPeerConnection = new Proxy(original, {
    construct(target, args, newTarget) { const pc = Reflect.construct(target, args, newTarget); remember(pc); return pc; }
  });
  if (window.webkitRTCPeerConnection === original) window.webkitRTCPeerConnection = window.RTCPeerConnection;
  const close = original.prototype.close;
  original.prototype.close = function (...args) { peers.delete(this); return Reflect.apply(close, this, args); };
  // Recovery for connections created by a cached constructor or before the extension loaded.
  for (const name of ["getStats", "createOffer", "createAnswer", "setLocalDescription", "setRemoteDescription", "addTrack", "addTransceiver"]) {
    const method = original.prototype[name];
    original.prototype[name] = function (...args) { remember(this); return Reflect.apply(method, this, args); };
  }
  let active = null;
  const emit = (token, value) => window.postMessage({ channel: "cassini-page", token, ...value }, "*");
  window.addEventListener("message", async event => {
    if (event.source !== window || event.data?.channel !== "cassini-control") return;
    const message = event.data;
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
            for (const media of document.querySelectorAll("audio,video")) {
              if (media.srcObject instanceof MediaStream) {
                for (const track of media.srcObject.getAudioTracks()) {
                  // A removed sender must not reappear as the page's raw microphone preview.
                  if (!sources.has(track) && !knownLocalTracks.has(track)) {
                    sources.set(track, { kind: "media" });
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
