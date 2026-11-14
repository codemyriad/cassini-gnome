// Only describe evidence exposed by the current worker. A native port, a page
// hook, a recording request, and confirmed saved audio are different things.
import { tabEvidenceView } from './tab-evidence.mjs';

// popup-record-v1: Start/Stop asks the connected Cassini app. The button only
// reflects this tab's own capture state and says why it is unavailable.
export function recordControl(result, tab, current) {
  if (!result?.browserHealth || !tab || !/^https?:/.test(tab.url || '')) return null;
  const mine = result.tabId === tab.id;
  const app = ['available', 'disconnected', 'unsupported'].includes(result.appRecording) ? result.appRecording : 'unsupported';
  const request = mine && ['start', 'stop'].includes(result.recordRequest?.action) ? result.recordRequest : null;
  if (request) return {action: request.action, enabled: false, hint: '', label: request.action === 'start' ? 'Starting…' : 'Stopping…'};
  if (mine && (result.recording === true || result.starting === true)) {
    if (result.stopping === true) return {action: 'stop', enabled: false, hint: '', label: 'Saving…'};
    const hint = app === 'available' ? '' : app === 'disconnected' ?
      'Cassini is not connected. Stop the recording in the Cassini app.' :
      'This version of Cassini cannot stop recordings from the browser. Stop it in the Cassini app.';
    return {action: 'stop', enabled: !hint, hint, label: 'Stop recording'};
  }
  const hint = app === 'disconnected' ? 'Open Cassini to record from the browser.' :
    app === 'unsupported' ? 'This version of Cassini cannot start recordings from the browser. Update Cassini or use the Cassini app.' :
    current?.state === 'reloadRequired' ? 'Reload the tab before recording.' :
    current?.state === 'checking' ? 'Wait until the page check finishes.' :
    current?.state !== 'ready' ? 'Join the call first. You can record once meeting audio is available.' : '';
  return {action: 'start', enabled: !hint, hint, label: 'Start recording'};
}

export function popupState(result, tab, reloadedTabId = null) {
  const view = popupStatus(result, tab, reloadedTabId);
  const health = result?.browserHealth;
  // Without a health row, this tab's own page evidence still says whether it
  // needs a reload or is being checked. Evidence-only `ready` is only a relay
  // handshake (no sources yet), so it never enables Start.
  const page = result?.tabEvidence?.tabId === tab?.id ? tabEvidenceView(result, tab?.id).transport.page : null;
  const current = health?.tabs?.find(item => item.tabId === tab?.id) ||
    (page === 'reloadRequired' || page === 'checking' ? {state: page} : null);
  const record = recordControl(result, tab, current);
  const request = record && result.tabId === tab?.id ? result.recordRequest : null;
  if (request?.action === 'start') return {...view, record, tone: 'neutral', title: 'Asking Cassini to start recording…',
    description: 'Cassini starts the recording with your settings for browser meetings. Waiting for it to confirm.', note: 'Recording is not yet confirmed'};
  if (request?.action === 'stop') return {...view, record, title: 'Asking Cassini to stop recording…',
    description: 'Waiting for Cassini to stop and save this recording.'};
  return {...view, record};
}

function popupStatus(result, tab, reloadedTabId) {
  const health = result?.browserHealth;
  const current = health?.tabs?.find(item => item.tabId === tab?.id) ||
    (result?.tabEvidence?.tabId===tab?.id ? {state:tabEvidenceView(result,tab?.id).transport.page} : null);
  const capture = result?.recording === true && result.tabId === tab?.id;
  const view = {connected: health?.nativeControl === 'connected', fileIssue: health?.filesOk === false, tone: 'neutral', reloadTabId: null,
    note: 'Start recording asks the Cassini app on this computer to record this tab.'};
  if (!health) return {...view, title: 'Meeting detection is unavailable',
    description: 'The extension did not return connection details. Try reopening this popup.'};
  if (!tab || !/^https?:/.test(tab.url || '')) return {...view, title: 'This page isn’t supported',
    description: 'Open this popup on the browser tab where your meeting is running.'};
  if (result.starting === true && result.tabId === tab.id) return {...view,
    title: result.stopping ? 'Cancelling browser capture…' : 'Preparing browser capture…',
    description: result.stopping ? 'Waiting for the capture request to finish stopping.' :
      'Cassini has requested capture for this tab. Waiting for the browser to finish preparing it.',
    note: 'Audio capture is not yet confirmed'};
  if (capture) return {...view, tone: result.failedSources?.length ? 'warning' : 'good',
    title: result.stopping ? 'Finishing browser capture' : result.tracks > 0 ? 'Browser capture is active' : 'Waiting for browser audio',
    description: result.failedSources?.length ? 'Some browser audio sources have a problem. Check the meeting in Cassini.' :
      result.tracks > 0 ? 'The extension is capturing audio for this tab. Check Cassini for the recording and its saved audio.' :
        'No audio tracks have been reported for this capture yet. Check the meeting in Cassini.',
    note: result.tracks > 0 ? `${result.tracks} browser audio ${result.tracks === 1 ? 'track' : 'tracks'} · stop here or in Cassini` : 'Waiting for browser audio tracks'};
  if (reloadedTabId === tab.id && !current) return {...view, title: 'Tab reloaded · rejoin your call',
    description: 'Waiting for fresh meeting sources. Reloading does not confirm that audio is available.'};
  if (current?.state === 'reloadRequired') return {...view, tone: 'warning', reloadTabId: tab.id,
    title: 'This tab needs to reload', description: 'Cassini cannot reach the meeting’s audio sources on this page. Reload the tab, then rejoin your call.'};
  if (current?.state === 'checking') return {...view, title: 'Checking the page connection…',
    description: 'The extension is checking whether it can reach this meeting’s audio sources. Recording is not confirmed.'};
  if (result?.tabEvidence?.tabId === tab.id) {
    const evidence = tabEvidenceView(result,tab.id);
    if (evidence.callEvidence.state === 'unknown') return {...view, title:'Interactive session is unverified',
      description:'Cassini has not verified an interactive media session. Listen-only calls are not recorded automatically; start recording manually once meeting audio is available.'};
    if (evidence.callEvidence.state === 'active') return {...view, tone:'good', title:'Interactive media session detected',
      description:evidence.captureCapability.kind === 'separated' ?
        'Live audio is flowing both ways. This can include microphone tests or early media.' :
        'An interactive media session is detected. Audio capture availability is checked separately in Cassini.'};
  }
  if (current?.state === 'ready') return {...view, tone: 'good', title: 'Meeting sources detected',
    description: view.connected ? 'The extension can reach audio sources on this page. You can start recording this tab.' :
      'The extension can reach audio sources on this page. Open Cassini to connect the app before recording.'};
  return {...view, title: 'No meeting sources detected',
    description: 'No meeting audio sources are currently reported for this tab. If your call is active, check the connection details. This does not confirm that the page connection is working.'};
}
