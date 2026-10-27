// Batch PCM on the audio render clock; UI/main-thread delays do not change timestamps.
class CassiniPCM extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.channels = options.processorOptions.channels;
    this.samples = new Int16Array(8192 * this.channels);
    this.used = 0;
    this.firstFrame = 0;
    this.stopped = false;
    this.port.onmessage = ({ data }) => {
      if (data === "stop") {
        this.stopped = true;
        this.flush();
        this.port.postMessage({ stopped: true });
      }
    };
  }
  flush() {
    if (!this.used) return;
    const pcm = this.samples.buffer.slice(0, this.used * 2);
    this.port.postMessage({ pcm, frame: this.firstFrame }, [pcm]);
    this.used = 0;
  }
  process(inputs) {
    if (this.stopped) return false;
    const input = inputs[0];
    const frames = input[0]?.length ?? 128;
    for (let frame = 0; frame < frames; frame++) {
      if (!this.used) this.firstFrame = currentFrame + frame;
      for (let channel = 0; channel < this.channels; channel++) {
        const value = input[channel]?.[frame] ?? 0;
        this.samples[this.used++] = Math.round(Math.max(-1, Math.min(1, Number.isFinite(value) ? value : 0)) * 32767);
      }
      if (this.used === this.samples.length) this.flush();
    }
    return true; // the output remains silent; remote audio is never played twice
  }
}
registerProcessor("cassini-pcm", CassiniPCM);
