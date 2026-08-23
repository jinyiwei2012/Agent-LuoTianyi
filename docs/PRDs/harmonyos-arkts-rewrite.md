# 鸿蒙 OS 客户端规划：ArkTS 原生重写

> 分支：`feat/harmonyos-arkts`（基于 `dev` b33d359）
> 目标：将现有 Expo/React Native 客户端（`app/`）的核心功能，以 **HarmonyOS NEXT 原生 ArkTS + ArkUI** 重写为鸿蒙版客户端。
> 技术背景：HarmonyOS NEXT（API 23 / HarmonyOS 6.1）已剥离 AOSP，仅支持 HAP 安装；官方开发语言 ArkTS（TypeScript 扩展）+ ArkUI 声明式 UI；DevEco Studio 6.x（Windows/macOS 均支持）。

## 一、目标与范围

### 1.1 目标
交付一个**功能对齐现有 Expo 客户端**的鸿蒙原生 App（HAP），覆盖：
- 登录 / 注册 / 自动登录（凭据加密存储）
- 与洛天依的文字聊天（WebSocket 全链路）
- 「图片识别 + 文字回复」（图片上传 → 服务端 VLM）
- Live2D 模特展示（WebView 加载本地资源 + JSBridge 消息交互）
- 情绪/思考气泡动画
- TTS 语音播放（服务端返回音频流，客户端播放）
- 历史记录 / 会话列表
- 设置（主题、偏好、服务器地址、LLM 模型设置——用户自带 API Key）
- 动态（发布/评论/查看未读）
- 触摸反应、语音播放控制等交互细节

### 1.2 非目标（第一期）
- 电话功能（v0.4.x 路线图，本期不做）
- 元服务（元卡片/HSP），本期只做常规 App
- 平板/手表/车机多端适配（仅手机 portrait）
- 分布式能力（虽然鸿蒙特色，但一期不引入，降低复杂度）

## 二、技术选型（ArkTS 原生）

| 项 | 选择 | 理由 |
|---|---|---|
| 语言 | ArkTS（strict 模式） | 官方唯一主流应用语言；TS 基础可迁移 |
| UI | ArkUI 声明式（`@Entry`/`@Component`/`@State` + Column/Row/List/Swiper） | 官方范式，性能好 |
| 状态管理 | V1 `@Observed` + `@Track`（**避免 V1/V2 混用**——官方已知崩溃坑） | 聊天 UI 状态简单，V1 稳定 |
| 路由 | 官方 `Navigation` + router（Stage 模型 UIAbility） | 标准方案 |
| 网络 | `@ohos.net.http`（HTTP）+ `@ohos.net.webSocket`（WebSocket） | 官方内置，无需三方 |
| 存储 | `@ohos.data.preferences`（KV 小数据）+ `@ohos.security.cryptoFramework`（加密）+ `@ohos.hiviewdfx.hilog`（日志） | 官方内置 |
| 音频播放 | `@ohos.multimedia.audio`（AVPlayer） | 官方内置 |
| 图片选择 | PhotoAccessHelper（相册）| 官方 API |
| Live2D | **WebView 加载本地 Web 资源**（`@ohos.web.webview` + WebviewController）——与 iOS/Android 一致方案 | Live2D 是 Web 技术栈（PixiJS+Cubism），原生 ArkUI 无 Live2D 渲染器，WebView + JSBridge 是唯一可行复用以方案 |
| 视频/图片展示 | `Image` 组件 + 网络图 | 官方组件 |

### 关键决策：Live2D 走 WebView 复用
现有 `public/live2d/`（html + js + model）**可直接复用**到鸿蒙的 `rawfile/` 目录，通过 `WebviewController.loadUrl('file:///...')`（鸿蒙 WebView 支持 rawfile 资源）+ `runJavaScript`/JSBridge 通信。与 iOS/Android 的 WebView 方案一致，**零重写 Live2D 渲染逻辑**。

### 2.1 为什么不走 RNOH（RN on OpenHarmony）
- 现有项目 **RN 0.81.5 + Expo SDK 54**，而 RN-OH 仅稳定支持 **RN 0.72.5**（0.77 尝鲜）——需大幅降级，且 React Compiler / 新架构特性不兼容
- Expo 项目需 eject + 逐包替换鸿蒙适配版（expo-router/expo-av/expo-secure-store 等 15+ 包），适配成本 ≥ 原生重写
- **结论**：重写更可控，且最终原生质量更高

## 三、架构设计

### 3.1 模块划分（ArkTS 工程结构）
```
harmony/
├─ entry/src/main/ets/
│  ├─ entryability/EntryAbility.ets        # 应用入口 UIAbility
│  ├─ pages/
│  │  ├─ LoginPage.ets                    # 登录/注册
│  │  ├─ ChatPage.ets                     # 主聊天（Live2D + 消息流）
│  │  ├─ HistoryPage.ets                  # 历史会话
│  │  ├─ SettingsPage.ets                 # 设置总览
│  │  ├─ LlmSettingsPage.ets              # LLM 模型设置（自备 Key）
│  │  └─ DynamicsPage.ets                 # 动态列表/发布
│  ├─ model/                              # 数据模型（与协议对齐）
│  │  ├─ types.ets                        # WS 事件/消息/用户模型
│  │  └─ conversation.ets                 # 会话/历史模型
│  ├─ service/                            # 服务层（对应用 app/utils）
│  │  ├─ WsTransport.ets                  # WebSocket 连接管理（心跳/重连/ack）
│  │  ├─ MessageProcessor.ets             # 收包分发/LLM 委托执行（llm_request/response）
│  │  ├─ NetworkClient.ets                # HTTP REST（登录/注册/历史/动态/LLM providers）
│  │  ├─ Live2DFacade.ets                 # WebView 控制器 + JSBridge 封装
│  │  ├─ AudioPlayer.ets                  # TTS 音频播放
│  │  ├─ SecurityStore.ets                # 凭据/LLM Key 加密存储
│  │  ├─ ThemeService.ets                 # 主题
│  │  └─ DebugTrace.ets                   # 调试轨迹（对齐 addDebugTrace）
│  └─ common/                             # 通用组件/常量
│     ├─ MessageBubble.ets                # 聊天气泡
│     ├─ ThinkingBubble.ets               # 思考动画
│     └─ Constants.ets
├─ entry/src/main/resources/
│  ├─ rawfile/live2d/                     # Live2D web 资源（直接迁移 public/live2d）
│  └─ base/media/                         # 图标等
```

### 3.2 关键服务设计

#### WsTransport（对齐现有 ws_transport.ts 功能）
- 连接生命周期：connect → auth → ready → io 循环；断线指数退避重连
- 心跳：`hb_ping`/`hb_pong`（10s 间隔，60s 超时判死）
- 消息可靠性：`request_id` 收发配对 + ACK waiter（server_ack 完成 promise）
- 事件分派：对不同 `type` 分发到 MessageProcessor 回调

#### MessageProcessor（对齐 message_processor.ts 功能）
- 收包：agent_message / agent_state_changed / system_message / error / llm_request / server_ack 等
- **LLM 委托执行**（对齐 PR53 的 client 侧）：收到 `llm_request` 时读本地 LLM 配置（SecureStore）→ 调 OpenAI 兼容 `chat/completions` → 回传 `llm_response`；异常回传 `{request_id, error}`
- `getLlmMode`：上报已启用类型（`llm_mode.types`）

#### SecurityStore（对齐 credential/llm_key_storage）
- 登录 token + server_url + LLM 模块配置（整份 JSON 原子写入）
- 敏感字段加密（`@ohos.security.cryptoFramework` + 系统安全存储）

#### Live2DFacade（关键创新点复用）
- WebView 加载 rawfile 中的 `live2d/live2d.html`
- JSBridge：`runJavaScript` 注入 + `onConsoleMessage`/JS 回调接收（对齐现有 postMessage 协议）
- 状态合成：模型就绪、触摸事件、表情/口型同步（`agent_state_changed` → 表情）

### 3.3 与现有协议完全对齐
客户端必须**严格对齐**服务端 `统一事件协议.md`：
- 事件类型串（`user_text`/`user_image`/`agent_message`/`llm_request`/`llm_response`/`hb_ping` 等）
- `llm_mode.types` 上报（PR53 类型化委托）
- ACK/request_id 语义（重发去重）

## 四、阶段计划（里程碑）

### 阶段 0：环境与验证（3-5 天）
- DevEco Studio 6.x 安装 + HarmonyOS SDK（需华为开发者账号实名）
- ArkTS 严格模式 + V1 状态管理 demo（验证 `@Observed`/`@Track` 写法，避免混用崩）
- **WebView + Live2D 验证**：先跑通 rawfile 加载 live2d.html + JSBridge 消息往返（**最大技术风险项，优先验证**）
- 产出：可运行的 "Live2D 展示 demo" HAP

### 阶段 1：账号与连接（1 周）
- LoginPage：登录/注册/自动登录（RSA 登录、邀请码注册——对齐 login.tsx）
- SecurityStore + NetworkClient（HTTP 层）
- WsTransport 基础：连接/auth/hb/重连
- 产出：能登录进主界面，WebSocket 存活，收到 agent_message 显示为文本

### 阶段 2：核心聊天（2 周）
- ChatPage 主界面：Live2D + 消息流（气泡/思考动画/输入框/发送/图片）
- MessageProcessor 完整事件路由
- TTS 音频播放
- 触摸反应、语音播放控制
- 产出：**与现有 App 功能对等的聊天体验**（可日常使用）

### 阶段 3：功能补齐（1.5 周）
- 历史记录/会话恢复（getHistory）
- 设置页（主题/URL/偏好）+ LLM 设置页（**用户自备 Key + 探测校验 + SecureStore 存储**，对齐 llm_settings.tsx）
- 动态页（发布/评论/未读，对齐 dynamics.tsx）
- 产出：全功能对齐

### 阶段 4：打磨与上架（1 周）
- 调试与性能（列表虚拟化、图片缓存、音频缓冲优化）
- 隐私合规（权限最小化声明：网络/相册/麦克风；隐私政策）——鸿蒙审核重点
- 真机回归（Mate/nova 系列）+ 崩溃率监控
- 签名（开发者证书）+ AppGallery Connect 提审
- 产出：**上架 HAP**

总量：约 **6-8 周**（单人全职），视阶段 0 Live2D 验证结果浮动。

## 五、测试策略

| 层 | 方案 |
|---|---|
| 单元 | ArkTS 单测（DevEco 内置 unittest 支持 `@ohos/hypium`），重点：MessageProcessor 事件路由、WS ack、LLM payload 构建 |
| 协议对齐 | 对照 `server/docs/dev/统一事件协议.md` + WS 测试脚本（本地起 server 联调） |
| UI | DevEco Previewer + 真机（模拟器内存/性能有差异，真机优先） |
| 集成 | 本地 server（`conda activate lty && python server_main.py`）全链路：登录→发消息→回复→TTS→图片→LLM 委托 |

## 六、主要风险与对策

| 风险 | 等级 | 对策 |
|---|---|---|
| **Live2D WebView 加载失败**（rawfile 路径/JSBridge/性能与 iOS/Android 差异） | 🔴 高 | 阶段 0 优先验证；若 WebView 性能差，降级方案：改为 Web 端渲染（远程 HTTPS 加载 live2d.html，纯 JS 性能可接受）或后续做原生 Live2D 渲染（成本高，暂缓） |
| **V1/V2 状态管理混用崩溃** | 🟡 中 | 全程只用 V1（`@Observed`+`@Track`）；建立编码规范（禁混用）；阶段 0 demo 验证 |
| **TTS 长音频播放卡顿**（GPT-SoVITS 音频流） | 🟡 中 | 音频缓冲 + 分段预加载；参考现有 client 的音频缓存策略 |
| **鸿蒙审核拒单**（权限滥用/隐私政策/内容敏感） | 🟡 中 | 权限最小化声明；隐私政策提前准备；接触审核文档 |
| **跨平台协议漂移**（鸿蒙端与服务端 ws 协议不匹配） | 🟡 中 | 以服务端协议文档为准实现；三端（iOS/Android/Harmony）协议一致性回归 |
| **ArkTS 严格模式迁移成本**（TS 动态特性被禁） | 🟢 低 | 编写前过一遍 ArkTS 严格模式手册（对象字面量/动态 import/for...in 等写法差异） |

## 七、资源复用清单

| 现有资产 | 复用方式 |
|---|---|
| `app/public/live2d/`（html/js/model 全套） | **直接拷贝**到 `rawfile/live2d/`，零修改 |
| `app/assets/images/`（图标/气泡图） | 拷贝到 `resources/base/media/` |
| 协议/事件类型（ws_transport/message_processor 类型定义） | 翻译为 ArkTS 类型（结构一致） |
| live2d_helper.ts（JSBridge 协议） | 翻译为 `@ohos.web.webview` 调用 |
| LLM 委托逻辑（llm_client/llm_key_storage） | 重写为 ArkTS（逻辑照搬） |
| 服务端 | **零改动**（客户端完全对齐现有协议） |

## 八、交付物与检查点

- 每阶段结束：真机 demo + 与现有 App 并排行为对比（协议/功能）
- 阶段 0 结束：**Live2D Demo + 技术可行性结论**（若不可行，立即转 Plan B——远程 Web 渲染）
- 最终：AppGallery 上架 HAP + 全功能回归清单

## 九、验收标准

1. 通过鸿蒙真机完成：注册→登录→自动登录→文字聊天→图片理解→TTS→历史→动态→LLM 自备 Key 设置全流程
2. Live2D 表现（表情口型同步、触摸反应）与 Android/iOS 版一致
3. WebSocket 断线重连 / llm_request 委托异常回传 等错误路径正确
4. 隐私合规（仅申请必要权限）

## 十、真机验收检查表（执行版）

> 前置：`scripts\build_harmony.ps1` 构建通过，产物 `harmony\entry\build\default\outputs\default\entry-default-unsigned.hap`。
> 需连真机/模拟器：`hdc list targets` 能看到设备；未签名 HAP 在开发机可直装（商用需 DevEco 登录自动签名）。

### 0. 设备连接
- [ ] `hdc list targets` 返回设备（如 `127.0.0.1:5555` 或真机序列号）
- [ ] 手机已开「开发者选项 → USB 调试」，PC 已装 HDC 驱动
- [ ] 安装：`hdc install <hap路径>` 或用 DevEco 直接运行 entry

### 1. Live2D 验证页（阶段 0 技术项，优先级最高）
- [ ] 首页 →「Live2D 验证」进入 `Live2DTestPage`
- [ ] **Live2D 模型渲染**（洛天依绿色长发模型）
- [ ] 状态显示「模型加载成功」（绿色）
- [ ] 点「表情开心/普通」→ 模型表情切换（Native→JS 方向）
- [ ] 触摸画布 → 显示「触摸区域: [...]」（JS→Native 方向）
- 排查：`hdc shell hilog | grep Live2DTest`

### 2. 账号与连接（阶段 1）
- [ ] 首页 →「进入聊天」→ 登录页
- [ ] 服务器地址设置（默认 `https://www-api.u3493359.nyat.app:11664`）
- [ ] 注册（账号+密码+邀请码）→ 自动切到登录 tab
- [ ] 登录 → 自动进 ChatPage，顶栏「连接中→已连接」
- [ ] 勾选自动登录后重开 App → 直接进 ChatPage
- [ ] 密码 RSA 加密链路：错误密码应提示「登录失败」（而非加密失败）
- 排查：`hdc shell hilog | grep AuthService`

### 3. 核心聊天（阶段 2）
- [ ] ChatPage 顶部显示 Live2D 模型
- [ ] 发文字 → 自气泡（submitted）；天依回复（agent_message）显示
- [ ] 发图片（点「图片」→ 相册选图）→ 图片气泡
- [ ] 天依回复时**口型/表情同步**(Live2D 演绎)
- [ ] 天依回复**语音播放**（TTS 分块流，WebView JS 引擎）
- [ ] 触摸 Live2D → 上报 user_touch（天依发声/反应）
- [ ] 断开网络 → 断线重连提示；恢复后自动重连
- 排查：`hdc shell hilog | grep -E "WsTransport|Live2DFacade"`

### 4. LLM 设置（阶段 2/3 委托）
- [ ] 设置 →「LLM 模型设置」
- [ ] 模型类型列表加载（`GET /llm/providers`）
- [ ] 填 Base URL + API Key → 「探测」显示可用模型数
- [ ] 保存 → 发消息时若服务端下发 `llm_request`，客户端直接调用成功（用户 Key 不落服务端）
- 排查：`hdc shell hilog | grep LlmSettingsPage`

### 5. 设置与偏好（阶段 3）
- [ ] ChatPage 顶栏「设置」→ 设置页
- [ ] 修改服务器地址并保存
- [ ] 相处模式偏好（关系/说话风格/人设/上下文）→「保存偏好」成功
- [ ] 退出登录 → 回登录页

### 6. 动态（阶段 3）
- [ ] 设置 →「动态」
- [ ] 动态列表加载；发布一条动态成功
- [ ] 点「评论 N」展开评论；发表评论成功（计数 +1）

### 7. 历史记录（阶段 3）
- [ ] 重登后进入 ChatPage，历史消息自动加载（最近 20 条）
- [ ] 图片历史暂缓回连（get_image 未实现，仅文本）

### 8. 收尾
- [ ] 全流程无崩溃/ANR
- [ ] 截取关键界面（登录/聊天/LLM 设置/动态）存档

## 十一、当前实现状态（feat/harmonyos-arkts）

- 分支：`feat/harmonyos-arkts`（基于 dev `b33d359`），19 个原子提交
- 已实现：工程骨架、Live2D WebView、登录/注册/自动登录（RSA-OAEP-SHA256）、
  WebSocket 全链路（auth/心跳/ack/重连）、文字/图片聊天（llm_mode 上报）、
  Live2D 表情/口型/触摸、TTS 分块流、LLM 委托（PR53）、
  历史记录、LLM 设置、设置/偏好、动态/评论
- 未实现（延期项）：历史图片回连（get_image）、动态未读红点、主题切换、登录页主题
- 验收：**待真机**（`hdc list targets` 为空，需连接设备后按上表逐项勾选）
