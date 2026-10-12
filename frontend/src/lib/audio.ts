/**
 * Browser audio for Azure Voice Live.
 * - MicStreamer: streams the microphone continuously as PCM16 mono at 24 kHz. Voice Live does semantic turn
 *   detection, noise suppression and echo cancellation on the server. While the agent is speaking, a barge-in
 *   gate only lets through speech clearly louder than the agent's own echo (silence is sent otherwise), so the
 *   agent can't hear itself and interrupt its own sentence.
 * - PcmPlayer: plays the PCM16 24 kHz chunks the model streams back, gaplessly, and can stop instantly when the
 *   customer interrupts (barge-in). Playback goes through a local WebRTC loopback: Chrome's echo canceller only
 *   removes audio played through WebRTC, not plain Web Audio output.
 *
 * URL switches for troubleshooting: ?gate=0 (no barge-in gate), ?loopback=0 (plain Web Audio playback).
 */
const RATE = 24000;
const params = new URLSearchParams(location.search);

// Barge-in gate (frame RMS, 0..1). Speech must beat both the floor and a multiple of the measured echo level.
const GATE_ON = params.get("gate") !== "0";
const GATE_MIN = 0.035;
const GATE_ECHO_RATIO = 2.5;
const GATE_ONSET_FRAMES = 2;        // consecutive loud frames (100 ms each) before the gate opens
const GATE_HOLD_MS = 1000;          // stays open this long after the last loud frame
const ECHO_TAIL_MS = 500;           // room echo + loopback buffer after playback stops

export class MicStreamer {
  private ctx?: AudioContext;
  private stream?: MediaStream;
  private node?: AudioWorkletNode;
  private echoFloor = 0.01;
  private loud = 0;
  private openUntil = 0;
  private held: ArrayBuffer[] = [];

  /** agentAudible: true while the agent's voice may reach the mic (playing or just finished). */
  constructor(private onChunk: (pcm16: ArrayBuffer) => void, private onLevel: (rms: number) => void,
              private agentAudible: () => boolean = () => false) {}

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    this.ctx = new AudioContext({ sampleRate: RATE });
    await this.ctx.audioWorklet.addModule("/capture-worklet.js");
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "capture-processor");
    this.node.port.onmessage = (e) => {
      const f32 = e.data as Float32Array;
      const i16 = new Int16Array(f32.length);
      let sum = 0;
      for (let i = 0; i < f32.length; i++) {
        const v = Math.max(-1, Math.min(1, f32[i]));
        i16[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / f32.length);
      this.onLevel(rms);
      this.gate(i16.buffer, rms);
    };
    src.connect(this.node);
  }

  /** Send the frame, or silence of the same length while the agent is speaking and this is only its echo. */
  private gate(frame: ArrayBuffer, rms: number) {
    const now = performance.now();
    if (!GATE_ON || !this.agentAudible()) {
      this.loud = 0; this.held = []; this.openUntil = 0;
      this.onChunk(frame);
      return;
    }
    const threshold = Math.max(GATE_MIN, this.echoFloor * GATE_ECHO_RATIO);
    if (rms > threshold) {
      this.loud += 1;
      this.held.push(frame);
      if (this.loud >= GATE_ONSET_FRAMES || now < this.openUntil) {
        this.openUntil = now + GATE_HOLD_MS;
        this.held.forEach((f) => this.onChunk(f));   // include the onset, so the first syllable isn't lost
        this.held = [];
      }
      return;
    }
    // quiet frame: it measures the echo level
    this.echoFloor = 0.85 * this.echoFloor + 0.15 * rms;
    this.loud = 0;
    if (now < this.openUntil) { this.held.forEach((f) => this.onChunk(f)); this.held = []; this.onChunk(frame); return; }
    this.held.forEach((f) => this.onChunk(new ArrayBuffer(f.byteLength)));
    this.held = [];
    this.onChunk(new ArrayBuffer(frame.byteLength));
  }

  stop() {
    this.node?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    this.ctx?.close();
    this.ctx = undefined;
  }

  get active() { return !!this.ctx; }
}

/** Play a Web Audio stream through a local peer connection, so the browser's echo canceller can see it. */
async function webrtcLoopback(stream: MediaStream): Promise<{ out: MediaStream; close: () => void }> {
  const a = new RTCPeerConnection(), b = new RTCPeerConnection();
  a.onicecandidate = (e) => { if (e.candidate) b.addIceCandidate(e.candidate).catch(() => {}); };
  b.onicecandidate = (e) => { if (e.candidate) a.addIceCandidate(e.candidate).catch(() => {}); };
  const got = new Promise<MediaStream>((resolve) => {
    b.ontrack = (e) => resolve(e.streams[0] ?? new MediaStream([e.track]));
  });
  stream.getTracks().forEach((t) => a.addTrack(t, stream));
  const offer = await a.createOffer();
  await a.setLocalDescription(offer);
  await b.setRemoteDescription(offer);
  const answer = await b.createAnswer();
  // voice quality: a higher Opus bitrate than the default
  answer.sdp = answer.sdp?.replace(/useinbandfec=1/g, "useinbandfec=1;maxaveragebitrate=96000");
  await b.setLocalDescription(answer);
  await a.setRemoteDescription(answer);
  const out = await Promise.race([got, new Promise<never>((_, rej) => setTimeout(() => rej(new Error("loopback timeout")), 3000))]);
  return { out, close: () => { a.close(); b.close(); } };
}

export class PcmPlayer {
  private ctx = new AudioContext({ sampleRate: RATE });
  private analyser = this.ctx.createAnalyser();
  private nextTime = 0;
  private sources = new Set<AudioBufferSourceNode>();
  private data = new Uint8Array(512);
  private routed = false;
  private el?: HTMLAudioElement;
  private closeLoopback?: () => void;
  private lastEnd = 0;

  constructor(private onPlaying: (playing: boolean) => void) {
    this.analyser.fftSize = 512;
  }

  /** Connect the output once: WebRTC loopback (echo-cancellable) if possible, else straight to the speakers. */
  async route() {
    if (this.routed) return;
    this.routed = true;
    if (params.get("loopback") !== "0" && typeof RTCPeerConnection !== "undefined") {
      try {
        const dest = this.ctx.createMediaStreamDestination();
        this.analyser.connect(dest);
        const { out, close } = await webrtcLoopback(dest.stream);
        this.el = new Audio();
        this.el.autoplay = true;
        this.el.srcObject = out;
        await this.el.play();
        this.closeLoopback = close;
        return;
      } catch (err) {
        console.warn("WebRTC loopback unavailable, playing directly", err);
        this.analyser.disconnect();
        this.closeLoopback?.();
      }
    }
    this.analyser.connect(this.ctx.destination);
  }

  play(b64: string) {
    if (!b64) return;
    if (!this.routed) this.route();
    if (this.ctx.state === "suspended") this.ctx.resume();
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const i16 = new Int16Array(bytes.buffer, 0, Math.floor(bytes.length / 2));
    const buf = this.ctx.createBuffer(1, i16.length, RATE);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < i16.length; i++) ch[i] = i16[i] / 0x8000;
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.analyser);
    this.nextTime = Math.max(this.nextTime, this.ctx.currentTime + 0.02);
    src.start(this.nextTime);
    this.nextTime += buf.duration;
    this.sources.add(src);
    this.onPlaying(true);
    src.onended = () => {
      this.sources.delete(src);
      if (this.sources.size === 0) { this.lastEnd = performance.now(); this.onPlaying(false); }
    };
  }

  /** Barge-in: the customer started talking, so stop everything that's queued. */
  clear() {
    this.sources.forEach((s) => { try { s.stop(); } catch { /* already stopped */ } });
    this.sources.clear();
    this.nextTime = 0;
    this.lastEnd = performance.now();
    this.onPlaying(false);
  }

  /** The agent's voice may still be reaching the microphone. */
  audible(): boolean {
    return this.sources.size > 0 || performance.now() - this.lastEnd < ECHO_TAIL_MS;
  }

  level(): number {
    if (this.sources.size === 0) return 0;
    this.analyser.getByteTimeDomainData(this.data);
    let sum = 0;
    for (const v of this.data) { const d = (v - 128) / 128; sum += d * d; }
    return Math.sqrt(sum / this.data.length);
  }

  unlock() {
    if (this.ctx.state === "suspended") this.ctx.resume();
    this.el?.play().catch(() => {});
  }
}
