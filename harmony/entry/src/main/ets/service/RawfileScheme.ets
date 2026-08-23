import { webview } from '@kit.ArkWeb';
import { WebNetErrorList } from '@ohos.web.netErrorList';
import { common } from '@kit.AbilityKit';
import { hilog } from '@kit.PerformanceAnalysisKit';

const TAG = 'RawfileScheme';
const SCHEME = 'live2d';
/** rawfile 内资源根目录（live2d/） */
const RAW_ROOT = 'live2d';

/**
 * 自定义 scheme（live2d://）服务（官方 WebSchemeHandler 方案）：
 * resource://rawfile 不支持 fetch/XHR（CORS 白名单无 resource），
 * Live2D 的 model3.json/mouth_value_proj.json 走 fetch 必然失败。
 * 方案：customizeSchemes(live2d://, isSupportFetch+CORS+Standard)
 * + WebviewController.setWebSchemeHandler 提供 rawfile 字节，
 * 主文档/子资源/fetch 全部由 handler 统一响应。
 */
export class RawfileScheme {
  private static registered = false;
  private static handlerBound = false;
  private static handler: webview.WebSchemeHandler = new webview.WebSchemeHandler();

  /** App 入口注册一次（须在任何 Web 组件加载前调用） */
  static registerScheme(): void {
    if (RawfileScheme.registered) {
      return;
    }
    const scheme: webview.WebCustomScheme = {
      schemeName: SCHEME,
      isSupportCORS: true,
      isSupportFetch: true,
      isStandard: true,
      isLocal: true,
      isSecure: true,
      isCodeCacheSupported: true,
    };
    webview.WebviewController.customizeSchemes([scheme]);
    RawfileScheme.registered = true;
    hilog.info(0x0000, TAG, 'scheme %{public}s registered', SCHEME);
  }

  /** 绑定 handler 到控制器（必须在 loadUrl 之前调用） */
  static bindHandler(controller: webview.WebviewController): void {
    if (RawfileScheme.handlerBound) {
      return;
    }
    const handler = RawfileScheme.handler;
    handler.onRequestStart((request: webview.WebSchemeHandlerRequest,
      resourceHandler: webview.WebResourceHandler): boolean => {
      RawfileScheme.serve(request, resourceHandler);
      return true;
    });
    controller.setWebSchemeHandler(SCHEME, handler);
    RawfileScheme.handlerBound = true;
    hilog.info(0x0000, TAG, 'handler bound');
  }

  /** URL 到 rawfile 路径：
   *  live2d://live2d.html           -> live2d/live2d.html
   *  live2d://live2d.html/js/a.js   -> live2d/js/a.js
   *  live2d://js/a.js               -> live2d/js/a.js
   */
  private static resolveRawPath(url: string): string {
    const prefix = `${SCHEME}://`;
    if (!url.startsWith(prefix)) {
      return '';
    }
    let rest = url.substring(prefix.length);
    // 去掉 query/hash
    const q = rest.indexOf('?');
    if (q >= 0) {
      rest = rest.substring(0, q);
    }
    const h = rest.indexOf('#');
    if (h >= 0) {
      rest = rest.substring(0, h);
    }
    rest = rest.replace(/^\/+/, '');
    rest = rest.replace(/\/+$/, '');
    // URL 解码（motion 文件名含中文/空格，会被编码为 %XX）
    try {
      rest = decodeURIComponent(rest);
    } catch (e) {
      // 解码失败按原样处理
      hilog.warn(0x0000, TAG, 'decode fail: %{public}s', (e as Error).message);
    }
    if (rest.length === 0) {
      return 'live2d/live2d.html';
    }
    // 相对解析：子资源 URL 形如 live2d.html/js/a.js（基于主文档路径），
    // 去掉首段「live2d.html」前缀，其余按 rawfile 相对路径即 live2d/ 下。
    if (rest.startsWith('live2d.html/')) {
      rest = rest.substring('live2d.html/'.length);
    }
    if (rest.length === 0) {
      return 'live2d/live2d.html';
    }
    return `${RAW_ROOT}/${rest}`;
  }

  private static mimeFor(path: string): string {
    const lower = path.toLowerCase();
    if (lower.endsWith('.html')) {
      return 'text/html';
    }
    if (lower.endsWith('.js')) {
      return 'application/javascript';
    }
    if (lower.endsWith('.json')) {
      return 'application/json';
    }
    if (lower.endsWith('.png')) {
      return 'image/png';
    }
    if (lower.endsWith('.jpg') || lower.endsWith('.jpeg')) {
      return 'image/jpeg';
    }
    if (lower.endsWith('.webp')) {
      return 'image/webp';
    }
    if (lower.endsWith('.mp3')) {
      return 'audio/mpeg';
    }
    if (lower.endsWith('.wav')) {
      return 'audio/wav';
    }
    return 'application/octet-stream';
  }

  /** 读取 rawfile 字节并返回；找不到返回 null */
  private static readRaw(rawPath: string): Uint8Array | null {
    const ctx = getContext() as common.UIAbilityContext;
    try {
      return ctx.resourceManager.getRawFileContentSync(rawPath);
    } catch (e) {
      hilog.warn(0x0000, TAG, 'rawfile miss %{public}s: %{public}s', rawPath, (e as Error).message);
      return null;
    }
  }

  /** 处理单个请求：响应头 + body + finish */
  private static serve(request: webview.WebSchemeHandlerRequest,
    resourceHandler: webview.WebResourceHandler): void {
    const url = request.getRequestUrl();
    const rawPath = RawfileScheme.resolveRawPath(url);
    hilog.info(0x0000, TAG, 'serve url=%{public}s -> raw=%{public}s', url, rawPath);
    if (rawPath.length === 0) {
      resourceHandler.didFail(WebNetErrorList.ERR_FILE_NOT_FOUND);
      return;
    }
    const bytes = RawfileScheme.readRaw(rawPath);
    if (bytes === null) {
      resourceHandler.didFail(WebNetErrorList.ERR_FILE_NOT_FOUND);
      return;
    }
    const response = new webview.WebSchemeHandlerResponse();
    response.setStatus(200);
    response.setMimeType(RawfileScheme.mimeFor(rawPath));
    response.setHeaderByName('Access-Control-Allow-Origin', '*', true);
    response.setHeaderByName('Cache-Control', 'no-cache', true);
    resourceHandler.didReceiveResponse(response);
    resourceHandler.didReceiveResponseBody(bytes.buffer as ArrayBuffer);
    resourceHandler.didFinish();
    hilog.info(0x0000, TAG, 'served %{public}s (%{public}d bytes)', rawPath, bytes.length);
  }

  /** 页面跳转入口（供页面在 onPageEnd 后勿重复绑定用） */
  static get isBound(): boolean {
    return RawfileScheme.handlerBound;
  }
}
