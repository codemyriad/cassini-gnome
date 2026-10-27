export function base64(buffer) {
  const bytes = new Uint8Array(buffer);
  let text = "";
  for (let i = 0; i < bytes.length; i++) text += String.fromCharCode(bytes[i]);
  return btoa(text);
}

async function bounded(promise, message) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), 4000); })]);
  } finally { clearTimeout(timer); }
}

const clocks = new WeakMap();
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

async function captureOnce({ stream, id, label, kind, origin, workletURL, send, fail, audible = false, enabled = () => true, channels = 1, sharedContext, warn = () => {} }) {
  const context = sharedContext || new AudioContext({ sampleRate: 48000 });
  let source, node, silent, remotePlayback, ending = false, failure = null;
  let pendingFrames = 0, sequence = 0, flushed, droppedFrames = 0;
  const waiting = [], inFlight = new Set();
  let stateChanged;
  function pump() {
    while (waiting.length && inFlight.size < 4 && !failure) {
      const { message, frames } = waiting.shift();
      const task = Promise.resolve().then(() => send(message)).catch(abort).finally(() => {
        pendingFrames -= frames; inFlight.delete(task); pump();
      });
      inFlight.add(task);
    }
  }
  function abort(error) {
    if (failure) return;
    failure = error;
    waiting.length = 0;
    fail(error.message);
  }
  try {
    await bounded(context.audioWorklet.addModule(workletURL), "This meeting page blocks separate-track recording. Record the whole tab instead.");
    // Chromium may not decode a receiver for Web Audio until a media renderer exists.
    // Keep a silent renderer alive (crbug.com/40094084), without playing remote audio twice.
    if (kind === "remote") {
      remotePlayback = new Audio();
      remotePlayback.muted = true;
      remotePlayback.srcObject = stream;
      await bounded(remotePlayback.play(), "A participant’s audio did not start. Record the whole tab instead.");
    }
    source = context.createMediaStreamSource(stream);
    node = new AudioWorkletNode(context, "cassini-pcm", {
      channelCount: channels, channelCountMode: "explicit", outputChannelCount: [channels],
      processorOptions: { channels }
    });
    silent = context.createGain();
    silent.gain.value = 0;
    source.connect(node).connect(silent).connect(context.destination);
    if (audible) source.connect(context.destination);
    await bounded(context.resume(), "Click somewhere in the meeting page so the browser allows audio, then try again. Or record the whole tab.");
    if (context.state !== "running") throw new Error("Audio capture is suspended. Interact with the meeting page, then try again or use Tab audio.");
    let clockOffset = clocks.get(context);
    await send({ type: "track", id, label, kind, sampleRate: context.sampleRate, channels });
    node.onprocessorerror = () => abort(new Error("Audio capture stopped unexpectedly."));
    stateChanged = () => {
      if (ending || context.state === "running") return;
      if (["suspended", "interrupted"].includes(context.state)) {
        void resumeSuspended(context).catch(abort);
      } else abort(new Error("Browser audio capture closed unexpectedly."));
    };
    context.addEventListener?.("statechange", stateChanged);
    node.port.onmessage = ({ data }) => {
      if (data.stopped) { flushed?.(); return; }
      if (failure) return;
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
        warn("One track fell more than ten seconds behind and has a gap. The other tracks continue.");
        return;
      }
      pendingFrames += frames;
      clocks.set(context, clockOffset);
      // For sender tracks, respect track enabled/muted state and sender suspension.
      if (!enabled()) new Uint8Array(data.pcm).fill(0);
      const message = { type: "audio", id, sequence: sequence++,
        offsetMs: Math.max(0, clockOffset + data.frame / context.sampleRate * 1000), pcm: base64(data.pcm) };
      waiting.push({ message, frames });
      pump();
    };
    return {
      get failed() { return !!failure; },
      get droppedFrames() { return droppedFrames; },
      async stop() {
        if (ending) return;
        ending = true;
        if (context.state === "running") {
          await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error("Audio capture did not finish in time.")), 2000);
            flushed = () => { clearTimeout(timer); resolve(); };
            node.port.postMessage("stop");
          }).catch(abort);
        }
        source.disconnect(); node.disconnect(); silent.disconnect();
        if (remotePlayback) { remotePlayback.pause(); remotePlayback.srcObject = null; }
        while (inFlight.size) await Promise.allSettled([...inFlight]);
        context.removeEventListener?.("statechange", stateChanged);
        if (!sharedContext) await context.close();
        if (failure) throw failure;
      }
    };
  } catch (error) {
    ending = true;
    source?.disconnect(); node?.disconnect(); silent?.disconnect();
    if (remotePlayback) { remotePlayback.pause(); remotePlayback.srcObject = null; }
    context.removeEventListener?.("statechange", stateChanged);
    if (!sharedContext) await context.close();
    throw error;
  }
}


// Keep retry ownership beside capture so page-hook's terminal failed-track suppression can
// remain terminal. This also serves tab capture; no second discovery pass is performed.
export async function capture(options) {
  let current, ending = false, terminal = null, retryTimer, retries = 0, recovering = false, retiredDrops = 0;
  let restarting = Promise.resolve();
  const warn = options.warn || (() => {});
  const schedule = message => {
    if (ending || terminal || retryTimer) return;
    recovering = true;
    warn("A track was interrupted; trying to resume it.");
    if (retries >= 3) { terminal = new Error(message); options.fail(message); return; }
    retryTimer = setTimeout(() => {
      retryTimer = null;
      restarting = (async () => {
        const previous = current; current = null;
        retiredDrops += previous?.droppedFrames || 0;
        await previous?.stop().catch(() => {});
        if (ending) return;
        retries++;
        try {
          current = await captureOnce({ ...options, id: crypto.randomUUID(),
            label: [...options.label].slice(0, 189).join("") + " (resumed)", fail: schedule });
          recovering = false;
        } catch (error) { schedule(error.message); }
      })();
    }, options.retryDelayMs ?? 5000);
  };
  current = await captureOnce({ ...options, fail: schedule });
  return {
    get failed() { return !!terminal; },
    get state() { return ending ? "stopped" : terminal ? "failed" : recovering ? "retrying" : "recording"; },
    get droppedFrames() { return retiredDrops + (current?.droppedFrames || 0); },
    async stop() {
      if (ending) return;
      ending = true; clearTimeout(retryTimer);
      await restarting;
      try { await current?.stop(); } catch (error) { if (terminal) throw error; warn(error.message); }
      if (terminal) throw terminal;
    }
  };
}
