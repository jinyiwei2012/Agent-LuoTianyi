# 方案文档 - AgentLuo Web 网页版

## 更新日志
- 2026-08-24：创建第一版方案文档，完成可行性分析、技术选型、Web 适配拆解与实施路径。

## 1. 背景与动机
AgentLuo 目前提供三种客户端形态：

| 客户端 | 技术栈 | 定位 |
| --- | --- | --- |
| `client/` | PySide6（Python 桌面） | PC 桌面客户端 |
| `app/` | Expo / React Native | 移动端（Android / iOS） |
| `harmony/` | HarmonyOS ArkTS | 鸿蒙端 |

现有客户端的共同局限是**使用门槛高**：需要下载安装、依赖特定操作系统、无法即点即用。用户期望能通过浏览器直接访问、无需安装即可与天依互动，降低触达成本，也便于快速分享演示。

本方案旨在为 AgentLuo 增加 **Web 网页版**，让用户通过浏览器直接使用完整的对话、Live2D 互动与语音能力。

## 2. 结论先行
**可行性很高，且属于"顺水推舟"而非从零开发。** 原因是现有 `app/`（Expo / React Native）**天生支持 Web 输出**：

- 依赖已包含 `react-native-web`、`react-dom`；
- `package.json` 已存在 `web` 脚本（`expo start --web`）；
- `app.json` 已配置 `web.output: "static"`。

也就是说，Web 版可以**复用 `app/` 绝大部分代码**，只需适配少量平台差异，而不是重写客户端。

> 强烈不建议重写 PySide6 桌面客户端（`client/`）为 Web：投入大、收益低。Web 化的正确路径是复用 `app/`。

## 3. 目标与非目标
### 3.1 目标
1. 用户通过浏览器访问网页即可与天依对话。
2. 复用 `app/` 现有的通信层、聊天逻辑与大部分 UI。
3. 在 Web 上还原核心体验：文字/图片输入、TTS 语音播放、Live2D 表情与口型同步。
4. 与现有服务端（FastAPI + HTTP + WebSocket）完全兼容，服务端无需大规模改动。

### 3.2 非目标（本期不做）
- 重写 PySide6 桌面客户端。
- 重构服务端消息协议。
- 引入新的实时通信技术（如 WebRTC）替代现有 WebSocket。
- 移动端 H5 与原生 App 的差异化深度定制（本期以"能跑通"为优先）。

## 4. 现状调研：`app/` 各模块 Web 兼容性
逐项核对 `app/` 关键代码后，按"是否兼容 Web"分类如下。

### 4.1 原生兼容 Web（可直接复用）
- **WebSocket 通信**（`utils/ws_transport.ts`）：使用标准 `WebSocket` API，跨平台通用。聊天流、重连、心跳、ack 机制在 Web 上可直接运行。
- **聊天 UI**（`app/index.tsx`、`components/ChatBubbles.tsx`）：纯 React Native 组件（`FlatList` / `Animated` / `Pressable` 等），`react-native-web` 均可渲染。
- **认证 / 登录 / 动态 / 偏好 / LLM 设置** 等页面：同为标准 RN 组件，与平台无关。
- **状态管理**（`hooks/useAuth`、`useChatLogic`、`useHistoryLogic`）：纯 React hooks，与平台无关。

### 4.2 需要适配（Web 化的主要工作量）
| 模块 | 现状 | Web 问题 | 适配方向 |
| --- | --- | --- | --- |
| **Live2D 渲染** | `react-native-webview` 加载本地 `public/live2d/live2d.html`（PixiJS + Cubism） | `react-native-webview` **不支持 Web 平台** | Live2D 本身是纯 Web 技术，改用 `<iframe>` 嵌入，或把 PixiJS 逻辑作为 Web 组件直接渲染 |
| **音频播放（TTS）** | `utils/message_processor.ts` 用 `expo-av` 的 `Audio.Sound` 播放流式 base64 音频 | `expo-av` 在 Web 支持有限 | 改用 HTML5 `<audio>` / `AudioContext`，base64 转 Blob URL 播放 |
| **本地存储** | `AsyncStorage`、`expo-secure-store`、`expo-file-system`、`expo-clipboard`、`expo-haptics` | 各模块 Web 支持不一 | `AsyncStorage`→`localStorage`（有 polyfill）；`SecureStore`→WebCrypto + `sessionStorage`；文件系统→不落盘改内存播放；clipboard/haptics 有替代或可禁用 |
| **WebView bridge** | `live2d_helper.ts` 用 `injectJavaScript` / `onMessage` 控制表情口型 | WebView API 不适用 | `<iframe>` 用 `postMessage` 替代，逻辑等价 |

### 4.3 服务端侧现状（利好条件）
- 服务端 `BASE_URL` 已使用 HTTPS（`https://www-api.u3493359.nyat.app:11664`），具备公网 HTTPS 部署基础。
- 服务端已有 `admin_ui` 静态前端通过 FastAPI 直接托管（构建产物 `admin_static/`），为"Web 前端由服务端托管"提供了现成先例。

## 5. 技术方案选型
### 5.1 推荐路径（分阶段演进）
**阶段一：复用 `app/` 输出 Web（最快验证）**
- 用 Expo 的 Web 输出（`npx expo export --platform web`），通过 `react-native-web` 直接生成网页。
- 适配 4.2 中的四块（Live2D、音频、存储、WebView bridge）。
- 适合：快速验证可行性、个人使用、局域网访问、演示。

**阶段二：独立 Web 前端（可选，质量更高）**
- 不复用 RN 组件，用 React + Vite/Next.js 重写专为 Web 优化的界面（桌面宽屏布局：Live2D 侧边、聊天主区）。
- 服务端 HTTP + WebSocket 接口完全复用，只写前端。
- 适合：作为正式产品形态、需要更好的桌面体验时。

**推荐：先做阶段一，达到可用后按需演进到阶段二。**

### 5.2 部署形态（推荐）
最优雅的形态是**服务端直接托管 Web 静态文件**：

- 将 Web 前端 build 产物交由 FastAPI 服务托管（类似现有 `admin_ui` 的处理方式）。
- 同一端口/域名同时提供聊天页面与管理控制台，用户访问一个网址即可使用，无需单独部署前端。
- 备选：独立静态托管（Nginx / GitHub Pages / 对象存储），Web 前端通过 HTTP/WS 连接独立部署的 FastAPI 服务端。

## 6. 关键现实考量
### 6.1 CORS / 跨域
Web 页面与服务端若不同源，需要服务端配置 CORS（FastAPI `CORSMiddleware`）并允许跨域 WebSocket。这是 Web 化**必须**处理的，移动 App 无此问题。

### 6.2 HTTPS 强制
浏览器安全策略下，`https` 页面不能连接 `ws://`（非加密）或 `http://` 接口，会触发混合内容拦截。Web 版公网访问时服务端需走 HTTPS。现有 HTTPS 部署基础已具备。

### 6.3 Live2D 资源复用
`public/live2d/` 内含完整 Cubism 模型（`.moc3`、`.model3.json`、表情 `.exp3.json` 等）与 PixiJS 运行时，这些**本身就是 Web 资源**，Web 上可直接复用，无需重新制作。

## 7. 实施拆解（阶段一）
### 7.1 Live2D 承载适配
- 将 `public/live2d/live2d.html` 通过 `<iframe>` 嵌入 Web 页面。
- 将 `live2d_helper.ts` 的 `injectJavaScript` / `onMessage` 改为 `postMessage` / 监听 `message` 事件，控制表情与口型同步。
- Live2D 渲染逻辑（PixiJS）不改，仅换承载容器。

### 7.2 音频播放适配
- 重写 `message_processor.ts` 中 `expo-av` 相关播放逻辑，改用 Web `AudioContext` / `<audio>`。
- 保留消息协议、音频优先级互斥逻辑（本地 TTS 与服务器音频的抢占/互斥）。
- 历史 TTS 音频改为不落盘、直接内存播放。

### 7.3 本地存储适配
- `AsyncStorage` → Web `localStorage`。
- `expo-secure-store`（存 LLM key、token）→ WebCrypto + `sessionStorage`/`localStorage`（注意安全性）。
- `expo-file-system`（读本地 wav）→ 移除或改内存。
- `expo-clipboard` / `expo-haptics` → 使用 Web 替代方案或按平台禁用。

### 7.4 服务端配合
- 配置 CORS 中间件，允许 Web 前端跨域访问 HTTP 与 WebSocket。
- 确认 HTTPS 反代可用，避免混合内容拦截。
- 可选：将 Web 前端构建产物接入 FastAPI 静态托管。

## 8. 风险与待确认问题
1. **Live2D 在 iframe 中的跨域与消息通信**：需确认本地静态资源与 Web 页面同源或 CORS 配置正确，保证 `postMessage` 正常。
2. **流式音频在 Web 上的延迟与兼容性**：不同浏览器对 `AudioContext` / `MediaSource` 的流式播放支持有差异，需在目标浏览器实测。
3. **安全存储降级**：Web 端无法使用系统级安全存储（Keychain/Keystore），LLM key / token 的存储安全性下降，需明确降级方案与风险。
4. **桌面宽屏布局体验**：阶段一直接复用移动端布局，在桌面宽屏上可能不够理想；如需更佳体验，需推进阶段二独立前端。
5. **多客户端一致性**：Web 版与 App / PC 客户端需保持行为一致，避免体验分裂。

## 9. 验收标准（阶段一）
1. 用户可通过浏览器访问网页完成注册 / 登录。
2. 用户可在网页中与天依进行文字对话。
3. 用户可发送图片，天依可回复（含图片理解链路）。
4. 天依的 TTS 语音可在网页中正常播放（含流式输出）。
5. Live2D 模型可在网页中正常加载，表情与口型可随对话同步。
6. 动态、偏好、LLM 设置等次级页面可用。
7. 服务端 CORS 与 HTTPS 配置正确，无混合内容 / 跨域报错。

## 10. 参考
- 现有 Web 输出配置：`app/package.json` 的 `web` 脚本、`app/app.json` 的 `web.output`。
- 服务端静态托管先例：`server/res/admin_ui/admin_static/`。
- 通信层：`app/utils/ws_transport.ts`、`app/utils/chat_stream.ts`、`app/utils/message_processor.ts`。
- Live2D 承载：`app/utils/live2d_helper.ts`、`app/public/live2d/live2d.html`。
