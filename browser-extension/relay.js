(() => {
  // A second injection in the same live isolated world must not duplicate
  // listeners or replace an active recording. Old extension realms fail valid().
  const previous = globalThis.__cassiniRelay;
  if (previous?.valid()) { previous.discover(); return; }
  previous?.retire();
  const observerDocumentStart = globalThis.__cassiniRepairInjection !== true;
  const protocol = 1;
  // A page opened before an update keeps its old hook (reinjection never replaces it), and a
  // revision 1 hook would record screen-share audio as Me, and a revision 2 page (with its
  // document-start collector) loses the microphone through WebAudio, so automatic recording never
  // starts on Microsoft Teams: that page needs a reload.
  const hookRevision = 3;
  let instanceId = null, hookReady = false, handshakeComplete = false, retired = false, handshakeTimer = null;
  const waiters = new Set();
  const listeners = new Map();
  const listen = (name, fn) => { listeners.set(name, fn); window.addEventListener(name, fn); };
  const valid = () => { try { return !retired && !!chrome.runtime.id && (!chrome.runtime.getManifest || !!chrome.runtime.getManifest()); } catch { return false; } };
  const moduleURL = chrome.runtime.getURL("capture.js"), workletURL = chrome.runtime.getURL("pcm-worklet.js");
  function settle() {
    const result = { connected: hookReady && valid(), protocol, instanceId, needsReload: !hookReady };
    for (const respond of waiters) respond(result);
    waiters.clear();
  }
  function cleanup() {
    for (const [name, fn] of listeners) window.removeEventListener?.(name, fn);
    listeners.clear();
    try { chrome.runtime.onMessage.removeListener(runtimeMessage); } catch {}
    try { port?.disconnect(); } catch {}
    port = null;
  }
  function retire() {
    if (retired) return;
    retired = true; hookReady = false;
    clearTimeout(handshakeTimer); settle();
    // Resolve every admitted request before stopping the old hook. The draining
    // listener also rejects requests emitted by a scan already in flight.
    if (token || drainingToken) {
      if (token) {
        drainingToken = token; token = null;
        for (const request of pending) control({type:"ack",token:drainingToken,request,error:"The extension reloaded. Audio recorded so far was saved."});
        control({type:"stop",token:drainingToken});
      }
      handshakeTimer = setTimeout(cleanup, 10000);
    } else cleanup();
  }
  function send(message) {
    if (['relayHello','relayReady','relayUnavailable'].includes(message.type)) message={...message,observerDocumentStart};
    try { port?.postMessage(message); }
    catch { retire(); }
  }
  globalThis.__cassiniRelay = {valid, retire, observerDocumentStart, discover: () => control({type:"discover"})};
  let port = null, token = null, drainingToken = null, pendingStart = null;
  const pending = new Set();
  let sourceCount, heard = false, hidden = false;
  const control = message => window.postMessage({ channel: "cassini-control", ...message,
    moduleURL, workletURL }, "*");
  function receive(message) {
    if (message.type === "relayHello") {
      if (message.protocol !== protocol || typeof message.instanceId !== "string") { retire(); return; }
      instanceId = message.instanceId; hookReady = false; handshakeComplete = false;
      send({type:"relayHello", protocol, instanceId});
      control({type:"handshake", protocol, instanceId});
      clearTimeout(handshakeTimer);
      handshakeTimer = setTimeout(() => {
        if (!hookReady) { handshakeComplete = true; send({type:"relayUnavailable", protocol, instanceId}); settle(); }
      }, 1500);
      return;
    }
    if (message.type === "discover") { control({ type: "discover" }); return; }
    if (message.type === "start") {
      if (drainingToken) { pendingStart = message; return; }
      token = message.token;
    }
    if (message.type === "stop" && pendingStart?.token === message.token) {
      pendingStart = null; send({ type: "stopped" }); return;
    }
    if (!token) return;
    if (message.type === "ack") pending.delete(message.request);
    control(message);
  }
  function disconnected(connected) {
    if (port !== connected) return;
    if (token) {
      drainingToken = token;
      for (const request of pending) control({ type: "ack", token, request, error: "Cassini disconnected. Audio recorded so far was saved." });
      control({ type: "stop", token });
    }
    pending.clear(); pendingStart = null; token = null; port = null;
    hookReady = false; handshakeComplete = false; instanceId = null;
    if (!valid()) retire();
  }
  function connect() {
    if (port || retired) return;
    let connected;
    try { connected = chrome.runtime.connect({ name: "cassini-frame" }); }
    catch { retire(); return; }
    port = connected;
    connected.onMessage.addListener(receive);
    // Reconnect asks the hook for fresh evidence; never replay a stale latch.
    control({ type: "discover" });
    connected.onDisconnect.addListener(() => {
      try { void chrome.runtime.lastError; } catch {}
      disconnected(connected);
    });
  }
  function runtimeMessage(message, _sender, respond) {
    if (message.type !== "cassini-connect") return;
    if (!valid()) { retire(); return; }
    connect(); control({ type: "discover" });
    if (handshakeComplete) respond({connected:hookReady, protocol, instanceId, needsReload:!hookReady});
    else { waiters.add(respond); return true; }
  }
  chrome.runtime.onMessage.addListener(runtimeMessage);
  connect();
  listen("pagehide", event => {
    if(event?.isTrusted!==true)return;
    hidden = true; sourceCount = 0; heard = false;
    send({ type: "pageState", state: "navigating" });
    send({ type: "discovery", sourceCount, heard });
  });
  listen("pageshow", event => {
    if(event?.isTrusted!==true)return;
    hidden = false;
    // The cached realm may thaw before its queued onDisconnect runs. Never
    // send through that stale port or retire an otherwise valid relay for it.
    if (event?.persisted && port) {
      const stale = port;
      disconnected(stale);
      try { stale.disconnect(); } catch {}
    }
    connect();
    send({ type: "pageState", state: "present" });
    control({ type: "discover" });
  });
  listen("message", event => {
    // isTrusted rejects synthetic dispatchEvent, not page postMessage forgery.
    // Admission uses the unconditionally claimed immutable native global;
    // discovery only wakes a browser-bound read of it, never supplies proof.
    if (event.isTrusted !== true || event.source !== window || event.data?.channel !== "cassini-page") return;
    const { type, packet, request } = event.data;
    if (retired && !(drainingToken && event.data.token === drainingToken)) return;
    if (type === "hookReady") {
      if (event.data.protocol !== protocol || event.data.instanceId !== instanceId || !instanceId) return;
      if(hookReady&&handshakeComplete)return;
      if (!(event.data.revision >= hookRevision)) {
        if (handshakeComplete) return;
        handshakeComplete = true; clearTimeout(handshakeTimer);
        send({type:"relayUnavailable", protocol, instanceId}); settle(); return;
      }
      hookReady = true; handshakeComplete = true; clearTimeout(handshakeTimer);
      send({type:"relayReady", protocol, instanceId}); settle();
      control({type:"discover"}); return;
    }
    if (type === "discovery") {
      if (hidden) return;
      const count = event.data.sourceCount;
      if (Number.isInteger(count) && count >= 0 && count <= 64) {
        sourceCount = count;
        heard = event.data.heard === true;
        const generations = {};
        for (const key of ["documentGeneration", "callGeneration"])
          if (typeof event.data[key] === "string" && event.data[key].length > 0 && event.data[key].length <= 200) generations[key] = event.data[key];
        send({ type, sourceCount, heard, ...generations });
      }
      return;
    }
    if (drainingToken && event.data.token === drainingToken && type === "packet") {
      control({ type: "ack", token: drainingToken, request, error: "Cassini disconnected. Audio recorded so far was saved." });
      return;
    }
    if (drainingToken && event.data.token === drainingToken && type === "stopped") {
      drainingToken = null;
      if (retired) { clearTimeout(handshakeTimer); cleanup(); return; }
      const next = pendingStart; pendingStart = null;
      if (next && port) receive(next);
      return;
    }
    if (!token || event.data.token !== token) return;
    // Page code is untrusted. It may supply audio, never filesystem paths or host commands.
    if (type === "packet" && ["track", "audio", "trackEnd", "sources"].includes(packet?.type)) {
      if (packet.type === "track" && typeof packet.label === "string") packet.label = [...packet.label.replace(/[\u0000-\u001f\u007f-\u009f]/g, "")].slice(0, 200).join("");
      if (JSON.stringify(packet).length <= 400000) { pending.add(request); send({ type, packet, request }); }
    } else if (["ready", "stopped", "error", "warning"].includes(type)) {
      const details = {};
      if (event.data.transient === true) details.transient = true;
      const source = event.data.sourceTrackId;
      if (typeof source === "string" && source.length > 0 && [...source].reduce((n,c)=>n+(c.codePointAt(0)<=127?1:c.codePointAt(0)<=2047?2:c.codePointAt(0)<=65535?3:4),0)<=200) {
        details.sourceTrackId = source;
        if (event.data.recovered === true) details.recovered = true;
        if (typeof event.data.message === "string") details.message = [...event.data.message.replace(/[\u0000-\u001f\u007f-\u009f]/g, "")].slice(0, 2000).join("");
      }
      if (Array.isArray(event.data.discoveredSources)) details.discoveredSources = event.data.discoveredSources.filter(s =>
        s && typeof s.sourceTrackId === "string" && s.sourceTrackId.length > 0 && s.sourceTrackId.length <= 200 &&
        ["local", "remote", "media", "tab"].includes(s.kind)).map(s => ({ sourceTrackId: s.sourceTrackId, kind: s.kind,
          label: typeof s.label === "string" ? [...s.label.replace(/[\u0000-\u001f\u007f-\u009f]/g, "")].slice(0, 200).join("") : s.kind, ...(s.labelSource != null ? {labelSource:s.labelSource} : {}), failed: s.failed === true }));
      send({ type, ...details });
      if (type === "stopped") { token = null; pending.clear(); }
    }
  });
})();
