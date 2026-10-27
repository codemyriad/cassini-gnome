async function hasOffscreenDocument() {
  return (await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] })).length > 0;
}

const frames = new Set();
let active = null;
let lastStatus = { recording: false, message: "Ready. Choose Separate tracks or Whole tab." };

const recovery = (async () => {
  const { cassiniActive } = await chrome.storage.session.get("cassiniActive");
  if (cassiniActive) {
    lastStatus = { recording: false, path: cassiniActive.path,
      message: "Recording was interrupted. Audio recorded so far was saved." };
    if (cassiniActive.mode === "tab" && await hasOffscreenDocument()) await chrome.offscreen.closeDocument();
    await chrome.storage.session.remove("cassiniActive");
    await chrome.action.setBadgeText({ text: "" });
  }
})().catch(async () => {
  lastStatus = { recording: false, message: "Cassini could not check the previous recording status. Saved audio remains on this computer." };
  await chrome.action.setBadgeText({ text: "" }).catch(() => {});
});

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
      const message = chrome.runtime.lastError?.message || "Lost the connection to Cassini. Audio recorded so far was saved.";
      for (const request of this.pending.splice(0)) { clearTimeout(request.timer); request.reject(new Error(message)); }
      if (!this.closed) onFailure(message);
    });
  }
  send(packet) {
    if (this.closed) return Promise.reject(new Error("Cassini is no longer connected."));
    if (this.pending.length >= 512) return Promise.reject(new Error("Cassini cannot keep up with the audio. Audio recorded so far was saved."));
    return new Promise((resolve, reject) => {
      const request = { resolve, reject, timer: setTimeout(() => {
        reject(new Error("Cassini stopped responding. Audio recorded so far was saved."));
        this.close();
      }, 10000) };
      this.pending.push(request);
      try { this.port.postMessage(packet); }
      catch (error) { clearTimeout(request.timer); this.pending.pop(); reject(error); }
    });
  }
  close() {
    this.closed = true;
    for (const request of this.pending.splice(0)) { clearTimeout(request.timer); request.reject(new Error("Cassini is no longer connected.")); }
    this.port.disconnect();
  }
}

function status() {
  return active ? { recording: true, stopping: active.stopping, tracks: active.tracks,
    message: active.warning || (!active.tracks ? "Waiting for audio. Join the call, or record the whole tab instead." : `${active.stopping ? "Saving" : "Recording"} ${active.mode === "tracks" ? "separate tracks" : "the whole tab"} · ${active.tracks} ${active.tracks === 1 ? "recorded track" : "recorded tracks"}`), path: active.path } : lastStatus;
}

function failure(message) {
  if (active) { active.problem ??= message; void stop(); }
}

async function stop(session = active) {
  if (!session) return;
  if (session.stopPromise) return session.stopPromise;
  session.stopping = true;
  session.stopPromise = (async () => {
  // Let a cancelled start settle before closing resources or clearing stored state.
  // The start operation never waits for stop, so a failure during discovery/host ACK
  // cannot deadlock this join or create a host after cleanup has already run.
  await session.starting?.catch(() => {});
  try {
    if (session.mode === "tracks") {
      await Promise.all([...session.frames].map(frame => new Promise(resolve => {
        const timer = setTimeout(() => { session.problem ??= "Part of the meeting page did not finish sending audio. Its last moments are missing from the recording."; resolve(); }, 15000);
        frame.finished = () => { clearTimeout(timer); frame.finished = null; resolve(); };
        try { frame.port.postMessage({ type: "stop", token: session.token }); }
        catch { frame.finished(); }
      })));
    } else if (await hasOffscreenDocument()) {
      const response = await chrome.runtime.sendMessage({ target: "offscreen", type: "stop" });
      if (response?.error) session.problem ??= response.error;
    }
    // Native messages are processed in order, so the stop acknowledgement follows all audio writes.
    const result = session.host ? await session.host.send({ type: "stop", problem: session.problem }) : {};
    lastStatus = { recording: false, path: result.path, message: session.problem || result.problem || "Saved on this computer." };
  } catch (error) {
    lastStatus = { recording: false, path: session.path, message: session.problem || error.message };
  } finally {
    session.host?.close();
    await chrome.storage.session.remove("cassiniActive").catch(() => {
      lastStatus.message += " Cassini could not update recording status.";
    });
    if (session.mode === "tab" && await hasOffscreenDocument()) await chrome.offscreen.closeDocument();
    if (active === session) active = null;
    await chrome.action.setBadgeText({ text: "" });
  }
  })();
  return session.stopPromise;
}

async function start(mode) {
  await recovery;
  if (active) throw new Error("A recording is already running.");
  if (!["tracks", "tab"].includes(mode)) throw new Error("Unknown recording mode.");
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !/^https?:/.test(tab.url || "")) throw new Error("Open the meeting tab first. Recording works on web pages only.");
  const session = { mode, tabId: tab.id, token: crypto.randomUUID(), origin: Date.now(),
    frames: new Set(), tracks: 0, stopping: false, problem: null };
  active = session;
  const checkStarting = () => {
    if (session.stopping || active !== session) throw new Error("Recording could not start.");
  };
  session.starting = (async () => {
    let streamId;
    if (mode === "tracks") {
      await chrome.tabs.sendMessage(tab.id, { type: "cassini-connect" }).catch(() => {});
      const deadline = Date.now() + 500;
      while (!session.stopping && ![...frames].some(frame => frame.tabId === tab.id) && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      if (![...frames].some(frame => frame.tabId === tab.id)) throw new Error("Cassini cannot reach this page yet. Reload the meeting tab, then try again.");
    } else {
      streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    }
    // No output folder exists until discovery or the invocation grant has succeeded.
    checkStarting();
    session.host = new NativeHost(failure);
    const result = await session.host.send({ type: "start", version: 1, title: tab.title || "Browser meeting" });
    session.path = result.path;
    checkStarting();
    await chrome.storage.session.set({ cassiniActive: { recording: true, mode, path: session.path, tabId: tab.id, startedAt: session.origin } });
    checkStarting();
    session.ready = true;
    if (mode === "tracks") {
      for (const frame of frames) {
        if (frame.tabId !== tab.id || session.frames.has(frame)) continue;
        session.frames.add(frame);
        frame.port.postMessage({ type: "start", token: session.token, origin: session.origin });
      }
    } else {
      if (await hasOffscreenDocument()) await chrome.offscreen.closeDocument();
      checkStarting();
      await chrome.offscreen.createDocument({ url: "offscreen.html", reasons: ["USER_MEDIA"], justification: "Record the selected tab audio locally while the popup is closed." });
      checkStarting();
      const response = await chrome.runtime.sendMessage({ target: "offscreen", type: "start", streamId, origin: session.origin });
      if (response?.error) throw new Error(response.error);
    }
    checkStarting();
    await chrome.action.setBadgeBackgroundColor({ color: "#C62828" });
    await chrome.action.setBadgeText({ text: "REC" });
  })();
  try { await session.starting; } catch (error) {
    session.problem ??= error.message;
    await stop(session);
    throw error;
  }
}

chrome.runtime.onConnect.addListener(port => {
  if (port.name !== "cassini-frame" || port.sender?.id !== chrome.runtime.id || !port.sender.tab) return;
  const frame = { port, tabId: port.sender.tab.id };
  frames.add(frame);
  // Newly inserted frames may carry meeting tracks; they share the original session clock.
  if (active?.ready && active.mode === "tracks" && !active.stopping && active.tabId === frame.tabId) {
    active.frames.add(frame);
    port.postMessage({ type: "start", token: active.token, origin: active.origin });
  }
  port.onMessage.addListener(async message => {
    const session = active;
    if (!session?.frames.has(frame)) return;
    if (message.type === "stopped") {
      if (!session.stopping) failure("The meeting page stopped sending audio. The recording so far was saved.");
      frame.finished?.(); return;
    }
    if (message.type === "warning") {
      session.warning = "One track was interrupted and has a gap. The other tracks continue.";
      session.problem ??= session.warning; return;
    }
    if (message.type === "error") { failure("The meeting page could not record separate tracks. Record the whole tab instead. Audio recorded so far was saved."); return; }
    if (message.type !== "packet" || !["track", "audio"].includes(message.packet?.type)) return;
    try {
      if (JSON.stringify(message.packet).length > 400000) throw new Error("The meeting page sent an oversized audio chunk.");
      await session.host.send(message.packet);
      if (message.packet.type === "track") { session.tracks++; session.warning = null; }
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
      if (!active.stopping) failure("The meeting page closed or navigated away. The recording so far was saved.");
    }
  });
});

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message.target !== "background" || sender.id !== chrome.runtime.id) return;
  const popup = sender.url === chrome.runtime.getURL("popup.html");
  const offscreen = sender.url === chrome.runtime.getURL("offscreen.html");
  (async () => {
    await recovery;
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
chrome.tabs.onRemoved.addListener(id => { if (active?.tabId === id) failure("The meeting tab was closed. The recording so far was saved."); });
