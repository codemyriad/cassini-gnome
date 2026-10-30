import { AppControl } from "./app-control.js";

async function hasOffscreenDocument() {
  return (await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] })).length > 0;
}

const frames = new Set();
// Browser-owned lifecycle evidence, never supplied by a page-posted packet.
const pageStates = new Map();
const pageState = tabId => pageStates.get(tabId) || "present";
function setPageState(tabId, value) {
  if (pageState(tabId) === "closed" && value !== "closed") return;
  if (pageStates.get(tabId) === value) return;
  pageStates.set(tabId, value);
  if (sessions.has(tabId)) void persistSessions().catch(() => {});
  // Idle pages do not retain unbounded tombstones; active sessions are <=100.
  if (pageStates.size > 100) for (const key of pageStates.keys()) {
    if (!sessions.has(key)) pageStates.delete(key);
    if (pageStates.size <= 100) break;
  }
  notifyControl();
}
const sessions = new Map();
const pendingStarts = new Map();
const saved = new Map();
const interrupted = [];
const firstSession = () => sessions.values().next().value;
let lastStatus = { recording: false, message: "Ready. Choose Separate tracks or Whole tab." };
let storageTail = Promise.resolve();
function persistSessions() {
  const write = async () => {
    const values = [...sessions.values()].filter(s => s.path).map(s =>
      ({ recording: true, mode: s.mode, path: s.path, tabId: s.tabId, startedAt: s.origin, captureId: s.token, pageState: pageState(s.tabId) }));
    if (values.length) await chrome.storage.session.set({ cassiniActive: values[0], cassiniSessions: values });
    else { await chrome.storage.session.remove("cassiniActive"); await chrome.storage.session.remove("cassiniSessions"); }
  };
  storageTail = storageTail.then(write, write);
  return storageTail;
}
const recovery = (async () => {
  const stored = await chrome.storage.session.get(["cassiniActive", "cassiniSessions"]);
  const previous = Array.isArray(stored.cassiniSessions) ? stored.cassiniSessions.slice(0, 100) : stored.cassiniActive ? [stored.cassiniActive] : [];
  for (const value of previous) {
    pageStates.set(value.tabId, value.pageState === "closed" ? "closed" : "navigating");
    const status = { recording: false, tabId: value.tabId ?? null, path: value.path, captureId: value.captureId,
      message: "Recording was interrupted. Audio recorded so far was saved.", pageState: value.pageState || "navigating" };
    saved.set(value.tabId, status); interrupted.push(status); lastStatus = status;
    if (value.mode === "tab" && await hasOffscreenDocument()) await chrome.offscreen.closeDocument();
  }
  if (previous.length) {
    await chrome.storage.session.remove("cassiniActive"); await chrome.storage.session.remove("cassiniSessions");
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
    this.port = chrome.runtime.connectNative("io.codemyriad.cassini");
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

function status(session = firstSession()) {
  return session ? { recording: true, stopping: session.stopping, tracks: session.tracks, tabId: session.tabId, mode: session.mode,
    pageState: pageState(session.tabId), message: session.warning || (!session.tracks ? "Waiting for audio. Join the call, or record the whole tab instead." : `${session.stopping ? "Saving" : "Recording"} ${session.mode === "tracks" ? "separate tracks" : "the whole tab"} · ${session.tracks} ${session.tracks === 1 ? "recorded track" : "recorded tracks"}`), path: session.path } : lastStatus;
}
function failure(message, session = firstSession()) {
  if (session) { session.problem ??= message; session.loss = true; notifyControl(); void stop(session); }
}
async function stop(session = firstSession(), reason = "user") {
  if (!session) { for (const request of pendingStarts.values()) request.cancelled = true; return; }
  if (session.stopPromise) return session.stopPromise;
  session.stopping = true;
  notifyControl();
  session.stopPromise = (async () => {
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
      const packet = { type: "stop", problem: session.problem };
      if (session.keepOnNavigation && !session.loss) packet.reason = reason;
      if (session.independent && session.loss) packet.failureReason = "lost";
      const result = session.host ? await session.host.send(packet) : {};
      lastStatus = { recording: false, tabId: session.tabId, pageState: pageState(session.tabId), path: result.path, message: session.problem || result.problem || "Saved on this computer." };
    } catch (error) {
      lastStatus = { recording: false, tabId: session.tabId, pageState: pageState(session.tabId), path: session.path, message: session.problem || error.message };
    } finally {
      session.host?.close();
      if (sessions.get(session.tabId) === session) sessions.delete(session.tabId);
      saved.set(session.tabId, lastStatus);
      if (saved.size > 100) saved.delete(saved.keys().next().value);
      await persistSessions().catch(() => { lastStatus.message += " Cassini could not update recording status."; });
      if (session.mode === "tab" && await hasOffscreenDocument()) await chrome.offscreen.closeDocument();
      await chrome.action.setBadgeText({ text: sessions.size ? "REC" : "" });
      notifyControl();
    }
  })();
  return session.stopPromise;
}

async function start(mode, tabId = null, options = {}) {
  await recovery;
  if ((!options.independent && (sessions.size || pendingStarts.size)) || sessions.has(tabId) || pendingStarts.has(tabId)) throw new Error("A recording is already running. Stop it before starting another.");
  if (!["tracks", "tab"].includes(mode)) throw new Error("Unknown recording mode.");
  const request = { cancelled: false };
  const key = tabId ?? Symbol();
  if (sessions.size + pendingStarts.size >= 100) throw new Error("Cassini has received too many commands. Reconnect and try again.");
  pendingStarts.set(key, request);
  try {
    let tab;
    if (tabId !== null) {
      if (!Number.isSafeInteger(tabId) || tabId < 0) throw new Error("Choose a meeting to record.");
      try { tab = await chrome.tabs.get(tabId); }
      catch { throw new Error("That meeting is no longer available."); }
    } else [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (request.cancelled) throw new Error("Recording could not start.");
    if (!Number.isSafeInteger(tab?.id) || tab.id < 0 || !/^https?:/.test(tab.url || "")) throw new Error("Open the meeting tab first. Recording works on web pages only.");
    if (sessions.has(tab.id)) throw new Error("A recording is already running. Stop it before starting another.");
    const session = { mode, tabId: tab.id, independent: options.independent === true, captureLocal: options.captureLocal !== false, keepOnNavigation: options.keepOnNavigation === true, token: crypto.randomUUID(), origin: Date.now(),
      frames: new Set(), tracks: 0, stopping: false, problem: null };
    sessions.set(tab.id, session);
    notifyControl();
    const checkStarting = () => {
      if (session.stopping || sessions.get(session.tabId) !== session) throw new Error("Recording could not start.");
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
      session.host = new NativeHost(message => failure(message, session));
      const start = { type: "start", version: 1, title: tab.title || "Browser meeting" };
      if (session.independent) Object.assign(start, { independent: true, microphone: options.microphone !== false, captureId: session.token, timelineOriginMs: session.origin });
      const result = await session.host.send(start);
      session.path = result.path;
      notifyControl();
      checkStarting();
      await persistSessions();
      checkStarting();
      session.ready = true;
      if (mode === "tracks") {
        for (const frame of frames) {
          if (frame.tabId !== tab.id || session.frames.has(frame)) continue;
          session.frames.add(frame);
          frame.port.postMessage({ type: "start", token: session.token, origin: session.origin, captureLocal: session.captureLocal });
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
      notifyControl();
    })();
    try { await session.starting; } catch (error) {
      session.problem ??= error.message;
      // Once the app owns a folder, an unexpected startup failure is browser
      // loss too. Preserve its native inputs/fallback just like a later relay
      // failure. An explicit Stop during startup must still finalize normally.
      if (session.independent && session.path && !session.stopping) session.loss = true;
      await stop(session);
      throw error;
    }
  } finally { if (pendingStarts.get(key) === request) pendingStarts.delete(key); }
}

chrome.runtime.onConnect.addListener(port => {
  if (port.name !== "cassini-frame" || port.sender?.id !== chrome.runtime.id || !port.sender.tab) return;
  const frame = { port, tabId: port.sender.tab.id, frameId: port.sender.frameId ?? 0, sourceCount: 0, heard: false };
  if (pageState(frame.tabId) === "closed") return;
  if (frame.frameId === 0) setPageState(frame.tabId, "present");
  frames.add(frame);
  port.postMessage({ type: "discover" });
  // Newly inserted frames may carry meeting tracks; they share the original session clock.
  const current = sessions.get(frame.tabId);
  if (current?.ready && current.mode === "tracks" && !current.stopping) {
    current.frames.add(frame);
    port.postMessage({ type: "start", token: current.token, origin: current.origin, captureLocal: current.captureLocal });
  }
  port.onMessage.addListener(async message => {
    if (!frames.has(frame)) return;
    if (message.type === "pageState") {
      if (!frame.obsolete && frame.frameId === 0 && ["present", "navigating"].includes(message.state)) {
        setPageState(frame.tabId, message.state);
        if (message.state === "navigating") { frame.sourceCount = 0; frame.heard = false; }
      }
      return;
    }
    if (message.type === "discovery") {
      if (frame.obsolete || pageState(frame.tabId) === "closed") return;
      if (Number.isInteger(message.sourceCount) && message.sourceCount >= 0 && message.sourceCount <= 64 &&
          (frame.sourceCount !== message.sourceCount || frame.heard !== (message.heard === true))) {
        frame.sourceCount = message.sourceCount;
        frame.heard = message.heard === true;
        notifyControl();
      }
      return;
    }
    const session = sessions.get(frame.tabId);
    if (!session?.frames.has(frame)) return;
    if (session.keepOnNavigation && (frame.obsolete || pageState(frame.tabId) !== "present") &&
        ["stopped", "error"].includes(message.type) && !session.stopping) {
      session.frames.delete(frame);
      frame.sourceCount = 0; frame.heard = false;
      frame.finished?.(); notifyControl(); return;
    }
    if (message.type === "stopped") {
      if (!session.stopping) failure("The meeting page stopped sending audio. The recording so far was saved.", session);
      frame.finished?.(); return;
    }
    if (message.type === "warning") {
      session.warning = "One track was interrupted and has a gap. The other tracks continue.";
      session.problem ??= session.warning; notifyControl(); return;
    }
    if (message.type === "error") { failure("The meeting page could not record separate tracks. Record the whole tab instead. Audio recorded so far was saved.", session); return; }
    if (message.type !== "packet" || !["track", "audio"].includes(message.packet?.type)) return;
    try {
      if (JSON.stringify(message.packet).length > 400000) throw new Error("The meeting page sent an oversized audio chunk.");
      await session.host.send(message.packet);
      if (message.packet.type === "track") { session.tracks++; session.warning = null; notifyControl(); }
      port.postMessage({ type: "ack", token: session.token, request: message.request });
    } catch (error) {
      try { port.postMessage({ type: "ack", token: session.token, request: message.request, error: error.message }); } catch {}
      failure(error.message, session);
    }
  });
  port.onDisconnect.addListener(() => {
    frames.delete(frame);
    notifyControl();
    frame.finished?.();
    const session = sessions.get(frame.tabId);
    if (session?.frames.has(frame)) {
      session.frames.delete(frame);
      if (!session.stopping && !session.keepOnNavigation) failure("The meeting page closed or navigated away. The recording so far was saved.", session);
      if (session.keepOnNavigation && ![...frames].some(f => f.tabId === frame.tabId && f.sourceCount > 0))
        setPageState(frame.tabId, "navigating");
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
      const active = [...sessions.values()].find(s => s.mode === "tab");
      if (message.type === "warning" && active) { active.problem ??= "Tab audio lost samples during a recording stall."; active.warning = active.problem; return {}; }
      if (message.type === "error") { failure(message.message, active); return {}; }
      if (message.type === "packet" && active?.mode === "tab" && ["track", "audio"].includes(message.packet?.type)) {
        await active.host.send(message.packet);
        if (message.packet.type === "track" && active) { active.tracks++; notifyControl(); }
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
chrome.tabs.onRemoved.addListener(id => {
  if (!sessions.has(id) && ![...frames].some(f => f.tabId === id)) return;
  setPageState(id, "closed");
  for (const frame of frames) if (frame.tabId === id) { frame.sourceCount = 0; frame.heard = false; }
  const session = sessions.get(id);
  if (session && !session.keepOnNavigation) failure("The meeting tab was closed. The recording so far was saved.", session);
  if (session?.keepOnNavigation) for (const frame of [...session.frames]) {
    session.frames.delete(frame); frames.delete(frame); frame.obsolete = true; frame.finished?.();
  }
  notifyControl();
});
chrome.tabs.onUpdated.addListener((id, change) => {
  if (change.status === "loading") {
    if (sessions.has(id) || [...frames].some(f => f.tabId === id)) setPageState(id, "navigating");
    for (const frame of frames) if (frame.tabId === id) { frame.sourceCount = 0; frame.heard = false; frame.obsolete = true; }
  }
  if (change.status || change.title !== undefined || change.url !== undefined) notifyControl();
});

function controlStatus(tabId = null) {
  const value = tabId === null ? status() : sessions.has(tabId) ? status(sessions.get(tabId)) : saved.get(tabId) || { recording: false, tabId, path: null, message: lastStatus.message };
  const packet = { type: "status", recording: value.recording, tabId: value.tabId ?? null,
    path: value.path ?? null, message: value.message, pageState: value.tabId == null ? undefined : value.pageState || pageState(value.tabId), tracks: value.tracks ?? 0, stopping: !!value.stopping };
  if (interrupted.length) packet.interrupted = interrupted;
  packet.sessions = [...sessions.values()].map(session => {
    const value = status(session);
    return { recording: true, tabId: session.tabId, path: session.path ?? null, captureId: session.token, tracks: session.tracks,
      stopping: session.stopping, message: value.message, pageState: pageState(session.tabId) };
  });
  return packet;
}
async function meetings() {
  const counts = new Map();
  for (const frame of frames) if (frame.sourceCount > 0) counts.set(frame.tabId, Math.min(4096, (counts.get(frame.tabId) || 0) + frame.sourceCount));
  for (const session of sessions.values()) if (!counts.has(session.tabId)) counts.set(session.tabId, 0);
  const found = await Promise.all([...counts].sort(([a], [b]) => Number(sessions.has(b)) - Number(sessions.has(a)) || a - b).slice(0, 100).map(async ([tabId, sourceCount]) => {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (!/^https?:/.test(tab.url || "")) return null;
      const heard = [...frames].some(frame => frame.tabId === tabId && frame.sourceCount > 0 && frame.heard);
      return { tabId, title: [...(tab.title || "Meeting")].slice(0, 200).join(""), url: new URL(tab.url).href.slice(0, 2048), sourceCount, heard, pageState: pageState(tabId) };
    } catch { return null; }
  }));
  return { type: "meetings", meetings: found.filter(Boolean) };
}
async function controlCommand(command) {
  await recovery;
  if (command.action === "start") {
    if (!command.independent && (sessions.size || pendingStarts.size)) throw new Error("A recording is already running. Stop it before starting another.");
    if (![...frames].some(frame => frame.tabId === command.tabId && frame.sourceCount > 0))
      throw new Error("No audio is available from that meeting yet.");
    await start("tracks", command.tabId, { independent: command.independent === true,
      captureLocal: command.captureLocal, microphone: command.microphone, keepOnNavigation: command.keepOnNavigation });
  } else {
    if (!command.independent && firstSession() && firstSession().tabId !== command.tabId) throw new Error("A recording is already running. Stop it before starting another.");
    const session = sessions.get(command.tabId);
    if (session) await stop(session, command.reason);
    else if (pendingStarts.has(command.tabId)) pendingStarts.get(command.tabId).cancelled = true;
  }
  return controlStatus(command.independent ? command.tabId : null);
}
function cancelPendingStart(command) {
  const request = pendingStarts.get(command.tabId);
  if (request) request.cancelled = true;
  const session = sessions.get(command.tabId);
  if (session && !session.ready) session.stopping = true;
}
function notifyControl() { appControl.publish(); }
const appControl = new AppControl({
  runtime: chrome.runtime, alarms: chrome.alarms, capabilities: ["independent-sessions-v1", "call-evidence-v1", "page-state-v1"], cancelPending: cancelPendingStart,
  connected: async () => {
    await recovery;
    const tabs = await chrome.tabs.query({});
    await Promise.all(tabs.filter(tab => /^https?:/.test(tab.url || "")).map(tab => chrome.tabs.sendMessage(tab.id, { type: "cassini-connect" }).catch(() => {})));
  },
  snapshot: async () => { await recovery; return [await meetings(), controlStatus()]; },
  command: controlCommand
});
appControl.start();
