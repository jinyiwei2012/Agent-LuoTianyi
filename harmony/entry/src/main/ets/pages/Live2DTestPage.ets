import { webview } from '@kit.ArkWeb';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { RawfileScheme } from '../service/RawfileScheme';

const TAG = 'Live2DTest';

/**
 * JS -> Native 桥对象。
 * 鸿蒙的 registerJavaScriptProxy 会把该对象暴露为 web 侧的
 * window.ReactNativeWebView，与现有 live2d.html 里的
 * ReactNativeWebView.postMessage(...) 调用协议完全对齐。
 */
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

@Entry
@Component
struct Live2DTestPage {
  private controller: webview.WebviewController = new webview.WebviewController();
  private bridge: JsBridge = new JsBridge();
  private reloadedOnce: boolean = false;
  @State lastEvent: string = '等待 JS 事件...';
  @State modelLoaded: string = '未加载';

  aboutToAppear(): void {
    RawfileScheme.registerScheme();
    this.bridge.setOnMessage((msg: string) => {
      this.handleJsEvent(msg);
    });
  }

  private handleJsEvent(msg: string): void {
    try {
      const obj: Record<string, Object> = JSON.parse(msg) as Record<string, Object>;
      const type = obj['type'] as string;
      if (type === 'modelLoaded') {
        const ok = obj['success'] as boolean;
        this.modelLoaded = ok ? '模型加载成功' : `模型加载失败: ${obj['error'] ?? ''}`;
      } else if (type === 'touch') {
        this.lastEvent = `触摸区域: ${JSON.stringify(obj['touchArea'] ?? [])}`;
      } else {
        this.lastEvent = `事件: ${type}`;
      }
    } catch (e) {
      this.lastEvent = `解析失败: ${msg}`;
    }
  }

  private callJs(script: string): void {
    this.controller.runJavaScript(script)
      .then((r: string) => {
        hilog.info(0x0000, TAG, 'JS返回: %{public}s', r);
      })
      .catch((e: Error) => {
        hilog.error(0x0000, TAG, 'runJavaScript失败: %{public}s', e.message);
      });
  }

  build() {
    Column({ space: 8 }) {
      // 顶部状态栏
      Row() {
        Text('Live2D WebView 验证')
          .fontSize(18)
          .fontWeight(FontWeight.Bold)
      }.width('100%').padding(12)

      // WebView 区域（透明背景，模型展示；自定义 scheme 拦截 rawfile 资源，
      // 绕开 resource:// 不支持 fetch/XHR 的限制）
      Web({ src: 'live2d://live2d.html', controller: this.controller })
        .width('100%')
        .layoutWeight(1)
        .javaScriptAccess(true)
        .domStorageAccess(true)
        .backgroundColor(Color.Transparent)
        .onControllerAttached(() => {
          // 在 Web 组件加载前绑定 scheme handler（live2d:// 资源由 rawfile 提供）
          RawfileScheme.bindHandler(this.controller);
        })
        .onPageEnd(() => {
          hilog.info(0x0000, TAG, 'onPageEnd');
          // 注册 JS 桥（对齐 window.ReactNativeWebView）
          this.controller.registerJavaScriptProxy(this.bridge, 'ReactNativeWebView', ['postMessage']);
          // 仅首次注册后重载一次：重载后 JS 在 init 时桥已存在，
          // 会发出 modelLoaded 事件；守卫避免 onPageEnd->refresh 死循环
          if (!this.reloadedOnce) {
            this.reloadedOnce = true;
            setTimeout(() => {
              this.controller.refresh();
            }, 300);
          }
        })

      // 状态区
      Text(this.modelLoaded)
        .fontSize(14)
        .fontColor(this.modelLoaded === '模型加载成功' ? Color.Green : Color.Red)
      Text(this.lastEvent)
        .fontSize(12)
        .maxLines(2)

      // 控制按钮：Native -> JS 方向验证
      Row({ space: 10 }) {
        Button('表情开心').onClick(() => {
          this.callJs("window.setExpression && window.setExpression('happy')");
        })
        Button('表情普通').onClick(() => {
          this.callJs("window.setExpression && window.setExpression('normal')");
        })
        Button('打印INFO').onClick(() => {
          this.callJs('console.log("native-js-ok")');
        })
      }.padding(12)
    }
    .width('100%')
    .height('100%')
  }
}
