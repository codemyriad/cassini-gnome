import { popupState } from './popup-state.mjs';
import { copyDiagnostics } from './tab-evidence.mjs';

const element = id => document.getElementById(id);
let pending = false, actionPending = false, reloadTabId = null, reloadedTabId = null, diagnosticText = '';
let requestGeneration = 0;
// A refresh asked for while a poll or action is outstanding runs once it ends.
let refreshQueued = false, lastResult = null, lastTab = null;
// The button's target is the tab and action displayed when it was rendered.
let recordTarget = null, recordError = null;
function boundedReply(promise, ms = 4000) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error('The extension did not answer in time. Recording status is unverified; check Cassini.')), ms);
  })]).finally(() => clearTimeout(timer));
}
function showRecordError(message) {
  element('record-error').textContent = message;
  element('record-error').hidden = !message;
}
function renderRecord(record, tab) {
  // A refusal stays visible until this tab's offered action changes.
  if (recordError && (recordError.tabId !== tab?.id || recordError.action !== record?.action || record?.enabled === false && !record.hint)) {
    recordError = null; showRecordError('');
  }
  recordTarget = record?.enabled ? {tabId: tab.id, action: record.action} : null;
  element('record').hidden = !record;
  element('record').disabled = !record?.enabled;
  element('record').textContent = record?.label || 'Start recording';
  element('record').dataset.action = record?.action || 'start';
  element('record-hint').textContent = record?.hint || '';
  element('record-hint').hidden = !record?.hint;
}
function showActionError(message) {
  element('action-error').textContent = message;
  element('action-error').hidden = !message;
}
function render(result, tab) {
  lastResult = result; lastTab = tab;
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
  renderRecord(view.record, tab);
  // Do not copy meeting URLs/titles or filesystem paths into the report.
  diagnosticText = JSON.stringify(copyDiagnostics(result, tab?.id), null, 2);
  element('diagnostics').textContent = diagnosticText;
  element('copy').disabled = false;
}
async function refresh() {
  if (pending || actionPending) { refreshQueued = true; return; }
  refreshQueued = false;
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
    recordError = null; showRecordError('');
    renderRecord(null, null);
    diagnosticText = JSON.stringify({schemaVersion:1,error:'statusUnavailable'});
    element('diagnostics').textContent = diagnosticText;
    element('copy').disabled = false;
  } finally {
    pending = false; element('tab-status').setAttribute('aria-busy', 'false');
    drainRefresh();
  }
}
function drainRefresh() {
  if (refreshQueued && !pending && !actionPending) void refresh();
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
  finally { actionPending = false; element('reload').disabled = false; drainRefresh(); }
});
element('record').addEventListener('click', async () => {
  if (actionPending || recordTarget === null) return;
  // Never re-read the active tab here: the ask targets the tab the user saw.
  const {tabId, action} = recordTarget;
  actionPending = true;
  ++requestGeneration;
  recordTarget = null;
  recordError = null; showRecordError('');
  // Render the whole pending card (tone, text, note, button) through popupState.
  // recordTarget only exists after a render of this tab, so lastResult is its view.
  const confirmed = lastResult;
  render({...confirmed, tabId, recordRequest: {action, state: 'pending'}}, lastTab);
  lastResult = confirmed;
  try {
    const result = await boundedReply(chrome.runtime.sendMessage({target: 'background', type: 'recordRequest', action, tabId}), 15000);
    if (!result || result.error) throw new Error(result?.error || 'Cassini could not do that. Check the Cassini app.');
  } catch (error) {
    recordError = {tabId, action};
    // Nothing changed: restore the last confirmed card at once rather than
    // leaving Starting…/Stopping… beside the refusal until the next poll.
    if (lastTab?.id === tabId) render(lastResult, lastTab);
    showRecordError(error.message);
  } finally {
    actionPending = false;
    // A poll invalidated by the click may still be outstanding; queue behind it.
    refreshQueued = true;
    drainRefresh();
  }
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
