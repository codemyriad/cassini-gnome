// Spectral magnitude averaged without assuming phase coherence across browser
// graph clock corrections. Measurement bounds and amplitude units are unchanged.
export function energy(audio, start, end, hz) {
  const first = Math.max(0, Math.round(start * audio.rate));
  const last = Math.min(audio.bytes.length / (2 * audio.channels), Math.round(end * audio.rate));
  const windowFrames = Math.max(1, Math.round(audio.rate * 0.1));
  let power = 0, magnitude = 0;
  for (let block = first; block < last; block += windowFrames) {
    let re = 0, im = 0;
    for (let n = block; n < Math.min(last, block + windowFrames); n++) {
      const value = audio.bytes.readInt16LE(n * 2 * audio.channels) / 32768;
      power += value * value;
      re += value * Math.cos(2 * Math.PI * hz * n / audio.rate);
      im += value * Math.sin(2 * Math.PI * hz * n / audio.rate);
    }
    magnitude += Math.hypot(re, im);
  }
  const count = Math.max(1, last - first);
  return {rms: Math.sqrt(power / count), tone: 2 * magnitude / count};
}
