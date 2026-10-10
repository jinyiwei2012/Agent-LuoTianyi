import type { CapturedAudioFrame } from '../call_audio_contracts';

export const CAPTURE_BYTES_PER_SECOND = 16_000 * 2;
export const MAX_RECOVERY_BYTES = CAPTURE_BYTES_PER_SECOND * 3;

/** Keeps original capture sequence numbers for call.v1 reconnect retransmission. */
export class CaptureRecoveryBuffer {
  private readonly frames: CapturedAudioFrame[] = [];
  private byteLength = 0;

  append(frame: CapturedAudioFrame): void {
    if (frame.payload.byteLength > MAX_RECOVERY_BYTES) {
      throw new Error('capture_frame_too_large');
    }
    this.frames.push(frame);
    this.byteLength += frame.payload.byteLength;
    while (this.byteLength > MAX_RECOVERY_BYTES) {
      const discarded = this.frames.shift();
      if (discarded) this.byteLength -= discarded.payload.byteLength;
    }
  }

  acknowledge(sequence: number): void {
    while (this.frames[0] && this.frames[0].sequence <= sequence) {
      this.byteLength -= this.frames.shift()!.payload.byteLength;
    }
  }

  unacknowledgedAfter(sequence: number): readonly CapturedAudioFrame[] {
    return this.frames.filter((frame) => frame.sequence > sequence);
  }

  clear(): void {
    this.frames.length = 0;
    this.byteLength = 0;
  }

  get bufferedBytes(): number {
    return this.byteLength;
  }
}
