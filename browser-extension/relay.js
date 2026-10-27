(() => {
  let port = null, token = null;
  function connect() {
    if (port) return;
    const connected = chrome.runtime.connect({ name: "cassini-frame" });
    port = connected;
  port.onMessage.addListener(message => {
    if (message.type === "start") token = message.token;
    if (!token) return;
    window.postMessage({ channel: "cassini-control", ...message,
      moduleURL: chrome.runtime.getURL("capture.js"), workletURL: chrome.runtime.getURL("pcm-worklet.js") }, "*");
  });
    connected.onDisconnect.addListener(() => {
      if (port !== connected) return;
      if (token) window.postMessage({ channel: "cassini-control", type: "stop", token }, "*");
      token = null; port = null;
    });
  }
  chrome.runtime.onMessage.addListener((message, _sender, respond) => {
    if (message.type !== "cassini-connect") return;
    connect(); respond({ connected: true });
  });
  connect();
  window.addEventListener("message", event => {
    if (event.source !== window || event.data?.channel !== "cassini-page" || !token || event.data.token !== token) return;
    const { type, packet, request, message } = event.data;
    // Page code is untrusted. It may supply audio, never filesystem paths or host commands.
    if (type === "packet" && ["track", "audio"].includes(packet?.type)) {
      if (packet.type === "track" && typeof packet.label === "string") packet.label = [...packet.label.replace(/[\u0000-\u001f\u007f-\u009f]/g, "")].slice(0, 200).join("");
      if (JSON.stringify(packet).length <= 400000) port?.postMessage({ type, packet, request });
    } else if (["ready", "stopped", "error", "warning"].includes(type)) {
      port?.postMessage({ type });
      if (type === "stopped") token = null;
    }
  });
})();
