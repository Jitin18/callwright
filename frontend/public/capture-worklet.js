// Collects microphone audio into ~100 ms blocks and posts them to the main thread.
// The AudioContext runs at 24 kHz, which is what Azure Voice Live expects (PCM16 mono).
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.block = Math.round(sampleRate * 0.1);
    this.buf = new Float32Array(this.block);
    this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      for (let i = 0; i < ch.length; i++) {
        this.buf[this.n++] = ch[i];
        if (this.n === this.block) {
          this.port.postMessage(this.buf.slice(0));
          this.n = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("capture-processor", CaptureProcessor);
