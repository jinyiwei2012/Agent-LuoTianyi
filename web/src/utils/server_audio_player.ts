/**
 * Web 服务器音频播放器：累积流式分块的 base64 音频，结束时合并为完整
 * wav 并播放。替代移动端 WebView 里 window.feedAudioChunk 的桥接
 * （见 Web 方案 §7.2）。
 */

export interface ServerAudioPlayerOptions {
  /** 播放结束时回调（用于通知 MessageProcessor 释放在线音频优先权） */
  onFinished?: () => void;
  /** 播放出错时回调 */
  onError?: (errorText: string) => void;
}

export class ServerAudioPlayer {
  private chunks: string[] = [];
  private audio: HTMLAudioElement | null = null;
  private playing = false;
  private readonly onFinished?: () => void;
  private readonly onError?: (errorText: string) => void;
  private endedHandler: (() => void) | null = null;

  constructor(options: ServerAudioPlayerOptions = {}) {
    this.onFinished = options.onFinished;
    this.onError = options.onError;
  }

  get isPlaying(): boolean {
    return this.playing;
  }

  /** 累积一个音频分块；isFinal=true 表示本句结束，合并并播放。 */
  feedChunk(base64Audio: string, isFinal: boolean): void {
    if (base64Audio) {
      this.chunks.push(base64Audio);
    }
    if (!isFinal) {
      return;
    }
    this.stopCurrent();
    if (this.chunks.length === 0) {
      this.finish();
      return;
    }
    this.playMerged();
  }

  stop(): void {
    this.chunks = [];
    this.stopCurrent();
  }

  private playMerged(): void {
    // 合并所有分块为一段完整 base64（用 ; 分隔再统一解码为字节）
    let binary = '';
    for (const chunk of this.chunks) {
      const raw = chunk.includes(',') ? chunk.slice(chunk.indexOf(',') + 1) : chunk;
      try {
        binary += atob(raw.replace(/\s+/g, '').replace(/-/g, '+').replace(/_/g, '/'));
      } catch {
        // 单个分块解码失败时跳过，尽量继续
      }
    }
    this.chunks = [];
    if (!binary) {
      this.finish();
      return;
    }

    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    const blob = new Blob([bytes], { type: 'audio/wav' });
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    this.audio = audio;
    this.playing = true;

    const finish = () => {
      this.playing = false;
      this.cleanupCurrent(url);
      this.finish();
    };
    this.endedHandler = finish;
    audio.addEventListener('ended', finish);
    audio.addEventListener('error', finish);
    void audio.play().catch(() => {
      this.playing = false;
      this.cleanupCurrent(url);
      this.onError?.('服务器音频播放失败');
      this.finish();
    });
  }

  private stopCurrent(): void {
    if (this.audio) {
      this.audio.pause();
      const src = this.audio.src;
      if (this.endedHandler) {
        this.audio.removeEventListener('ended', this.endedHandler);
        this.audio.removeEventListener('error', this.endedHandler);
        this.endedHandler = null;
      }
      this.audio = null;
      if (src.startsWith('blob:')) {
        URL.revokeObjectURL(src);
      }
    }
    this.playing = false;
  }

  private cleanupCurrent(url: string): void {
    if (this.audio) {
      if (this.endedHandler) {
        this.audio.removeEventListener('ended', this.endedHandler);
        this.audio.removeEventListener('error', this.endedHandler);
        this.endedHandler = null;
      }
      this.audio = null;
    }
    if (url.startsWith('blob:')) {
      URL.revokeObjectURL(url);
    }
  }

  private finish(): void {
    this.playing = false;
    this.onFinished?.();
  }
}
