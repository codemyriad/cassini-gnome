(() => {
  const port = chrome.runtime.connect({ name: "cassini-frame" });
  let token = null;
  port.onMessage.addListener(message => {
    if (message.type === "start") token = message.token;
    if (!token) return;
    window.postMessage({ channel: "cassini-control", ...message,
      moduleURL: chrome.runtime.getURL("capture.js"), workletURL: chrome.runtime.getURL("pcm-worklet.js") }, "*");
  });
  window.addEventListener("message", event => {
    if (event.source !== window || event.data?.channel !== "cassini-page" || !token || event.data.token !== token) return;
    const { type, packet, request, message } = event.data;
    // Page code is untrusted. It may supply audio, never filesystem paths or host commands.
    if (type === "packet" && ["track", "audio"].includes(packet?.type)) {
      if (JSON.stringify(packet).length <= 400000) port.postMessage({ type, packet, request });
    } else if (["ready", "stopped", "error"].includes(type)) {
      port.postMessage({ type, message: typeof message === "string" ? message.slice(0, 1000) : undefined });
      if (type === "stopped") token = null;
    }
  });
  port.onDisconnect.addListener(() => {
    if (token) window.postMessage({ channel: "cassini-control", type: "stop", token }, "*");
    token = null;
  });
})();
