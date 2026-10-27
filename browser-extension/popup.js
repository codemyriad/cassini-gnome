const statusText = document.querySelector("#status");
let busy = false;
async function command(type, mode) {
  if (busy && type === "status") return;
  if (type !== "status") { busy = true; document.querySelectorAll("button").forEach(button => button.disabled = true); }
  try {
    const result = await chrome.runtime.sendMessage({ target: "background", type, mode });
    if (result.error) throw new Error(result.error);
    statusText.textContent = result.message;
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
void command("status");
setInterval(() => command("status"), 1000);
