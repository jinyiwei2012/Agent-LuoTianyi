import type { CallAudioNativeModule } from '../modules/call-audio/src/CallAudioModule';
import type {
  NativeCapability,
  NativeCapturedFrame,
  NativePlaybackCompleted,
  NativePlaybackStopped,
} from '../modules/call-audio/src/CallAudio.types';
import { AndroidCallAudio, CAPTURE_FORMAT, PLAYBACK_FORMAT } from '../utils/call_audio/native_call_audio';

jest.mock('expo-modules-core', () => ({
  requireOptionalNativeModule: jest.fn(() => null),
}));
jest.mock('react-native', () => ({
  Platform: { OS: 'android' },
}));

type Listener = (event: any) => void;

class FakeNativeCallAudio implements CallAudioNativeModule {
  private readonly listeners = new Map<string, Set<Listener>>();
  generation = 0;
  capability: NativeCapability = {
    available: true,
    captureFormat: CAPTURE_FORMAT as NativeCapability['captureFormat'],
    playbackFormat: PLAYBACK_FORMAT as NativeCapability['playbackFormat'],
    aec: { available: true, enabled: true },
  };

  async getCapability() { return this.capability; }
  async startSession() { return this.capability; }
  async stopSession() {}
  async startCapture() { return this.capability; }
  async stopCapture() {}
  async enqueuePlayback() { return ++this.generation; }
  async stopPlayback() {}
  async tombstone() {}
  addListener(eventName: string, listener: Listener) {
    const listeners = this.listeners.get(eventName) ?? new Set();
    listeners.add(listener);
    this.listeners.set(eventName, listeners);
    return { remove: () => listeners.delete(listener) };
  }
  emit(eventName: string, event: unknown) {
    this.listeners.get(eventName)?.forEach((listener) => listener(event));
  }
}

describe('AndroidCallAudio adapter', () => {
  it('stays unavailable until native initialization reports exact formats', async () => {
    const native = new FakeNativeCallAudio();
    native.capability = {
      ...native.capability,
      playbackFormat: { encoding: 'pcm_s16le', sampleRateHz: 16_000, channels: 1 },
    };
    const audio = new AndroidCallAudio({}, native, 'android');

    await expect(audio.initialize()).resolves.toBe('unavailable');
  });

  it('emits completion only for actual current-generation native consumption', async () => {
    const native = new FakeNativeCallAudio();
    const audio = new AndroidCallAudio({}, native, 'android');
    const completed = jest.fn();
    const stream = { callId: 'call', responseId: 'response', streamId: 1 };
    audio.setConsumptionListener(completed);
    await audio.initialize();
    await audio.enqueue({ stream, format: PLAYBACK_FORMAT, payload: new Uint8Array([0, 0]), isFinal: true });

    native.emit('onPlaybackCompleted', {
      responseId: 'response', streamId: 1, generation: 0,
    } satisfies NativePlaybackCompleted);
    expect(completed).not.toHaveBeenCalled();

    native.emit('onPlaybackCompleted', {
      responseId: 'response', streamId: 1, generation: 1,
    } satisfies NativePlaybackCompleted);
    native.emit('onPlaybackCompleted', {
      responseId: 'response', streamId: 1, generation: 1,
    } satisfies NativePlaybackCompleted);
    expect(completed).toHaveBeenCalledTimes(1);
  });

  it('rejects a late completion after the native stopped receipt tombstones it', async () => {
    const native = new FakeNativeCallAudio();
    const audio = new AndroidCallAudio({}, native, 'android');
    const completed = jest.fn();
    const stream = { callId: 'call', responseId: 'response', streamId: 1 };
    audio.setConsumptionListener(completed);
    await audio.initialize();
    await audio.enqueue({ stream, format: PLAYBACK_FORMAT, payload: new Uint8Array([0, 0]), isFinal: true });

    native.emit('onPlaybackStopped', {
      responseId: 'response', streamId: 1, generation: 2,
    } satisfies NativePlaybackStopped);
    native.emit('onPlaybackCompleted', {
      responseId: 'response', streamId: 1, generation: 1,
    } satisfies NativePlaybackCompleted);

    expect(completed).not.toHaveBeenCalled();
  });

  it('tombstones synchronously before awaiting native stop', async () => {
    const native = new FakeNativeCallAudio();
    let releaseStop!: () => void;
    native.stopPlayback = async () => new Promise<void>((resolve) => { releaseStop = resolve; });
    const audio = new AndroidCallAudio({}, native, 'android');
    const completed = jest.fn();
    const stream = { callId: 'call', responseId: 'response', streamId: 1 };
    audio.setConsumptionListener(completed);
    await audio.initialize();
    await audio.enqueue({ stream, format: PLAYBACK_FORMAT, payload: new Uint8Array([0, 0]), isFinal: true });

    const stopping = audio.stop(stream);
    native.emit('onPlaybackCompleted', {
      responseId: 'response', streamId: 1, generation: 1,
    } satisfies NativePlaybackCompleted);
    expect(completed).not.toHaveBeenCalled();
    releaseStop();
    await stopping;
  });

  it('decodes capture bridge Base64 without changing original sequence', async () => {
    const native = new FakeNativeCallAudio();
    const audio = new AndroidCallAudio({}, native, 'android');
    const frames: NativeCapturedFrame[] = [];
    await audio.initialize();
    await audio.startCapture(CAPTURE_FORMAT, (frame) => frames.push({
      sequence: frame.sequence,
      payloadBase64: Buffer.from(frame.payload).toString('base64'),
      format: frame.format as NativeCapturedFrame['format'],
    }));

    native.emit('onCapturedAudio', {
      sequence: 42,
      payloadBase64: 'AQI=',
      format: CAPTURE_FORMAT as NativeCapturedFrame['format'],
    } satisfies NativeCapturedFrame);

    expect(frames).toEqual([{ sequence: 42, payloadBase64: 'AQI=', format: CAPTURE_FORMAT }]);
  });
});
