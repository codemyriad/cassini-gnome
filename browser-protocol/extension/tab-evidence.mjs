// One content-free projection for the popup and manually copied diagnostics.
// Never spread a status object: future fields may contain private page data.
const member = (value, allowed, fallback) => allowed.includes(value) ? value : fallback;
const count = value => Number.isSafeInteger(value) && value >= 0 && value <= 4096 ? value : 0;
const version = value => typeof value === 'string' && /^\d{1,4}\.\d{1,4}\.\d{1,4}$/.test(value) ? value : null;

export function tabEvidenceView(result, tabId) {
  const health = result?.browserHealth;
  const selected = Number.isSafeInteger(tabId) && tabId >= 0;
  const source = selected && result?.tabEvidence?.tabId === tabId ? result.tabEvidence : null;
  const current = selected && Array.isArray(health?.tabs) ? health.tabs.find(row => row?.tabId === tabId) : null;
  const scoped = selected && result?.tabId === tabId;
  return {
    algorithm:'webrtc-interactive-v1',
    callEvidence:{state:member(source?.callEvidence?.state, ['active','inactive','unknown'], 'unknown'),
      reason:member(source?.callEvidence?.reason, ['activeCall','callEnded','nonCall','unverified','unsupported','evidenceConflict'], 'unverified')},
    captureCapability:{kind:member(source?.captureCapability?.kind, ['separated','mixed','unavailable','unknown'], 'unknown'),
      assurance:member(source?.captureCapability?.assurance, ['observed','unknown'], 'unknown')},
    transport:{page:member(source?.transport?.page ?? current?.state, ['ready','checking','reloadRequired','unavailable'], 'unavailable'),
      app:member(health?.nativeControl, ['connected','disconnected'], 'unknown')},
    execution:{recording:scoped && result.recording === true, starting:scoped && result.starting === true,
      stopping:scoped && result.stopping === true, tracks:scoped ? count(result.tracks) : 0,
      failedSources:scoped && Array.isArray(result.failedSources) ? Math.min(4096,result.failedSources.length) : 0}
  };
}

export function copyDiagnostics(result, tabId) {
  return {schemaVersion:1, controlProtocolVersion:1,
    extensionVersion:version(result?.browserHealth?.extensionVersion),
    filesOk:typeof result?.browserHealth?.filesOk === 'boolean' ? result.browserHealth.filesOk : null,
    ...tabEvidenceView(result,tabId)};
}
