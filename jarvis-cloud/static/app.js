"use strict";
const $ = id => document.getElementById(id);
let ws, audio, workletLoaded = false, mic, source, capture;
let connected = false, busy = false, recording = false, flushing = false;
let setupVersion = 0, recordStart = 0, lastVoice = 0, voicedMs = 0, heardVoice = false;
let assistantNode, nextPlay = 0, carry = new Uint8Array(0), audioRate = 24000;
let sources = new Set(), serverDone = false, doneTimer, turnTimer;
let cancelling = false, discardAudio = false;

function status(message, state = "idle") {
  $("status").textContent = message;
  $("orb").dataset.state = state;
}
function controls() {
  $("talk").disabled = !connected || (busy && !recording);
  $("talk").textContent = recording ? "Finish speaking" : "Start speaking";
  $("send").disabled = $("text").disabled = !connected || busy;
  $("cancel").disabled = !connected || !busy || cancelling;
  $("disconnect").disabled = !connected;
  $("connect").disabled = connected || (ws && ws.readyState === WebSocket.CONNECTING);
  $("language").disabled = $("reply").disabled = busy;
}
async function initAudio() {
  if (!audio) audio = new AudioContext({latencyHint:"interactive"});
  await audio.resume(); // Called from a user gesture to unlock mobile playback.
  if (!workletLoaded) {
    await audio.audioWorklet.addModule("/static/pcm-worklet.js");
    workletLoaded = true;
  }
}
function addMessage(label, text, cls = "") {
  const article = document.createElement("article");
  article.className = cls;
  const small = document.createElement("small");
  small.textContent = label;
  const content = document.createElement("span");
  content.textContent = text;
  article.append(small, content);
  $("chat").append(article);
  while ($("chat").children.length > 24) $("chat").firstChild.remove();
  return content;
}
function send(value) {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(value));
}
function options() {
  return {language: $("language").value, reply_mode: $("reply").value};
}
function stopMic() {
  clearTimeout(turnTimer);
  if (capture) { capture.port.onmessage = null; capture.disconnect(); }
  source?.disconnect();
  mic?.getTracks().forEach(track => track.stop());
  mic = source = capture = null;
  recording = flushing = false;
  $("orb").style.setProperty("--level", 0);
}
function stopPlayback() {
  clearTimeout(doneTimer);
  for (const node of sources) { try { node.stop(); } catch (_) {} }
  sources.clear();
  nextPlay = audio?.currentTime || 0;
  carry = new Uint8Array(0);
}
function finishWhenPlayed() {
  if (!serverDone || !audio) return;
  clearTimeout(doneTimer);
  doneTimer = setTimeout(() => {
    busy = false;
    status("Ready — tap to speak");
    controls();
  }, Math.max(0, nextPlay - audio.currentTime) * 1000 + 80);
}
function playPCM(buffer) {
  if (discardAudio || !audio) return;
  const incoming = new Uint8Array(buffer);
  const data = new Uint8Array(carry.length + incoming.length);
  data.set(carry); data.set(incoming, carry.length);
  const usable = data.length - data.length % 2;
  carry = data.slice(usable);
  if (!usable) return;
  const pcm = new DataView(data.buffer, 0, usable);
  const output = audio.createBuffer(1, usable / 2, audioRate);
  const floats = output.getChannelData(0);
  for (let i = 0; i < floats.length; i++) floats[i] = pcm.getInt16(i * 2, true) / 32768;
  const node = audio.createBufferSource();
  node.buffer = output; node.connect(audio.destination);
  nextPlay = Math.max(nextPlay, audio.currentTime + 0.12);
  if (nextPlay - audio.currentTime > 90) { cancelTurn(); return; }
  node.start(nextPlay); nextPlay += output.duration;
  sources.add(node);
  node.onended = () => { sources.delete(node); node.disconnect(); };
  status("Speaking…", "speaking");
}
function beginTurn() {
  busy = true; cancelling = false; discardAudio = false;
  serverDone = false; assistantNode = null;
  stopPlayback(); controls();
}
function fail(message) {
  setupVersion++;
  stopMic(); stopPlayback();
  busy = false; serverDone = false; discardAudio = true;
  status(message, "idle"); controls();
}
function cancelTurn() {
  if (!connected) return;
  setupVersion++;
  stopMic(); stopPlayback();
  cancelling = true; discardAudio = true; serverDone = false;
  send({type:"cancel"});
  status("Cancelling…"); controls();
}
$("login").addEventListener("submit", async event => {
  event.preventDefault();
  if (connected || ws?.readyState === WebSocket.CONNECTING) return;
  let key = $("key").value;
  try { await initAudio(); } catch (_) { status("This browser could not start audio."); return; }
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  controls(); status("Connecting…");
  ws.onopen = () => {
    send({type:"auth", key}); key = ""; $("key").value = "";
  };
  ws.onmessage = event => {
    if (event.data instanceof ArrayBuffer) { playPCM(event.data); return; }
    const message = JSON.parse(event.data);
    if (cancelling && message.type !== "cancelled") return;
    switch (message.type) {
      case "ready": connected = true; status("Ready — tap to speak"); break;
      case "recording":
        recording = true; recordStart = lastVoice = performance.now();
        voicedMs = 0; heardVoice = false;
        turnTimer = setTimeout(finishRecording, 19000);
        status("Listening…", "listening"); break;
      case "thinking": status("Thinking…", "thinking"); break;
      case "transcript":
        addMessage("YOU", message.text, "user");
        assistantNode = addMessage("JARVIS", ""); break;
      case "token":
        if (!assistantNode) assistantNode = addMessage("JARVIS", "");
        assistantNode.textContent += message.text; break;
      case "audio_format": audioRate = message.sample_rate; break;
      case "speaking": status("Preparing speech…", "thinking"); break;
      case "done": serverDone = true; finishWhenPlayed(); break;
      case "cancelled":
        cancelling = false; busy = false; status("Cancelled. Ready to speak."); break;
      case "error": fail(message.message); break;
    }
    controls();
  };
  ws.onclose = event => {
    key = ""; connected = false; setupVersion++;
    stopMic(); stopPlayback(); busy = cancelling = false; discardAudio = true;
    const reason = event.code === 1008 ? "Access denied, origin rejected, or session expired."
      : event.code === 1013 ? "Another session is already connected."
      : "Disconnected. Re-enter your key to reconnect.";
    status(reason, "offline"); controls();
  };
  ws.onerror = () => status("Connection failed. Check the deployment and HTTPS address.", "offline");
});

async function startRecording() {
  if (!connected || busy) return;
  beginTurn();
  const version = ++setupVersion;
  status("Opening microphone…");
  try {
    await initAudio();
    const stream = await navigator.mediaDevices.getUserMedia({audio:{
      channelCount:1, echoCancellation:true, noiseSuppression:true, autoGainControl:true,
    }});
    if (version !== setupVersion || !connected) { stream.getTracks().forEach(t => t.stop()); return; }
    mic = stream;
    source = audio.createMediaStreamSource(mic);
    capture = new AudioWorkletNode(audio, "capture-pcm", {channelCount:1});
    capture.port.onmessage = ({data}) => {
      if (version !== setupVersion) return;
      if (data.type === "flushed") {
        stopMic(); send({type:"stop"}); status("Thinking…", "thinking"); controls(); return;
      }
      if (data.type !== "pcm" || !recording) return;
      if (ws.bufferedAmount > 256000) {
        cancelTurn(); status("Network too slow. Recording cancelled."); return;
      }
      ws.send(data.buffer);
      $("orb").style.setProperty("--level", Math.min(1, data.rms * 14));
      if (flushing) return;
      const now = performance.now();
      if (data.rms > 0.018) {
        voicedMs += data.buffer.byteLength / 2 / audio.sampleRate * 1000;
        lastVoice = now;
        if (voicedMs >= 180) heardVoice = true;
      }
      if ((heardVoice && now - lastVoice > 850) || now - recordStart > 19000) finishRecording();
      else if (!heardVoice && now - recordStart > 5000) cancelTurn();
    };
    source.connect(capture); capture.connect(audio.destination);
    send({type:"start", sample_rate:audio.sampleRate, ...options()});
  } catch (_) {
    if (version === setupVersion) fail("Microphone unavailable. Allow microphone access and use HTTPS.");
  }
}
function finishRecording() {
  if (!recording || flushing) return;
  flushing = true; clearTimeout(turnTimer);
  capture.port.postMessage("stop");
  $("talk").disabled = true;
}
$("talk").onclick = () => recording ? finishRecording() : startRecording();
$("cancel").onclick = cancelTurn;
$("disconnect").onclick = () => ws?.close(1000, "User disconnected");
$("textForm").addEventListener("submit", async event => {
  event.preventDefault();
  const text = $("text").value.trim();
  if (!text || !connected || busy) return;
  beginTurn();
  const version = ++setupVersion;
  try {
    await initAudio();
    if (version !== setupVersion || !connected) return;
    $("text").value = "";
    send({type:"text", text, ...options()}); status("Thinking…", "thinking");
  } catch (_) { if (version === setupVersion) fail("Audio playback could not start."); }
});
window.addEventListener("pagehide", () => { stopMic(); stopPlayback(); ws?.close(); });
