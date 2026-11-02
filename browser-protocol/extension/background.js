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
  if (sessions.has(tabId)) {
    void persistSessions().catch(() => {});
    appControl.publishStatus(controlStatus());
  }
  // Idle pages do not retain unbounded tombstones; active sessions are <=100.
  if (pageStates.size > 100) for (const key of pageStates.keys()) {
    if (!sessions.has(key)) pageStates.delete(key);
    if (pageStates.size <= 100) break;
  }
  notifyControl();
}
const sessions = new Map();
const pendingStarts = new Map();
const callGenerations = new Map();
let identitiesHydrated = false;
const callAddress = value => typeof value === "string" ? value.split("#", 1)[0] : null;
function callInfo(tabId) {
  if (!callGenerations.has(tabId)) callGenerations.set(tabId, { generation: crypto.randomUUID(), url: null });
  if (callGenerations.size > 200) for (const key of callGenerations.keys()) {
    if (key !== tabId && !sessions.has(key)) callGenerations.delete(key);
    if (callGenerations.size <= 200) break;
  }
  return callGenerations.get(tabId);
}
function documentFor(tabId) {
  return [...frames].find(f => f.tabId === tabId && !f.obsolete && f.frameId === 0)?.documentGeneration;
}
async function addressFingerprint(url) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(callAddress(url)));
  return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
}
function fenceBaseline(frame, info) {
  if (typeof info.documentGeneration === "string" && typeof info.hookGeneration === "string")
    frame.fencedGenerations.add(JSON.stringify([info.documentGeneration,info.hookGeneration]));
  frame.identitySeeded = true;
}
function callChanged(tabId, url = null) {
  const current = sessions.get(tabId);
  const info = callInfo(tabId);
  info.generation = crypto.randomUUID(); info.url = url; info.boundaryPending = true;
  delete info.addressHash;
  for (const frame of frames) if (frame.tabId === tabId) {
    if (frame.frameId === 0) fenceBaseline(frame, info);
    if (frame.callGeneration !== undefined) frame.fencedGenerations.add(JSON.stringify([frame.documentGeneration,frame.callGeneration]));
    frame.callGeneration = undefined; frame.sourceCount = 0; frame.heard = false;
  }
  if (current && !current.callChanged) { current.callChanged = true; void stop(current, "callEnded"); }
  void persistSessions().catch(() => {});
  notifyControl();
}

const saved = new Map();
const interrupted = [];
const firstSession = () => sessions.values().next().value;
const sourceEncoder = new TextEncoder();
function sourceRecords(values) {
  if (!Array.isArray(values)) throw Object.assign(new Error("Invalid audio source records."), {scope: "command", code: "source"});
  return values.map(source => {
    if (!source || typeof source.sourceTrackId !== "string" || !source.sourceTrackId.length ||
        sourceEncoder.encode(source.sourceTrackId).length > 200 ||
        !["local", "remote", "media", "tab", "application"].includes(source.kind) ||
        (source.failed !== undefined && typeof source.failed !== "boolean"))
      throw Object.assign(new Error("Invalid audio source record."), {scope: "command", code: "source"});
    return {sourceTrackId: source.sourceTrackId, kind: source.kind,
      label: typeof source.label === "string" ? [...source.label.replace(/[\u0000-\u001f\u007f-\u009f]/g, "")].slice(0, 200).join("") : source.kind,
      failed: source.failed === true};
  });
}
async function recordSources(session, values) {
  const records = sourceRecords(values).map(source => {
    const previous = session.discoveredSources.get(source.sourceTrackId);
    source.failed ||= previous?.failed === true;
    session.discoveredSources.set(source.sourceTrackId, source);
    return source;
  });
  // Bound each UTF-8 native request, never the session's append-only ledger.
  const packets = []; let batch = [], bytes = 42;
  for (const source of records) {
    const size = sourceEncoder.encode(JSON.stringify(source)).length + 1;
    if (bytes + size > 400000 && batch.length) { packets.push({type: "sources", discoveredSources: batch}); batch = []; bytes = 42; }
    batch.push(source); bytes += size;
  }
  if (batch.length) packets.push({type: "sources", discoveredSources: batch});
  // Enqueue together: every request's existing ten-second deadline starts now,
  // rather than accumulating an unbounded chain of sequential Stop waits.
  const pending = Promise.all(packets.map(packet => session.host.send(packet)));
  session.sourceUpdates.add(pending);
  try { await pending; } finally { session.sourceUpdates.delete(pending); }
}
let lastStatus = { recording: false, message: "Ready. Choose Separate tracks or Whole tab." };
let storageTail = Promise.resolve();
function persistSessions() {
  if (!identitiesHydrated) return recovery.then(() => persistSessions());
  const write = async () => {
    const identities = [];
    for (const [tabId, snapshot] of [...callGenerations].map(([tabId, info]) => [tabId, {...info}])) {
      const {url, ...info} = snapshot;
      info.addressHash = url === null ? info.addressHash ?? null : await addressFingerprint(url);
      identities.push({tabId, ...info});
    }
    await chrome.storage.session.set({cassiniCallGenerations: identities});
    const values = [...sessions.values()].filter(s => s.path).map(s =>
      ({ recording: true, mode: s.mode, path: s.path, tabId: s.tabId, startedAt: s.origin, captureId: s.token, pageState: pageState(s.tabId) }));
    if (values.length) await chrome.storage.session.set({ cassiniActive: values[0], cassiniSessions: values });
    else { await chrome.storage.session.remove("cassiniActive"); await chrome.storage.session.remove("cassiniSessions"); }
  };
  storageTail = storageTail.then(write, write);
  return storageTail;
}
const recovery = (async () => {
  const stored = await chrome.storage.session.get(["cassiniActive", "cassiniSessions", "cassiniCallGenerations"]);
  const opaque = value => typeof value === "string" && value.length > 0 && sourceEncoder.encode(value).length <= 200;
  const identities = stored.cassiniCallGenerations;
  if (Array.isArray(identities) && identities.length <= 200) for (const value of identities) {
    if (!value || !Number.isSafeInteger(value.tabId) || value.tabId < 0 || !opaque(value.generation) ||
        !(value.addressHash === null || typeof value.addressHash === "string" && /^[0-9a-f]{64}$/.test(value.addressHash))) continue;
    const info = {generation:value.generation, url:null, addressHash:value.addressHash};
    if (opaque(value.documentGeneration) && opaque(value.hookGeneration))
      Object.assign(info, {documentGeneration:value.documentGeneration, hookGeneration:value.hookGeneration});
    if (value.boundaryPending === true) info.boundaryPending = true;
    callGenerations.set(value.tabId, info);
    try {
      const tab = await chrome.tabs.get(value.tabId);
      if (typeof tab.url === "string" && /^https?:/.test(tab.url)) {
        if (info.addressHash !== null && await addressFingerprint(tab.url) !== info.addressHash)
          callChanged(value.tabId, callAddress(tab.url));
        else info.url = callAddress(tab.url);
      }
    } catch { /* A missing lookup cannot erase a retained call identity. */ }
  }
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
  identitiesHydrated = true;
})().catch(async () => {
  lastStatus = { recording: false, message: "Cassini could not check the previous recording status. Saved audio remains on this computer." };
  await chrome.action.setBadgeText({ text: "" }).catch(() => {});
  identitiesHydrated = true;
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
        message.type === "error" ? request.reject(Object.assign(new Error(message.message), { code: message.code, scope: message.scope, id: message.id, ...(message.retired === true ? {retired:true} : {}) })) : request.resolve(message);
      }
      if (message.type === "error" && !["source", "command"].includes(message.scope)) onFailure(message.message);
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

function sourceWarning(session, message, fallback) {
  const key = message.sourceTrackId;
  if (typeof key === "string" && key.length > 0 && sourceEncoder.encode(key).length <= 200 &&
      (message.recovered === true || typeof message.message === "string" && message.message.length > 0)) {
    session.sourceWarnings ??= new Map();
    if (message.recovered === true) session.sourceWarnings.delete(key);
    else {
      const error = [...message.message.replace(/[\u0000-\u001f\u007f-\u009f]/g, "")].slice(0,2000).join("");
      session.sourceWarnings.set(key, error);
      if (!message.transient) session.problem ??= error;
    }
  } else {
    session.warning = fallback;
    if (!message.transient) session.problem ??= fallback;
  }
  notifyControl();
}

function status(session = firstSession()) {
  return session ? { recording: true, captureId: session.token, timelineOriginMs: session.origin, documentGeneration: documentFor(session.tabId), callGeneration: session.callGeneration, stopping: session.stopping, tracks: session.tracks, tabId: session.tabId, mode: session.mode,
    pageState: pageState(session.tabId), failedSources: [...new Set([...session.discoveredSources].filter(([,s])=>s.failed).map(([id])=>id).concat([...(session.sourceWarnings?.keys() || [])]))], message: session.sourceWarnings?.values().next().value || session.warning || (!session.tracks ? "Waiting for audio. Join the call, or record the whole tab instead." : `${session.stopping ? "Saving" : "Recording"} ${session.mode === "tracks" ? "separate tracks" : "the whole tab"} · ${session.tracks} ${session.tracks === 1 ? "recorded track" : "recorded tracks"}`), path: session.path } : lastStatus;
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
      if (session.independent) packet.captureId = session.token;
      if (session.sourceOutcomes) {
        await Promise.allSettled([...session.sourceUpdates]);
        packet.discoveredSources = []; // The acknowledged live batches are already durable.
      }
      if ((session.keepOnNavigation || session.sourceOutcomes) && !session.loss) packet.reason = reason;
      if (session.independent && session.loss) packet.failureReason = "lost";
      // A refused/unsupported resume never owns the target. Closing this
      // transport must not turn an attempted attachment into a Stop of it.
      const result = session.host && (!session.resumeTarget || session.resumed) ? await session.host.send(packet) : {};
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
  if (options.resumeIntent?.cancelled) throw new Error("Recording could not start.");
  if ((!options.independent && (sessions.size || pendingStarts.size)) || sessions.has(tabId) || pendingStarts.has(tabId)) throw new Error("A recording is already running. Stop it before starting another.");
  if (!["tracks", "tab"].includes(mode)) throw new Error("Unknown recording mode.");
  const request = { cancelled: false, resumeCapture: options.resumeCapture };
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
    if (request.cancelled || options.resumeIntent?.cancelled) throw new Error("Recording could not start.");
    if (!Number.isSafeInteger(tab?.id) || tab.id < 0 || !/^https?:/.test(tab.url || "")) throw new Error("Open the meeting tab first. Recording works on web pages only.");
    if (sessions.has(tab.id)) throw new Error("A recording is already running. Stop it before starting another.");
    const identity = callInfo(tab.id);
    if (options.expectedCallGeneration !== undefined && options.expectedCallGeneration !== identity.generation)
      throw new Error("That call is no longer active.");
    identity.url = callAddress(tab.url);
    const target = options.resumeCapture;
    const session = { callGeneration: identity.generation, mode, tabId: tab.id, independent: options.independent === true, captureLocal: options.captureLocal !== false, keepOnNavigation: options.keepOnNavigation === true, token: target?.captureId ?? crypto.randomUUID(), origin: target?.timelineOriginMs ?? Date.now(), resumeTarget: target,
      frames: new Set(), tracks: 0, trackIds: new Set(), trackReservations: new Set(), endedTrackIds: new Set(), trackSources: new Map(), discoveredSources: new Map(), sourceUpdates: new Set(), lastTab: tab, lastSourceCount: 0, stopping: false, problem: null };
    sessions.set(tab.id, session);
    notifyControl();
    const checkStarting = () => {
      if (session.stopping || sessions.get(session.tabId) !== session) throw new Error("Recording could not start.");
      if (options.expectedCallGeneration !== undefined && callInfo(session.tabId).generation !== session.callGeneration)
        throw new Error("That call is no longer active.");
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
      const start = { type: "start", version: 1, title: tab.title || "Browser meeting", features: ["source-outcomes-v1"],
        callGeneration: session.callGeneration, documentGeneration: documentFor(tab.id) };
      if (session.independent) Object.assign(start, { independent: true, microphone: options.microphone !== false, captureId: session.token, timelineOriginMs: session.origin });
      if (target) {
        // A new native packet type fails closed on older hosts: an unknown
        // optional field on ordinary Start could create a second folder.
        start.type = "resume";
        start.features.push("resume-capture-v1");
        delete start.microphone; // The app retains its actual per-session selection.
      }
      const result = await session.host.send(start);
      if (target) {
        if (result.type !== "started" || typeof result.path !== "string" || !result.path.startsWith("/") ||
            result.resumed !== true || result.captureId !== session.token ||
            result.timelineOriginMs !== session.origin || result.callGeneration !== session.callGeneration ||
            !Array.isArray(result.features) || !result.features.includes("resume-capture-v1"))
          throw new Error("That recording could not be resumed.");
        session.resumed = true;
      }
      session.path = result.path;
      session.sourceOutcomes = Array.isArray(result.features) && result.features.includes("source-outcomes-v1");
      notifyControl();
      checkStarting();
      await persistSessions();
      checkStarting();
      session.ready = true;
      if (mode === "tracks") {
        for (const frame of frames) {
          if (frame.tabId !== tab.id || session.frames.has(frame)) continue;
          session.frames.add(frame);
          frame.port.postMessage({ type: "start", token: session.token, origin: session.origin, captureLocal: session.captureLocal, sourceOutcomes: session.sourceOutcomes });
        }
      } else {
        if (session.sourceOutcomes) {
          session.tabSourceId = crypto.randomUUID();
          await recordSources(session, [{sourceTrackId:session.tabSourceId,kind:"tab",label:"Tab audio",failed:false}]);
          checkStarting();
        }
        if (await hasOffscreenDocument()) await chrome.offscreen.closeDocument();
        checkStarting();
        await chrome.offscreen.createDocument({ url: "offscreen.html", reasons: ["USER_MEDIA"], justification: "Record the selected tab audio locally while the popup is closed." });
        checkStarting();
        const response = await chrome.runtime.sendMessage({ target: "offscreen", type: "start", streamId, origin: session.origin,
          sourceTrackId: session.tabSourceId, sourceOutcomes: session.sourceOutcomes });
        if (response?.error) throw new Error(response.error);
      }
      checkStarting();
      await chrome.action.setBadgeBackgroundColor({ color: "#C62828" });
      await chrome.action.setBadgeText({ text: "REC" });
      notifyControl();
    })();
    try { await session.starting; } catch (error) {
      session.problem ??= error.message;
      if (session.sourceOutcomes && session.tabSourceId && !session.stopping && !session.host?.closed) {
        await recordSources(session, [{sourceTrackId:session.tabSourceId,kind:"tab",label:"Tab audio",failed:true}]).catch(() => {});
      }
      // Once the app owns a folder, an unexpected startup failure is browser
      // loss too. Preserve its native inputs/fallback just like a later relay
      // failure. An explicit Stop during startup must still finalize normally.
      if (session.independent && session.path && !session.stopping) session.loss = true;
      await stop(session);
      throw error;
    }
  } finally { if (pendingStarts.get(key) === request) pendingStarts.delete(key); }
}

// One admission path for page frames and whole-tab offscreen capture. A retired
// span is fenced locally: its late packets are not a failure of the live root.
async function forwardPacket(session, packet) {
  if (session.callChanged && packet.type !== "trackEnd")
    throw Object.assign(new Error("This audio belongs to a different call."), { scope: "source", code: "callChanged" });
  if (["track", "audio", "trackEnd"].includes(packet.type) &&
      (session.endedTrackIds.has(packet.id) || packet.type !== "track" && !session.trackIds.has(packet.id)))
    throw Object.assign(new Error("This audio span is no longer active."), {code:"state",scope:"source",retired:true});
  if (JSON.stringify(packet).length > 400000) throw new Error("The meeting page sent an oversized audio chunk.");
  const reserve = packet.type === "track" && !session.trackIds.has(packet.id) && !session.trackReservations.has(packet.id);
  if (reserve && session.trackIds.size + session.trackReservations.size >= 64)
    throw Object.assign(new Error("This recording has reached its track limit."), { code: "limit", scope: "source" });
  if (reserve) session.trackReservations.add(packet.id);
  if (packet.type === "trackEnd") session.endedTrackIds.add(packet.id);
  try {
    if (packet.type === "sources") {
      if (!Array.isArray(packet.discoveredSources) || !packet.discoveredSources.length)
        throw Object.assign(new Error("Empty audio source update."), {scope:"command",code:"source"});
      await recordSources(session, packet.discoveredSources);
    }
    else await session.host.send(packet);
    if (packet.type === "track" && !session.trackIds.has(packet.id)) {
      session.trackIds.add(packet.id);
      session.trackSources.set(packet.id, packet.sourceTrackId || packet.id); session.tracks++; session.warning = null; notifyControl();
    }
  } catch (error) {
    if (error.scope === "source" && error.code === "state" && session.endedTrackIds.has(packet.id))
      error.retired = true; // Also fence in-flight replies from older hosts.
    // A refused physical span cannot accept more queued PCM. Keep its source
    // eligible for a fresh-span retry; the session and other sources stay live.
    if (error.scope === "source" && error.code !== "callChanged" &&
        (error.code !== "state" || error.retired) && ["track", "audio", "trackEnd"].includes(packet.type))
      session.endedTrackIds.add(packet.id);
    throw error;
  } finally { if (reserve) session.trackReservations.delete(packet.id); }
}

chrome.runtime.onConnect.addListener(port => {
  if (port.name !== "cassini-frame" || port.sender?.id !== chrome.runtime.id || !port.sender.tab) return;
  const frame = { port, tabId: port.sender.tab.id, frameId: port.sender.frameId ?? 0, sourceCount: 0, heard: false, fencedGenerations: new Set() };
  if (pageState(frame.tabId) === "closed") return;
  if (frame.frameId === 0) setPageState(frame.tabId, "present");
  frames.add(frame);
  if (!identitiesHydrated && frame.frameId === 0) void recovery.then(() => {
    if (frames.has(frame) && !frame.obsolete) setPageState(frame.tabId, "present");
  });
  port.postMessage({ type: "discover" });
  // Newly inserted frames may carry meeting tracks; they share the original session clock.
  const current = sessions.get(frame.tabId);
  if (current?.ready && current.mode === "tracks" && !current.stopping) {
    current.frames.add(frame);
    port.postMessage({ type: "start", token: current.token, origin: current.origin, captureLocal: current.captureLocal, sourceOutcomes: current.sourceOutcomes });
  }
  port.onMessage.addListener(async message => {
    if (!identitiesHydrated) await recovery;
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
      const document = message.documentGeneration, generation = message.callGeneration;
      if (typeof document === "string" && document.length > 0 && document.length <= 200 &&
          typeof generation === "string" && generation.length > 0 && generation.length <= 200) {
        const info = callInfo(frame.tabId);
        if (frame.frameId === 0 && !frame.identitySeeded) {
          frame.identitySeeded = true;
          if (info.boundaryPending) fenceBaseline(frame, info);
          else if (info.documentGeneration === document) {
            frame.documentGeneration = document; frame.callGeneration = info.hookGeneration;
          }
        }
        if (frame.fencedGenerations.has(JSON.stringify([document,generation]))) return;
        const changed = frame.frameId === 0 && frame.documentGeneration === document && frame.callGeneration !== undefined && frame.callGeneration !== generation;
        if (changed) callChanged(frame.tabId);
        frame.documentGeneration = document; frame.callGeneration = generation;
        if (frame.frameId === 0 && (info.documentGeneration !== document || info.hookGeneration !== generation)) {
          Object.assign(info, {documentGeneration:document, hookGeneration:generation, boundaryPending:false});
          void persistSessions().catch(() => {});
        }
      }
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
    if (session.sourceOutcomes && Array.isArray(message.discoveredSources)) {
      try { await recordSources(session, message.discoveredSources); }
      catch (error) { failure(error.message, session); return; }
    }
    if (message.type === "stopped") {
      if (!session.stopping) failure("The meeting page stopped sending audio. The recording so far was saved.", session);
      frame.finished?.(); return;
    }
    if (message.type === "warning") {
      sourceWarning(session, message, "One track was interrupted and has a gap. The other tracks continue.");
      return;
    }
    if (message.type === "error") { failure("The meeting page could not record separate tracks. Record the whole tab instead. Audio recorded so far was saved.", session); return; }
    if (message.type !== "packet" || !["track", "audio", ...(session.sourceOutcomes ? ["trackEnd", "sources"] : [])].includes(message.packet?.type)) return;
    try {
      await forwardPacket(session, message.packet);
      port.postMessage({ type: "ack", token: session.token, request: message.request });
    } catch (error) {
      if (error.retired) { /* Already retired locally; keep other spans recording. */ }
      else if (error.code === "state" && ["source", "command"].includes(error.scope)) {
        void stop(session, "user"); // Native-first Stop already saved this capture.
      }
      else if (error.scope === "source" && error.code !== "callChanged") {
        const key = message.packet.sourceTrackId || session.trackSources.get(message.packet.id);
        if (session.sourceOutcomes && typeof key === "string") {
          const previous = session.discoveredSources.get(key);
          try { await recordSources(session, [{sourceTrackId:key,kind:previous?.kind || message.packet.kind || "remote",label:previous?.label || message.packet.label || "Audio",failed:true}]); }
          catch (recordError) { failure(recordError.message, session); }
        }
        session.warning = "One track was interrupted and has a gap. The other tracks continue."; session.problem ??= session.warning; notifyControl(); }
      else if (!["command", "source"].includes(error.scope)) failure(error.message, session);
      try { port.postMessage({ type: "ack", token: session.token, request: message.request, error: error.message, code: error.code, scope: error.scope, ...(error.retired === true ? {retired:true} : {}) }); } catch {}
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
      if (message.type === "warning" && active?.tabSourceId && typeof message.sourceTrackId === "string" && message.sourceTrackId !== active.tabSourceId) return {};
      if ((message.type === "error" || message.type === "warning" && !message.transient) && active?.sourceOutcomes && active.tabSourceId) {
        await recordSources(active, [{sourceTrackId:active.tabSourceId,kind:"tab",label:"Tab audio",failed:true}]);
      }
      if (message.type === "warning" && active) { sourceWarning(active, message, "Tab audio lost samples during a recording stall."); return {}; }
      if (message.type === "error") { failure(message.message, active); return {}; }
      if (message.type === "packet" && active?.mode === "tab" && ["track", "audio", ...(active.sourceOutcomes ? ["trackEnd"] : [])].includes(message.packet?.type)) {
        try { await forwardPacket(active, message.packet); }
        catch (error) {
          if (!error.retired && error.code === "state" && ["source", "command"].includes(error.scope))
            void stop(active, "user"); // The native app has already kept the capture.
          throw error;
        }
        return {};
      }
      throw new Error("No active tab recording.");
    }
    if (!popup) throw new Error("Only the extension popup can control recording.");
    if (message.type === "start") await start(message.mode);
    else if (message.type === "stop") await stop();
    else if (message.type !== "status") throw new Error("Unknown control message.");
    return status();
  })().then(respond, error => respond({ error: error.message, code: error.code, scope: error.scope, ...(error.retired === true ? {retired:true} : {}) }));
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
function tabUpdated(id, change) {
  if (!identitiesHydrated) { void recovery.then(() => tabUpdated(id, change)); return; }
  if (typeof change.url === "string" && callGenerations.has(id)) {
    const info = callInfo(id);
    if (info.url !== null && info.url !== callAddress(change.url)) callChanged(id, callAddress(change.url));
    info.url = callAddress(change.url);
  }
  if (change.status === "loading") {
    if (sessions.has(id) || [...frames].some(f => f.tabId === id)) setPageState(id, "navigating");
    for (const frame of frames) if (frame.tabId === id) { frame.sourceCount = 0; frame.heard = false; frame.obsolete = true; }
  }
  if (change.status || change.title !== undefined || change.url !== undefined) notifyControl();
}
chrome.tabs.onUpdated.addListener(tabUpdated);

function controlStatus(tabId = null) {
  const value = tabId === null ? status() : sessions.has(tabId) ? status(sessions.get(tabId)) : saved.get(tabId) || { recording: false, tabId, path: null, message: lastStatus.message };
  const packet = { type: "status", recording: value.recording, tabId: value.tabId ?? null,
    path: value.path ?? null, message: value.message, captureId: value.captureId, timelineOriginMs: value.timelineOriginMs, failedSources: value.failedSources, documentGeneration: value.documentGeneration,
    callGeneration: value.callGeneration, pageState: value.tabId == null ? undefined : value.pageState || pageState(value.tabId), tracks: value.tracks ?? 0, stopping: !!value.stopping };
  if (interrupted.length) packet.interrupted = interrupted;
  packet.sessions = [...sessions.values()].map(session => {
    const value = status(session);
    return { recording: true, tabId: session.tabId, path: session.path ?? null, captureId: session.token, timelineOriginMs: session.origin, tracks: session.tracks,
      stopping: session.stopping, message: value.message, pageState: pageState(session.tabId), failedSources: value.failedSources,
      documentGeneration: value.documentGeneration, callGeneration: session.callGeneration };
  });
  return packet;
}
async function meetings() {
  const counts = new Map();
  for (const frame of frames) if (frame.sourceCount > 0) counts.set(frame.tabId, Math.min(4096, (counts.get(frame.tabId) || 0) + frame.sourceCount));
  for (const session of sessions.values()) if (!counts.has(session.tabId)) counts.set(session.tabId, 0);
  const tabs = await Promise.all([...counts].sort(([a], [b]) => Number(sessions.has(b)) - Number(sessions.has(a)) || a - b).slice(0, 100).map(async ([tabId]) => {
    try { return [tabId, await chrome.tabs.get(tabId)]; }
    catch { return [tabId, null]; }
  }));
  const found = tabs.map(([tabId, tab]) => {
    try {
      const session = sessions.get(tabId);
      const known = !!tab && /^https?:/.test(tab.url || "");
      if (!known) tab = session?.lastTab;
      if (!tab || !/^https?:/.test(tab.url || "")) return null;
      if (session && known) session.lastTab = { ...tab };
      if (known && callInfo(tabId).url === null) callInfo(tabId).url = callAddress(tab.url);
      // A can reload after its lookup finishes while B's is still pending.
      // Collect every row's current evidence synchronously after ALL lookups.
      const current = [...frames].filter(frame => frame.tabId === tabId && frame.sourceCount > 0);
      const sourceCount = known ? Math.min(4096, current.reduce((total, frame) => total + frame.sourceCount, 0)) : session.lastSourceCount;
      if (session && known) session.lastSourceCount = sourceCount;
      if (!sourceCount && !sessions.has(tabId)) return null;
      const heard = current.some(frame => frame.heard);
      return { tabId, title: [...(tab.title || "Meeting")].slice(0, 200).join(""), url: new URL(tab.url).href.slice(0, 2048), sourceCount, heard, sourceCountKnown: known, documentGeneration: documentFor(tabId), callGeneration: callInfo(tabId).generation, pageState: pageState(tabId) };
    } catch { return null; }
  });
  return { type: "meetings", meetings: found.filter(Boolean) };
}
async function controlCommand(command, intent = {}) {
  await recovery;
  if (intent.cancelled) throw new Error("Recording could not start.");
  if (command.action === "start") {
    if (!command.independent && (sessions.size || pendingStarts.size)) throw new Error("A recording is already running. Stop it before starting another.");
    if (![...frames].some(frame => frame.tabId === command.tabId && frame.sourceCount > 0))
      throw new Error("No audio is available from that meeting yet.");
    await start("tracks", command.tabId, { independent: command.independent === true,
      captureLocal: command.captureLocal, microphone: command.microphone, keepOnNavigation: command.keepOnNavigation, expectedCallGeneration: command.callGeneration, resumeCapture: command.resumeCapture, resumeIntent: intent });
  } else {
    if (!command.independent && firstSession() && firstSession().tabId !== command.tabId) throw new Error("A recording is already running. Stop it before starting another.");
    const session = sessions.get(command.tabId);
    if (command.captureId !== undefined && session?.token !== command.captureId && !(intent.cancelledResume && !session))
      throw new Error("That recording is no longer active.");
    if (session) await stop(session, command.reason);
    else if (pendingStarts.has(command.tabId)) pendingStarts.get(command.tabId).cancelled = true;
  }
  return controlStatus(command.independent ? command.tabId : null);
}
function cancelPendingStart(command) {
  const request = pendingStarts.get(command.tabId);
  if (command.captureId !== undefined && sessions.get(command.tabId)?.token !== command.captureId &&
      request?.resumeCapture?.captureId !== command.captureId) return;
  if (request) request.cancelled = true;
  const session = sessions.get(command.tabId);
  if (session && !session.ready) session.stopping = true;
}
function notifyControl() { appControl.publish(); }
const appControl = new AppControl({
  runtime: chrome.runtime, alarms: chrome.alarms, capabilities: ["independent-sessions-v1", "call-evidence-v1", "page-state-v1", "multi-stream-v1", "resume-capture-v1", "heartbeat-v1"], cancelPending: cancelPendingStart,
  connected: async () => {
    await recovery;
    const tabs = await chrome.tabs.query({});
    await Promise.all(tabs.filter(tab => /^https?:/.test(tab.url || "")).map(tab => chrome.tabs.sendMessage(tab.id, { type: "cassini-connect" }).catch(() => {})));
  },
  snapshot: async () => { await recovery; return [await meetings(), controlStatus()]; },
  command: controlCommand
});
appControl.start();
