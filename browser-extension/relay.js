(() => {
  let port = null, token = null, drainingToken = null, pendingStart = null;
  const pending = new Set();
  let sourceCount, heard = false, hidden = false;
  const control = message => window.postMessage({ channel: "cassini-control", ...message,
    moduleURL: chrome.runtime.getURL("capture.js"), workletURL: chrome.runtime.getURL("pcm-worklet.js") }, "*");
  function receive(message) {
    if (message.type === "discover") { control({ type: "discover" }); return; }
    if (message.type === "start") {
      if (drainingToken) { pendingStart = message; return; }
      token = message.token;
    }
    if (message.type === "stop" && pendingStart?.token === message.token) {
      pendingStart = null; port?.postMessage({ type: "stopped" }); return;
    }
    if (!token) return;
    if (message.type === "ack") pending.delete(message.request);
    control(message);
  }
  function connect() {
    if (port) return;
    const connected = chrome.runtime.connect({ name: "cassini-frame" });
    port = connected;
    connected.onMessage.addListener(receive);
    // Reconnect asks the hook for fresh evidence; never replay a stale latch.
    control({ type: "discover" });
    connected.onDisconnect.addListener(() => {
      if (port !== connected) return;
      if (token) {
        drainingToken = token;
        for (const request of pending) control({ type: "ack", token, request, error: "Cassini disconnected. Audio recorded so far was saved." });
        control({ type: "stop", token });
      }
      pending.clear(); pendingStart = null; token = null; port = null;
    });
  }
  chrome.runtime.onMessage.addListener((message, _sender, respond) => {
    if (message.type !== "cassini-connect") return;
    connect(); control({ type: "discover" }); respond({ connected: true });
  });
  connect();
  window.addEventListener("pagehide", () => {
    hidden = true; sourceCount = 0; heard = false;
    try { port?.postMessage({ type: "pageState", state: "navigating" }); } catch {}
    try { port?.postMessage({ type: "discovery", sourceCount, heard }); } catch {}
  });
  window.addEventListener("pageshow", () => {
    hidden = false; connect();
    try { port?.postMessage({ type: "pageState", state: "present" }); } catch {}
    control({ type: "discover" });
  });
  window.addEventListener("message", event => {
    if (event.source !== window || event.data?.channel !== "cassini-page") return;
    const { type, packet, request } = event.data;
    if (type === "discovery") {
      if (hidden) return;
      const count = event.data.sourceCount;
      if (Number.isInteger(count) && count >= 0 && count <= 64) {
        sourceCount = count;
        heard = event.data.heard === true;
        const generations = {};
        for (const key of ["documentGeneration", "callGeneration"])
          if (typeof event.data[key] === "string" && event.data[key].length > 0 && event.data[key].length <= 200) generations[key] = event.data[key];
        try { port?.postMessage({ type, sourceCount, heard, ...generations }); } catch {}
      }
      return;
    }
    if (drainingToken && event.data.token === drainingToken && type === "packet") {
      control({ type: "ack", token: drainingToken, request, error: "Cassini disconnected. Audio recorded so far was saved." });
      return;
    }
    if (drainingToken && event.data.token === drainingToken && type === "stopped") {
      drainingToken = null;
      const next = pendingStart; pendingStart = null;
      if (next && port) receive(next);
      return;
    }
    if (!token || event.data.token !== token) return;
    // Page code is untrusted. It may supply audio, never filesystem paths or host commands.
    if (type === "packet" && ["track", "audio", "trackEnd", "sources"].includes(packet?.type)) {
      if (packet.type === "track" && typeof packet.label === "string") packet.label = [...packet.label.replace(/[\u0000-\u001f\u007f-\u009f]/g, "")].slice(0, 200).join("");
      if (JSON.stringify(packet).length <= 400000) { pending.add(request); port?.postMessage({ type, packet, request }); }
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
          label: typeof s.label === "string" ? [...s.label.replace(/[\u0000-\u001f\u007f-\u009f]/g, "")].slice(0, 200).join("") : s.kind, failed: s.failed === true }));
      port?.postMessage({ type, ...details });
      if (type === "stopped") { token = null; pending.clear(); }
    }
  });
})();
