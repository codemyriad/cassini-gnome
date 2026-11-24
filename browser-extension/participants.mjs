// Display-only roster of who is in the call, read from Nextcloud Talk's own participant cards.
// Names never grant capture eligibility and never enter the content-free diagnostics.

// Injected into the page (ISOLATED world) with chrome.scripting.executeScript: it must stay
// self-contained, with no imports or closure over module state.
export function readTalkParticipants() {
  const seen = new Map();
  for (const media of document.querySelectorAll('audio,video')) {
    const stream = media.srcObject;
    if (!stream || typeof stream.getAudioTracks !== 'function') continue;
    if (!stream.getAudioTracks().some(track => track.readyState === 'live')) continue;
    const card = media.closest('.video-container[id]');
    const participant = card?.id.match(/^(?:placeholder-)?container_(.+)_video_incoming$/)?.[1];
    if (!participant || seen.has(participant)) continue;
    const names = new Set();
    for (const node of card.querySelectorAll('.participant-name')) {
      if (node.closest('.video-container[id]') !== card) continue;
      // Bound inside the page world too, so an oversized name is never transferred.
      const name = [...node.textContent.slice(0, 1024).replace(/\s+/g, ' ').trim()].slice(0, 200).join('');
      if (name) names.add(name);
    }
    if (names.size === 1) seen.set(participant, [...names][0]);
    if (seen.size >= 64) break;
  }
  return [...seen.values()];
}

// Untrusted page text, bounded and stripped before it reaches the popup.
export function cleanParticipants(value) {
  if (!Array.isArray(value)) return [];
  const names = [];
  for (const entry of value.slice(0, 64)) {
    if (typeof entry !== 'string') continue;
    const name = [...entry.replace(/[\p{Cc}\p{Cf}]/gu, '').replace(/\s+/gu, ' ').trim()].slice(0, 200).join('');
    if (name) names.push(name);
  }
  return names;
}
