import { webview } from '@kit.ArkWeb';
import { util } from '@kit.ArkTS';
import { hilog } from '@kit.PerformanceAnalysisKit';

const TAG = 'Live2DFacade';

/** JS -> Native 桥对象。registerJavaScriptProxy 暴露为 web 侧 window.ReactNativeWebView，
 *  与 live2d.html 的 ReactNativeWebView.postMessage(...) 协议完全对齐。 */
class JsBridge {
  private onMessage: (msg: string) => void = () => {
  };

  setOnMessage(cb: (msg: string) => void): void {
    this.onMessage = cb;
  }

  postMessage(message: string): void {
    hilog.info(0x0000, TAG, 'JS->Native: %{public}s', message);
    this.onMessage(message);
  }
}

/** Live2D 事件回调 */
export interface Live2DEvents {
  onModelLoaded?: (ok: boolean, error?: string) => void;
  onTouch?: (touchArea: string, raw: string) => void;
  onAudioStopped?: () => void;
  onAudioFinished?: () => void;
}

/**
 * Live2D WebView 门面：加载 rawfile 中的 live2d.html，封装到 JS 的双向调用。
 * 对齐 app/utils/live2d_helper.ts 的 bridge 协议：
 *   JS->Native: modelLoaded/touch/audio_stopped/audio_finished
 *   Native->JS: setExpression/setMouthOpenY/feedAudioChunk/stopServerAudio
 */
export class Live2DFacade {
  readonly controller: webview.WebviewController = new webview.WebviewController();
  private bridge: JsBridge = new JsBridge();
  private events: Live2DEvents;
  private bound = false;

  constructor(events: Live2DEvents) {
    this.events = events;
    this.bridge.setOnMessage((msg: string) => {
      this.dispatchJsEvent(msg);
    });
  }

  private dispatchJsEvent(msg: string): void {
    try {
      const obj: Record<string, Object> = JSON.parse(msg) as Record<string, Object>;
      const type = obj['type'] as string;
      if (type === 'modelLoaded') {
        const ok = obj['success'] as boolean;
        if (this.events.onModelLoaded !== undefined) {
          this.events.onModelLoaded(ok, obj['error'] !== undefined ? String(obj['error']) : undefined);
        }
      } else if (type === 'touch') {
        if (this.events.onTouch !== undefined) {
          this.events.onTouch(String(obj['touchArea'] ?? ''), msg);
        }
      } else if (type === 'audio_stopped') {
        if (this.events.onAudioStopped !== undefined) {
          this.events.onAudioStopped();
        }
      } else if (type === 'audio_finished') {
        if (this.events.onAudioFinished !== undefined) {
          this.events.onAudioFinished();
        }
      }
    } catch (e) {
      hilog.error(0x0000, TAG, 'js event parse failed: %{public}s', (e as Error).message);
    }
  }

  /** 在 Web 组件 onPageEnd 后调用：注册桥（对齐 window.ReactNativeWebView） */
  bindBridge(): void {
    if (this.bound) {
      return;
    }
    this.controller.registerJavaScriptProxy(this.bridge, 'ReactNativeWebView', ['postMessage']);
    this.bound = true;
    hilog.info(0x0000, TAG, 'bridge registered');
  }

  /**
   * 注册桥 + 首次重载一次：JS init 发生在桥注册前（modelLoaded 事件被跳过），
   * 重载后 bridge 已存在，JS 能发出 modelLoaded/touch 事件。
   * 守卫避免 onPageEnd 循环刷新。
   */
  bindBridgeAndReload(): void {
    if (this.bound) {
      return;
    }
    this.bindBridge();
    setTimeout(() => {
      this.controller.refresh();
    }, 300);
  }

  /** 页面重载后重新绑定 */
  resetBridge(): void {
    this.bound = false;
  }

  private callJs(script: string): void {
    this.controller.runJavaScript(script).catch((e: Error) => {
      hilog.warn(0x0000, TAG, 'runJavaScript failed: %{public}s', e.message);
    });
  }

  /** 切换表情（对齐 window.setExpression） */
  setExpression(expressionId: string): void {
    this.callJs(`window.setExpression && window.setExpression(${JSON.stringify(expressionId)})`);
  }

  /** 口型同步（对齐 window.setMouthOpenY） */
  setMouthOpenY(value: number, weight: number = 1): void {
    this.callJs(`window.setMouthOpenY && window.setMouthOpenY(${value}, ${weight})`);
  }

  /** 音频分块投喂（对齐 window.feedAudioChunk） */
  feedAudioChunk(base64Audio: string, isFinal: boolean): void {
    this.callJs(`window.feedAudioChunk && window.feedAudioChunk(${JSON.stringify(base64Audio)}, ${isFinal ? 'true' : 'false'})`);
  }

  /** 停止服务端音频（对齐 window.stopServerAudio） */
  stopServerAudio(): void {
    this.callJs('window.stopServerAudio && window.stopServerAudio()');
  }

  /**
   * 发送 base64（确保正确传给 JS；JSON.stringify 保证转义安全）
   */
  debugLog(tag: string): void {
    hilog.info(0x0000, TAG, 'debug: %{public}s', tag);
  }
}
