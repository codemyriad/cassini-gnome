const statusText = document.querySelector("#status");
let busy = false, reloadTabId = null;
async function command(type, mode) {
  if (busy && type === "status") return;
  if (type !== "status") { busy = true; document.querySelectorAll("button").forEach(button => button.disabled = true); }
  try {
    const result = await chrome.runtime.sendMessage({ target: "background", type, mode, tabId:reloadTabId });
    if (result.error) throw new Error(result.error);
    statusText.textContent = result.message;
    const health = result.browserHealth;
    const broken = health?.tabs.find(tab => tab.state === "reloadRequired");
    reloadTabId = broken?.tabId ?? null;
    document.querySelector("#health").hidden = !broken;
    document.querySelector("#health").textContent = broken?.message || "";
    document.querySelector("#reload").hidden = !broken;
    document.querySelector("#diagnostics").textContent = health ?
      `Extension ${health.extensionVersion}\nInstance ${health.extensionInstance}\nInstall: ${health.installType || "checking"}\nCapture files: ${health.filesOk === undefined ? "checking" : health.filesOk ? "readable" : "unavailable"}\nWorker: ${health.worker}\nNative connection: ${health.nativeControl}\nMeeting tabs: ${health.state}` +
      health.tabs.map(tab => `\nTab ${tab.tabId}: ${tab.state}; hook ${tab.hookSeen ? "seen" : "not seen"}; relay ${tab.relayAlive ? "alive" : "not connected"}`).join("") : "Checking…";
    document.querySelector("#path").textContent = result.path || "";
    document.querySelector("#tracks").hidden = result.recording;
    document.querySelector("#tab").hidden = result.recording;
    document.querySelector("#stop").hidden = !result.recording;
    document.querySelector("#stop").disabled = !!result.stopping;
  } catch (error) { statusText.textContent = error.message; }
  finally {
    if (type !== "status") { busy = false; document.querySelectorAll("button").forEach(button => button.disabled = false); }
  }
}
document.querySelector("#tracks").onclick = () => command("start", "tracks");
document.querySelector("#tab").onclick = () => command("start", "tab");
document.querySelector("#stop").onclick = () => command("stop");
document.querySelector("#reload").onclick = () => command("reloadMeetingTab");
void command("status");
setInterval(() => command("status"), 1000);
