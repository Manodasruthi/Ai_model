// AudioWorklet, not deprecated ScriptProcessorNode. No model runs in the browser.
class CapturePCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Float32Array(2048);
    this.used = 0;
    this.stopped = false;
    this.port.onmessage = ({data}) => {
      if (data === "stop") {
        this.flush();
        this.stopped = true;
        this.port.postMessage({type: "flushed"});
      }
    };
  }
  flush() {
    if (!this.used) return;
    const buffer = new ArrayBuffer(this.used * 2);
    const view = new DataView(buffer);
    let energy = 0;
    for (let i = 0; i < this.used; i++) {
      const sample = Math.max(-1, Math.min(1, this.samples[i]));
      view.setInt16(i * 2, sample < 0 ? sample * 32768 : sample * 32767, true);
      energy += sample * sample;
    }
    this.port.postMessage({type:"pcm", buffer, rms: Math.sqrt(energy / this.used)}, [buffer]);
    this.used = 0;
  }
  process(inputs) {
    if (this.stopped) return false;
    const channel = inputs[0]?.[0];
    if (channel) {
      for (const sample of channel) {
        this.samples[this.used++] = sample;
        if (this.used === this.samples.length) this.flush();
      }
    }
    return true; // Outputs stay zero: never play the microphone through speakers.
  }
}
registerProcessor("capture-pcm", CapturePCM);
