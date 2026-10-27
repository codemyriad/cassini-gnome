async function hasOffscreenDocument() {
  return (await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] })).length > 0;
}

const frames = new Set();
let active = null;
let lastStatus = { recording: false, message: "Ready. Choose WebRTC tracks or Tab audio." };

class NativeHost {
  constructor(onFailure) {
    this.pending = [];
    this.closed = false;
    this.port = chrome.runtime.connectNative("com.codemyriad.cassini");
    this.port.onMessage.addListener(message => {
      const request = this.pending.shift();
      if (request) {
        clearTimeout(request.timer);
        message.type === "error" ? request.reject(new Error(message.message)) : request.resolve(message);
      }
      if (message.type === "error") onFailure(message.message);
    });
    this.port.onDisconnect.addListener(() => {
      const message = chrome.runtime.lastError?.message || "The local recording host disconnected.";
      for (const request of this.pending.splice(0)) { clearTimeout(request.timer); request.reject(new Error(message)); }
      if (!this.closed) onFailure(message);
    });
  }
  send(packet) {
    if (this.closed) return Promise.reject(new Error("Recording host is closed."));
    if (this.pending.length >= 512) return Promise.reject(new Error("The recording host cannot keep up with audio."));
    return new Promise((resolve, reject) => {
      const request = { resolve, reject, timer: setTimeout(() => {
        reject(new Error("The recording host stopped responding."));
        this.close();
      }, 10000) };
      this.pending.push(request);
      try { this.port.postMessage(packet); }
      catch (error) { clearTimeout(request.timer); this.pending.pop(); reject(error); }
    });
  }
  close() {
    this.closed = true;
    for (const request of this.pending.splice(0)) { clearTimeout(request.timer); request.reject(new Error("Recording host closed.")); }
    this.port.disconnect();
  }
}

function status() {
  return active ? { recording: true, stopping: active.stopping, tracks: active.tracks,
    message: active.warning || (!active.tracks ? "Waiting for audio sources. Join the call or choose Tab audio." : `${active.stopping ? "Saving" : "Recording"}: ${active.mode === "tracks" ? "WebRTC tracks" : "tab audio"} · ${active.tracks} sources`), path: active.path } : lastStatus;
}

function failure(message) {
  if (active) { active.problem ??= message; void stop(); }
}

async function stop() {
  const session = active;
  if (!session || session.stopping) return;
  session.stopping = true;
  clearTimeout(session.discoveryTimer);
  try {
    if (session.mode === "tracks") {
      await Promise.all([...session.frames].map(frame => new Promise(resolve => {
        const timer = setTimeout(() => { session.problem ??= "A meeting frame did not finish sending audio."; resolve(); }, 15000);
        frame.finished = () => { clearTimeout(timer); frame.finished = null; resolve(); };
        try { frame.port.postMessage({ type: "stop", token: session.token }); }
        catch { frame.finished(); }
      })));
    } else if (await hasOffscreenDocument()) {
      const response = await chrome.runtime.sendMessage({ target: "offscreen", type: "stop" });
      if (response?.error) session.problem ??= response.error;
    }
    // Native messages are processed in order, so the stop acknowledgement follows all audio writes.
    const result = await session.host.send({ type: "stop", problem: session.problem });
    lastStatus = { recording: false, path: result.path, message: session.problem || result.problem || "Recording saved locally." };
  } catch (error) {
    lastStatus = { recording: false, path: session.path, message: session.problem || error.message };
  } finally {
    session.host?.close();
    if (session.mode === "tab" && await hasOffscreenDocument()) await chrome.offscreen.closeDocument();
    if (active === session) active = null;
    await chrome.action.setBadgeText({ text: "" });
  }
}

async function start(mode) {
  if (active) throw new Error("A recording is already running.");
  if (!["tracks", "tab"].includes(mode)) throw new Error("Unknown recording mode.");
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !/^https?:/.test(tab.url || "")) throw new Error("Open a meeting in an HTTP or HTTPS tab first.");
  const session = { mode, tabId: tab.id, token: crypto.randomUUID(), origin: Date.now(),
    frames: new Set(), tracks: 0, stopping: false, problem: null };
  active = session;
  try {
    session.host = new NativeHost(failure);
    const result = await session.host.send({ type: "start", version: 1, title: tab.title || "Browser meeting" });
    session.path = result.path;
    if (session.stopping) throw new Error("Recording could not start.");
    if (mode === "tracks") {
      // Idle MV3 workers lose ports. Wake every existing isolated relay on demand.
      await chrome.tabs.sendMessage(tab.id, { type: "cassini-connect" }).catch(() => {});
      await new Promise(resolve => setTimeout(resolve, 100));
      for (const frame of frames) {
        if (frame.tabId !== tab.id || session.frames.has(frame)) continue;
        session.frames.add(frame);
        frame.port.postMessage({ type: "start", token: session.token, origin: session.origin });
      }
      if (!session.frames.size) throw new Error("Reload the meeting tab after installing the extension, then try again.");

    } else {
      if (await hasOffscreenDocument()) await chrome.offscreen.closeDocument();
      await chrome.offscreen.createDocument({ url: "offscreen.html", reasons: ["USER_MEDIA"], justification: "Record the selected tab audio locally while the popup is closed." });
      const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
      const response = await chrome.runtime.sendMessage({ target: "offscreen", type: "start", streamId, origin: session.origin });
      if (response?.error) throw new Error(response.error);
    }
    await chrome.action.setBadgeBackgroundColor({ color: "#C62828" });
    await chrome.action.setBadgeText({ text: "REC" });
  } catch (error) {
    session.problem ??= error.message;
    await stop();
    throw error;
  }
}

chrome.runtime.onConnect.addListener(port => {
  if (port.name !== "cassini-frame" || port.sender?.id !== chrome.runtime.id || !port.sender.tab) return;
  const frame = { port, tabId: port.sender.tab.id };
  frames.add(frame);
  // Newly inserted frames may carry meeting tracks; they share the original session clock.
  if (active?.mode === "tracks" && !active.stopping && active.tabId === frame.tabId) {
    active.frames.add(frame);
    port.postMessage({ type: "start", token: active.token, origin: active.origin });
  }
  port.onMessage.addListener(async message => {
    const session = active;
    if (!session?.frames.has(frame)) return;
    if (message.type === "stopped") {
      if (!session.stopping) failure("A meeting frame stopped capture unexpectedly. The partial recording was saved.");
      frame.finished?.(); return;
    }
    if (message.type === "warning") {
      session.warning = "One audio source was interrupted or lost samples. Other sources continue.";
      session.problem ??= session.warning; return;
    }
    if (message.type === "error") { failure("The meeting frame could not capture audio. Try Tab audio. The partial recording was saved."); return; }
    if (message.type !== "packet" || !["track", "audio"].includes(message.packet?.type)) return;
    try {
      if (JSON.stringify(message.packet).length > 400000) throw new Error("Audio message exceeds the size limit.");
      await session.host.send(message.packet);
      if (message.packet.type === "track") session.tracks++;
      port.postMessage({ type: "ack", token: session.token, request: message.request });
    } catch (error) {
      try { port.postMessage({ type: "ack", token: session.token, request: message.request, error: error.message }); } catch {}
      failure(error.message);
    }
  });
  port.onDisconnect.addListener(() => {
    frames.delete(frame);
    frame.finished?.();
    if (active?.frames.has(frame)) {
      active.frames.delete(frame);
      if (!active.stopping) failure("The meeting frame closed or navigated. The partial recording was saved.");
    }
  });
});

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message.target !== "background" || sender.id !== chrome.runtime.id) return;
  const popup = sender.url === chrome.runtime.getURL("popup.html");
  const offscreen = sender.url === chrome.runtime.getURL("offscreen.html");
  (async () => {
    if (offscreen) {
      if (message.type === "warning" && active) { active.problem ??= "Tab audio lost samples during a recording stall."; active.warning = active.problem; return {}; }
      if (message.type === "error") { failure(message.message); return {}; }
      if (message.type === "packet" && active?.mode === "tab" && ["track", "audio"].includes(message.packet?.type)) {
        await active.host.send(message.packet);
        if (message.packet.type === "track" && active) active.tracks++;
        return {};
      }
      throw new Error("No active tab recording.");
    }
    if (!popup) throw new Error("Only the extension popup can control recording.");
    if (message.type === "start") await start(message.mode);
    else if (message.type === "stop") await stop();
    else if (message.type !== "status") throw new Error("Unknown control message.");
    return status();
  })().then(respond, error => respond({ error: error.message }));
  return true;
});
chrome.tabs.onRemoved.addListener(id => { if (active?.tabId === id) failure("The meeting tab closed. The partial recording was saved."); });
