// Controlled models, not reverse-engineered assertions about current product internals.
export async function connectReceiveSlots(local, remote, frequencies, { prejoin = false } = {}) {
  const offer = await local.evaluate(async ({ count, prejoin }) => {
    const peer = new RTCPeerConnection();
    for (let i = 0; i < count; i++) peer.addTransceiver('audio', { direction: 'recvonly' });
    if (prejoin) {
      const audio = new AudioContext(), oscillator = audio.createOscillator();
      const destination = audio.createMediaStreamDestination();
      oscillator.connect(destination); oscillator.start(); await audio.resume();
      peer.addTransceiver(destination.stream.getAudioTracks()[0], { direction: 'sendonly' });
      window.topologyPreview = { audio, oscillator, destination };
    }
    window.topologyPeer = peer;
    await peer.setLocalDescription(await peer.createOffer());
    if (peer.iceGatheringState !== 'complete') await new Promise(resolve => peer.addEventListener('icegatheringstatechange', () => {
      if (peer.iceGatheringState === 'complete') resolve();
    }));
    return peer.localDescription.toJSON();
  }, { count: frequencies.length, prejoin });
  const answer = await remote.evaluate(async ({ offer, frequencies, prejoin }) => {
    const peer = new RTCPeerConnection(), audio = new AudioContext({ sampleRate: 48000 });
    const retained = [];
    function tone(values) {
      const output = audio.createMediaStreamDestination();
      for (const frequency of values) {
        const oscillator = audio.createOscillator(), gain = audio.createGain();
        oscillator.frequency.value = frequency; gain.gain.value = 0.25;
        oscillator.connect(gain).connect(output); oscillator.start(); retained.push(oscillator, gain);
      }
      retained.push(output); return output.stream.getAudioTracks()[0];
    }
    await peer.setRemoteDescription(offer);
    await Promise.all(frequencies.map((values, index) => {
      const transceiver = peer.getTransceivers()[index]; transceiver.direction = 'sendonly';
      return prejoin ? undefined : transceiver.sender.replaceTrack(tone(values));
    }));
    await audio.resume(); await peer.setLocalDescription(await peer.createAnswer());
    if (peer.iceGatheringState !== 'complete') await new Promise(resolve => peer.addEventListener('icegatheringstatechange', () => {
      if (peer.iceGatheringState === 'complete') resolve();
    }));
    window.topologyRemote = { peer, audio, retained, tone };
    return peer.localDescription.toJSON();
  }, { offer, frequencies, prejoin });
  await local.evaluate(answer => topologyPeer.setRemoteDescription(answer), answer);
  try {
    await local.waitForFunction(() => topologyPeer.connectionState === 'connected');
  } catch (error) {
    const describe = remoteSide => {
      const peer = remoteSide ? topologyRemote.peer : topologyPeer;
      return {connection:peer.connectionState,ice:peer.iceConnectionState,
        localCandidates:(peer.localDescription?.sdp.match(/^a=candidate:/gm)||[]).length,
        remoteCandidates:(peer.remoteDescription?.sdp.match(/^a=candidate:/gm)||[]).length,
        directions:peer.getTransceivers().map(t=>({direction:t.currentDirection,sender:!!t.sender.track,receiver:t.receiver.track.readyState}))};
    };
    console.error('Synthetic topology ICE diagnostics', JSON.stringify({
      local:await local.evaluate(describe, false),
      remote:await remote.evaluate(describe, true)
    }));
    throw error;
  }
}

export async function switchReceiveSlot(remote, slot, frequencies) {
  await remote.evaluate(async ({ slot, frequencies }) => {
    await topologyRemote.peer.getTransceivers()[slot].sender.replaceTrack(topologyRemote.tone(frequencies));
  }, { slot, frequencies });
}

export async function installZoomLikeAudio(page) {
  await page.evaluate(async () => {
    // No PeerConnection and no media-element srcObject; custom Web Audio renders the mix.
    const audio = new AudioContext(), oscillator = audio.createOscillator(), gain = audio.createGain();
    oscillator.frequency.value = 990; gain.gain.value = 0.1;
    oscillator.connect(gain).connect(audio.destination); oscillator.start(); await audio.resume();
    window.zoomLikeAudio = { audio, oscillator, gain };
  });
}
