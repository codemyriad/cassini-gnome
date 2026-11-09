import { popupState } from './popup-state.mjs';
import { copyDiagnostics } from './tab-evidence.mjs';

const element = id => document.getElementById(id);
let pending = false, actionPending = false, reloadTabId = null, reloadedTabId = null, diagnosticText = '';
let requestGeneration = 0;
function boundedReply(promise) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error('The extension did not answer in time. Recording status is unverified; check Cassini.')), 4000);
  })]).finally(() => clearTimeout(timer));
}
function showActionError(message) {
  element('action-error').textContent = message;
  element('action-error').hidden = !message;
}
function render(result, tab) {
  const current = result.browserHealth?.tabs?.find(item => item.tabId === tab?.id);
  const capture = result.tabId === tab?.id && (result.recording === true || result.starting === true);
  if (tab?.id !== reloadedTabId || current || capture) reloadedTabId = null;
  const view = popupState(result, tab, reloadedTabId);
  reloadTabId = view.reloadTabId;
  if (reloadTabId === null) showActionError('');
  element('connection').textContent = view.connected ? 'App link connected · audio checked separately below' : 'Cassini app link is not connected';
  element('connection').dataset.connected = String(view.connected);
  element('tab-title').textContent = tab?.title || 'No meeting tab selected';
  try { element('site').textContent = new URL(tab.url).hostname; }
  catch { element('site').textContent = ''; }
  element('card').dataset.tone = view.tone;
  element('status').textContent = view.title;
  element('explanation').textContent = view.description;
  element('recording-note').textContent = view.note;
  element('installation-warning').hidden = !view.fileIssue;
  element('reload').hidden = reloadTabId === null;
  element('reload-warning').hidden = reloadTabId === null;
  // Do not copy meeting URLs/titles or filesystem paths into the report.
  diagnosticText = JSON.stringify(copyDiagnostics(result, tab?.id), null, 2);
  element('diagnostics').textContent = diagnosticText;
  element('copy').disabled = false;
}
async function refresh() {
  if (pending || actionPending) return;
  pending = true;
  const generation = ++requestGeneration;
  try {
    const {result, tab} = await boundedReply((async () => {
      const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
      const result = await chrome.runtime.sendMessage({target: 'background', type: 'status',
        ...(Number.isSafeInteger(tab?.id) && tab.id >= 0 ? {tabId: tab.id} : {})});
      return {result, tab};
    })());
    if (generation !== requestGeneration) return;
    if (!result || result.error) throw new Error(result?.error || 'No reply from the extension');
    render(result, tab);
  } catch (error) {
    if (generation !== requestGeneration) return;
    reloadTabId = null;
    showActionError('');
    element('connection').textContent = 'App connection is unverified';
    element('connection').dataset.connected = 'false';
    element('installation-warning').hidden = true;
    element('card').dataset.tone = 'warning';
    element('status').textContent = 'Meeting detection is unavailable';
    element('explanation').textContent = error.message;
    element('recording-note').textContent = 'Recording status is unavailable. Check Cassini.';
    element('reload').hidden = element('reload-warning').hidden = true;
    diagnosticText = JSON.stringify({schemaVersion:1,error:'statusUnavailable'});
    element('diagnostics').textContent = diagnosticText;
    element('copy').disabled = false;
  } finally { pending = false; element('tab-status').setAttribute('aria-busy', 'false'); }
}
element('reload').addEventListener('click', async () => {
  if (actionPending || reloadTabId === null) return;
  // Keep the action bound to the displayed tab even if an in-flight poll finishes.
  const tabId = reloadTabId;
  actionPending = true;
  ++requestGeneration;
  element('reload').disabled = true;
  showActionError('');
  try {
    const result = await chrome.runtime.sendMessage({target: 'background', type: 'reloadMeetingTab', tabId});
    if (!result || result.error) throw new Error(result?.error || 'The tab could not be reloaded');
    reloadedTabId = tabId;
    element('status').textContent = 'Tab reloaded · rejoin your call';
    element('explanation').textContent = 'Waiting for fresh meeting sources. Reloading does not confirm that audio is available.';
    element('card').dataset.tone = 'neutral';
    reloadTabId = null;
    element('reload').hidden = element('reload-warning').hidden = true;
  } catch (error) { showActionError(error.message); }
  finally { actionPending = false; element('reload').disabled = false; }
});
element('copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(diagnosticText);
    element('copy-result').textContent = 'Diagnostics copied.';
  } catch { element('copy-result').textContent = 'Could not copy. Select the connection details above to copy them manually.'; }
  element('copy-result').hidden = false;
});
void refresh();
setInterval(() => void refresh(), 1000);
