/**
 * Web 音频播放器：基于 HTMLAudioElement 封装 base64 音频的播放与停止。
 * 替代移动端的 expo-av Audio.Sound（见 Web 方案 §7.2）。
 */

export interface WebAudioPlayerOptions {
  onPlaybackEnded?: () => void;
}

/**
 * 将 base64（可能带 data URL 前缀）转换为可播放的 data URL / Blob URL。
 */
export function base64ToAudioUrl(base64: string, mimeType = 'audio/wav'): string {
  const raw = base64.includes(',') ? base64.slice(base64.indexOf(',') + 1) : base64;
  try {
    const binary = atob(raw);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    const blob = new Blob([bytes], { type: mimeType });
    return URL.createObjectURL(blob);
  } catch {
    // atob 失败时退回 data URL（由上层负责错误提示）
    return `data:${mimeType};base64,${raw}`;
  }
}

export class WebAudioPlayer {
  private audio: HTMLAudioElement | null = null;
  private readonly onPlaybackEnded?: () => void;
  private endedHandler: (() => void) | null = null;

  constructor(options: WebAudioPlayerOptions = {}) {
    this.onPlaybackEnded = options.onPlaybackEnded;
  }

  get isPlaying(): boolean {
    return this.audio !== null && !this.audio.paused && !this.audio.ended;
  }

  /**
   * 播放一段 base64 音频，播放完成（自然结束或手动停止）后 resolve。
   */
  playBase64(base64: string, mimeType = 'audio/wav'): Promise<void> {
    this.stop();
    const url = base64ToAudioUrl(base64, mimeType);
    const audio = new Audio(url);
    this.audio = audio;

    return new Promise<void>((resolve) => {
      const finish = () => {
        this.cleanupCurrent();
        this.onPlaybackEnded?.();
        resolve();
      };
      this.endedHandler = finish;
      audio.addEventListener('ended', finish);
      audio.addEventListener('error', finish);
      // 主动 stop 也会触发，统一走 cleanupCurrent + resolve
      void audio.play().catch(finish);
    });
  }

  /**
   * 停止当前播放并 resolve 等待中的 playBase64。
   */
  stop(): void {
    if (this.audio) {
      this.audio.pause();
      this.cleanupCurrent();
      this.audio = null;
    }
  }

  private cleanupCurrent(): void {
    if (this.audio && this.endedHandler) {
      this.audio.removeEventListener('ended', this.endedHandler);
      this.audio.removeEventListener('error', this.endedHandler);
      this.endedHandler = null;
    }
    if (this.audio) {
      try {
        const src = this.audio.src;
        this.audio.removeAttribute('src');
        if (src.startsWith('blob:')) {
          URL.revokeObjectURL(src);
        }
      } catch {
        // ignore cleanup errors
      }
      this.audio = null;
    }
  }
}
