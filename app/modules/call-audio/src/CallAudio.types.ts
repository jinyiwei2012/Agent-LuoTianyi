export interface NativeAudioFormat {
  encoding: 'pcm_s16le';
  sampleRateHz: number;
  channels: 1;
}

export interface NativeCapability {
  available: boolean;
  reason?: string;
  captureFormat?: NativeAudioFormat;
  playbackFormat?: NativeAudioFormat;
  aec: {
    available: boolean;
    enabled: boolean;
    error?: string;
  };
}

export interface NativeCapturedFrame {
  sequence: number;
  payloadBase64: string;
  format: NativeAudioFormat;
}

export interface NativePlaybackIdentity {
  responseId: string;
  streamId: number;
  generation: number;
}

export type NativePlaybackCompleted = NativePlaybackIdentity;

export type NativePlaybackStopped = NativePlaybackIdentity;

export interface NativeAudioFailure {
  code: string;
  operation: 'capture' | 'playback' | 'session';
}

export interface NativeLifecycleEvent {
  state: 'background';
}

export interface NativeRouteEvent {
  outputs: string[];
}
