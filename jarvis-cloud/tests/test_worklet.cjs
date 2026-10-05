// Test actual worklet encoding and partial-buffer flush without browser hardware.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
let Worklet;
class MockAudioWorkletProcessor {
  constructor() { this.port = {postMessage: data => this.messages.push(data)}; this.messages = []; }
}
vm.runInNewContext(fs.readFileSync("static/pcm-worklet.js", "utf8"), {
  AudioWorkletProcessor: MockAudioWorkletProcessor,
  registerProcessor: (_, klass) => { Worklet = klass; },
  Float32Array, ArrayBuffer, DataView, Math,
});
const capture = new Worklet();
capture.process([[new Float32Array([-1, 0, 1, 0.5])]]);
assert.equal(capture.messages.length, 0);
capture.port.onmessage({data:"stop"});
const frame = capture.messages[0];
assert.equal(frame.type, "pcm");
assert.equal(frame.buffer.byteLength, 8);
const pcm = new DataView(frame.buffer);
assert.deepEqual([0, 2, 4, 6].map(n => pcm.getInt16(n, true)), [-32768, 0, 32767, 16383]);
assert.equal(capture.messages[1].type, "flushed");
assert.equal(capture.process([]), false);
console.log("PCM worklet encoding and flush passed.");
