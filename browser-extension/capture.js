export function base64(buffer) {
  const bytes = new Uint8Array(buffer);
  let text = "";
  for (let i = 0; i < bytes.length; i++) text += String.fromCharCode(bytes[i]);
  return btoa(text);
}

async function bounded(promise, message, schedule = setTimeout, cancel = clearTimeout) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => { timer = schedule(() => reject(new Error(message)), 4000); })]);
  } finally { cancel(timer); }
}

const clocks = new WeakMap();
const retirementReason = error => error?.code === "callChanged" ? "callChanged" :
  error?.code === "state" && error.scope === "source" && error.retired === true ? "ended" :
  error?.code === "state" && ["source", "command"].includes(error.scope) ? "stopped" : null;
const resuming = new WeakMap();
function resumeSuspended(context) {
  if (resuming.has(context)) return resuming.get(context);
  const began = performance.now(), render = context.currentTime || 0;
  const task = bounded(Promise.resolve().then(() => context.resume()), "Audio capture did not resume within four seconds.").then(() => {
    if (context.state !== "running") throw new Error("Audio capture remains suspended.");
    const gap = Math.max(0, performance.now() - began - ((context.currentTime || 0) - render) * 1000);
    if (clocks.has(context)) clocks.set(context, clocks.get(context) + gap);
  });
  resuming.set(context, task);
  task.then(() => resuming.delete(context), () => resuming.delete(context));
  return task;
}

async function captureOnce({ stream, id, label, kind, origin, workletURL, send, fail, audible = false, enabled = () => true, channels = 1, sharedContext, getSharedContext, warn = () => {}, sourceTrackId, reportOutcomes = false, onAudio = () => {}, schedule, cancel }) {
  sharedContext ||= getSharedContext?.();
  const context = sharedContext || new AudioContext({ sampleRate: 48000 });
  const wait = (promise, message) => bounded(promise, message, schedule, cancel);
  let source, node, silent, remotePlayback, stopping = false, ending = false, failure = null;
  let pendingFrames = 0, sequence = 0, flushed, droppedFrames = 0;
  const waiting = [], inFlight = new Set();
  let stateChanged, announced = false, ended = false, endOffsetMs = null;
  const end = async reason => {
    if (!reportOutcomes || !announced || ended) return;
    ended = true;
    try { await send({ type: "trackEnd", id, reason, droppedFrames, endOffsetMs }); }
    catch (error) { if (!retirementReason(error)) throw error; }
  };
  function pump() {
    while (waiting.length && inFlight.size < 4 && !failure && !ended) {
      const { message, frames } = waiting.shift();
      const task = Promise.resolve().then(() => send(message)).then(() => {
        endOffsetMs = Math.max(endOffsetMs ?? 0, message.offsetMs + frames * 1000 / context.sampleRate);
        if (!failure && !stopping && !ending && !ended) onAudio();
      }).catch(abort).finally(() => {
        pendingFrames -= frames; inFlight.delete(task); pump();
      });
      inFlight.add(task);
    }
  }
  function abort(error) {
    if (failure) return;
    failure = error;
    waiting.length = 0;
    fail(error);
  }
  try {
    await wait(context.audioWorklet.addModule(workletURL), "This meeting page blocks separate-track recording. Record the whole tab instead.");
    // Chromium may not decode a receiver for Web Audio until a media renderer exists.
    // Keep a silent renderer alive (crbug.com/40094084), without playing remote audio twice.
    if (kind === "remote") {
      remotePlayback = new Audio();
      remotePlayback.muted = true;
      remotePlayback.srcObject = stream;
      await wait(remotePlayback.play(), "A participant’s audio did not start. Record the whole tab instead.");
    }
    source = context.createMediaStreamSource(stream);
    node = new AudioWorkletNode(context, "cassini-pcm", {
      channelCount: channels, channelCountMode: "explicit", outputChannelCount: [channels],
      processorOptions: { channels }
    });
    silent = context.createGain();
    silent.gain.value = 0;
    // A newly constructed worklet can observe its initial global frame before
    // the device clock catches up. Warm only the disconnected graph; no source
    // PCM is admitted or timestamp adjusted during this readiness handshake.
    const ready = new Promise(resolve => { node.port.onmessage = ({data}) => {if (data.ready) resolve();}; });
    node.connect(silent).connect(context.destination);
    if (audible) source.connect(context.destination);
    await wait(context.resume(), "Click somewhere in the meeting page so the browser allows audio, then try again. Or record the whole tab.");
    if (context.state !== "running") throw new Error("Audio capture is suspended. Interact with the meeting page, then try again or use Tab audio.");
    await wait(ready, "Audio render clock did not become ready.");
    source.connect(node);
    let clockOffset = clocks.get(context);
    await send({ type: "track", id, label, kind, sampleRate: context.sampleRate, channels,
      ...(sourceTrackId ? { sourceTrackId } : {}) });
    announced = true;
    node.onprocessorerror = () => abort(new Error("Audio capture stopped unexpectedly."));
    stateChanged = () => {
      if (stopping || ending || context.state === "running") return;
      if (["suspended", "interrupted"].includes(context.state)) {
        void resumeSuspended(context).catch(abort);
      } else abort(new Error("Browser audio capture closed unexpectedly."));
    };
    context.addEventListener?.("statechange", stateChanged);
    node.port.onmessage = ({ data }) => {
      if (data.stopped) { if (stopping) ending = true; flushed?.(); return; }
      if (failure || ending || ended) return;
      const frames = data.pcm.byteLength / (channels * 2);
      clockOffset = clocks.get(context) ?? clockOffset;
      if (clockOffset === undefined) {
        // resume() can resolve before the device/render clock has started. Calibrate
        // once samples exist, then preserve render-frame timing rather than timing
        // packets by their arrival on the main thread.
        const stamp = context.getOutputTimestamp?.();
        clockOffset = stamp?.contextTime > 0 && stamp?.performanceTime > 0
          ? performance.timeOrigin + stamp.performanceTime - origin - stamp.contextTime * 1000
          : performance.timeOrigin + performance.now() - origin - (data.frame + frames) / context.sampleRate * 1000;
      }
      if (pendingFrames + frames > context.sampleRate * 10) {
        droppedFrames += frames;
        warn("One track fell more than ten seconds behind and has a gap. The other tracks continue.", {kind:"gap"});
        return;
      }
      const offsetMs = Math.max(0, clockOffset + data.frame / context.sampleRate * 1000);
      if (endOffsetMs !== null && offsetMs - endOffsetMs > 60000) {
        droppedFrames += frames;
        abort(new Error("A suspended source needs a new audio span."));
        return;
      }
      pendingFrames += frames;
      clocks.set(context, clockOffset);
      // For sender tracks, respect track enabled/muted state and sender suspension.
      if (!enabled()) new Uint8Array(data.pcm).fill(0);
      const message = { type: "audio", id, sequence: sequence++,
        offsetMs, pcm: base64(data.pcm) };
      waiting.push({ message, frames });
      pump();
    };
    node.port.postMessage("start");
    return {
      get failed() { return !!failure; },
      get droppedFrames() { return droppedFrames; },
      async stop(reason = "stopped") {
        if (stopping) return;
        stopping = true;
        if (context.state === "running") {
          await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error("Audio capture did not finish in time.")), 2000);
            flushed = () => { clearTimeout(timer); resolve(); };
            node.port.postMessage("stop");
          }).catch(abort);
        }
        // Accept the worklet's final flush through its stopped barrier, then
        // fence even a retained callback before draining admitted PCM.
        ending = true;
        node.port.onmessage = null; node.onprocessorerror = null;
        source.disconnect(); node.disconnect(); silent.disconnect();
        if (remotePlayback) { remotePlayback.pause(); remotePlayback.srcObject = null; }
        while (inFlight.size) await Promise.allSettled([...inFlight]);
        waiting.length = 0;
        context.removeEventListener?.("statechange", stateChanged);
        if (!sharedContext) await context.close();
        await end(retirementReason(failure) || (reason === "retry" ? "retry" : failure ? "failed" : reason));
        if (failure && !retirementReason(failure)) throw failure;
      }
    };
  } catch (error) {
    stopping = ending = true;
    if (node) { node.port.onmessage = null; node.onprocessorerror = null; }
    waiting.length = 0;
    source?.disconnect(); node?.disconnect(); silent?.disconnect();
    if (remotePlayback) { remotePlayback.pause(); remotePlayback.srcObject = null; }
    context.removeEventListener?.("statechange", stateChanged);
    if (!sharedContext) await context.close();
    await end(retirementReason(error) || "failed").catch(() => {});
    throw error;
  }
}


// The same policy is exercised with a fake monotonic clock in both capture and
// discovery tests. Quiet PCM is healthy; only transport/source failures retry.
export function retryDelay(attempts) {
  return [1000, 5000, 15000][Math.max(0, attempts)] ?? 30000;
}

export function reconcile(state, attempts, lastHealthyAt, now) {
  if (state.stopping || state.live === false) return "giveUpAndReport";
  if (state.status === "recording") return "none";
  if (state.failedAt === null || state.failedAt === undefined) return "reattach";
  const recovered = lastHealthyAt !== null && lastHealthyAt !== undefined && state.failedAt - lastHealthyAt >= 60000;
  return now >= state.failedAt + retryDelay(recovered ? 0 : attempts) ? "reattach" : "none";
}

export async function capture(options) {
  let current = null, ending = false, terminal = null, retryTimer = null, retries = 0;
  let recovering = false, retiring = false, retiredDrops = 0, healthySince = null;
  let restarting = Promise.resolve();
  const warn = options.warn || (() => {});
  const now = options.clock || (() => performance.now());
  const later = options.schedule || ((fn, ms) => setTimeout(fn, ms));
  const cancel = options.cancel || (timer => clearTimeout(timer));
  const live = options.live || (() => {
    const tracks = options.stream.getAudioTracks?.();
    return !tracks || tracks.some(track => track.readyState === "live");
  });
  const healthy = () => {
    if (recovering) warn(undefined, {kind:"recovered", sourceTrackId:options.sourceTrackId || options.id});
    recovering = false;
    healthySince ??= now();
    if (now() - healthySince >= 60000) retries = 0;
    options.onAudio?.();
  };
  const schedule = value => {
    if (ending || retiring || terminal || retryTimer !== null) return;
    const error = value instanceof Error ? value : new Error(String(value));
    if (retirementReason(error) && retirementReason(error) !== "ended") {
      // The token is deliberately fenced or already saved by the native app.
      retiring = true;
      if (retryTimer !== null) cancel(retryTimer);
      retryTimer = null;
      return;
    }
    const at = now();
    if (error.code === "limit" || reconcile({ live: live(), failedAt: at }, retries, healthySince, at) === "giveUpAndReport") {
      terminal = error; options.fail?.(error.message); return;
    }
    if (healthySince !== null && at - healthySince >= 60000) retries = 0;
    healthySince = null;
    recovering = true;
    warn(error.message, {kind:"retry", sourceTrackId:options.sourceTrackId || options.id});
    const delay = options.retryDelayMs ?? retryDelay(retries);
    retryTimer = later(async () => {
      retryTimer = null;
      restarting = (async () => {
        const previous = current; current = null;
        retiredDrops += previous?.droppedFrames || 0;
        await previous?.stop("retry").catch(() => {});
        if (ending) return;
        if (!live()) { terminal = error; options.fail?.(error.message); return; }
        retries++;
        try {
          const id = options.nextSpanId ? options.nextSpanId() : crypto.randomUUID();
          if (!id) throw Object.assign(new Error("This recording has reached its track limit."), {code:"limit",scope:"source"});
          current = await captureOnce({ ...options, id, fail: schedule, onAudio: healthy });
        } catch (next) { schedule(next); }
      })();
      await restarting;
    }, delay);
  };
  try { current = await captureOnce({ ...options, fail: schedule, onAudio: healthy }); }
  catch (error) { schedule(error); }
  return {
    get failed() { return !!terminal; },
    get state() { return ending || retiring ? "stopped" : terminal ? "failed" : recovering ? "retrying" : "recording"; },
    get droppedFrames() { return retiredDrops + (current?.droppedFrames || 0); },
    async stop(reason = "stopped") {
      if (ending) return;
      ending = true;
      if (retryTimer !== null) cancel(retryTimer);
      retryTimer = null;
      await restarting;
      try { await current?.stop(reason); } catch (error) { if (terminal) throw error; warn(error.message); }
      if (terminal) throw terminal;
    }
  };
}
