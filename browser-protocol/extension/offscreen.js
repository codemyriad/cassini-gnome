import { capture } from "./capture.js";
let recording, stream;
const fail = message => { void chrome.runtime.sendMessage({ target: "background", type: "error", message }); };
chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  if (message.target !== "offscreen") return;
  (async () => {
    if (message.type === "start") {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { mandatory: {
        chromeMediaSource: "tab", chromeMediaSourceId: message.streamId
      } }, video: false });
      for (const track of stream.getTracks()) track.addEventListener("ended", () => fail("Tab audio ended. The partial recording was saved."), { once: true });
      recording = await capture({ stream, id: crypto.randomUUID(), label: "Tab audio", kind: "tab", channels: 2,
        sourceTrackId: message.sourceTrackId, reportOutcomes: message.sourceOutcomes === true,
        origin: message.origin, workletURL: new URL("pcm-worklet.js", location.href).href, audible: true, fail,
        warn: (_message, details) => { void chrome.runtime.sendMessage({ target: "background", type: "warning", transient: details?.kind === "retry" }); },
        send: async packet => {
          const result = await chrome.runtime.sendMessage({ target: "background", type: "packet", packet });
          if (result?.error) throw Object.assign(new Error(result.error), {code: result.code, scope: result.scope});
        } });
    } else if (message.type === "stop") {
      if (recording?.state === "retrying") await chrome.runtime.sendMessage({ target: "background", type: "warning" });
      try { await recording?.stop(); }
      finally { stream?.getTracks().forEach(track => track.stop()); recording = null; stream = null; }
    }
    return {};
  })().then(respond, error => {
    stream?.getTracks().forEach(track => track.stop());
    respond({ error: error.message });
  });
  return true;
});
