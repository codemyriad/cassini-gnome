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

export async function capture({ stream, id, label, kind, origin, workletURL, send, fail, audible = false, enabled = () => true, channels = 1, sharedContext, warn = () => {} }) {
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
    await bounded(context.audioWorklet.addModule(workletURL), "The meeting page blocked the audio worklet. Use Tab audio instead.");
    // Chromium may not decode a receiver for Web Audio until a media renderer exists.
    // Keep a silent renderer alive (crbug.com/40094084), without playing remote audio twice.
    if (kind === "remote") {
      remotePlayback = new Audio();
      remotePlayback.muted = true;
      remotePlayback.srcObject = stream;
      await bounded(remotePlayback.play(), "The remote audio source did not start. Use Tab audio instead.");
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
    await bounded(context.resume(), "Interact with the meeting page to allow audio, then try again or use Tab audio.");
    if (context.state !== "running") throw new Error("Audio capture is suspended. Interact with the meeting page, then try again or use Tab audio.");
    let clockOffset = clocks.get(context);
    await send({ type: "track", id, label, kind, sampleRate: context.sampleRate, channels });
    node.onprocessorerror = () => abort(new Error("The audio worklet stopped unexpectedly."));
    stateChanged = () => {
      if (!ending && context.state !== "running") abort(new Error("Browser audio capture was suspended. The partial recording was saved."));
    };
    context.addEventListener?.("statechange", stateChanged);
    node.port.onmessage = ({ data }) => {
      if (data.stopped) { flushed?.(); return; }
      if (failure) return;
      const frames = data.pcm.byteLength / (channels * 2);
      clockOffset ??= clocks.get(context);
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
        warn("A source exceeded its ten-second audio buffer. Its timeline contains a gap; other sources continue.");
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
            const timer = setTimeout(() => reject(new Error("Audio worklet did not finish.")), 2000);
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
