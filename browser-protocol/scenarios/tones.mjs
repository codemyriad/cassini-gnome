// Portable Playwright scenario primitives. No host paths, platform hooks, or dependencies.
export async function answerWithTone(page, offer, frequency = 880) {
  return page.evaluate(async ({ offer, frequency }) => {
    const context = new AudioContext();
    const oscillator = context.createOscillator(); oscillator.frequency.value = frequency;
    const output = context.createMediaStreamDestination();
    oscillator.connect(output); oscillator.start(); await context.resume();
    const peer = new RTCPeerConnection(); peer.addTrack(output.stream.getAudioTracks()[0]);
    await peer.setRemoteDescription(offer); await peer.setLocalDescription(await peer.createAnswer());
    if (peer.iceGatheringState !== 'complete') await new Promise(resolve => peer.addEventListener('icegatheringstatechange', () => {
      if (peer.iceGatheringState === 'complete') resolve();
    }));
    window.cassiniTonePeer = { peer, context, oscillator, output };
    return peer.localDescription.toJSON();
  }, { offer, frequency });
}

export function tonePower(wav, sampleRate, frequency, first = 4800, count = 12000, channels = 1, channel = 0) {
  let real = 0, imaginary = 0;
  for (let i = 0; i < count; i++) {
    const sample = wav.readInt16LE(44 + ((first + i) * channels + channel) * 2) / 32768;
    real += sample * Math.cos(2 * Math.PI * frequency * i / sampleRate);
    imaginary += sample * Math.sin(2 * Math.PI * frequency * i / sampleRate);
  }
  return (real ** 2 + imaginary ** 2) / count ** 2;
}
