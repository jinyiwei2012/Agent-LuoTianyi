import { router } from '@kit.ArkUI';
import { webview } from '@kit.ArkWeb';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { AuthService } from '../service/AuthService';
import { WsTransport, WsCallbacks, SendTouchPayload } from '../service/WsTransport';
import { Live2DFacade, Live2DEvents } from '../service/Live2DFacade';
import { LlmConfigStore } from '../service/LlmConfigStore';
import { ImagePicker } from '../service/ImagePicker';
import { HistoryService } from '../service/HistoryService';
import { RawfileScheme } from '../service/RawfileScheme';
import { SecurityStore } from '../service/SecurityStore';
import { DynamicsService } from '../service/DynamicsService';
import { PreferencesService } from '../service/PreferencesService';
import { addDebugTrace, clearDebugTrace, getDebugTrace, DebugTraceEntry, formatDebugTs } from '../service/DebugTrace';
import {
  AppTheme, ColorMode, THEME_LIGHT, THEME_DARK, resolveTheme,
  COLOR_MODE_LABEL_LIST, COLOR_MODE_STORAGE_KEY, colorModeLabel,
  MSG_FONT_SIZE, BUBBLE_PADDING_V, BUBBLE_PADDING_H, BUBBLE_RADIUS,
  BUBBLE_TAIL_RADIUS, BUBBLE_MAX_WIDTH,
} from '../common/Theme';
import { AgentMessagePayload, ChatMessage, SendStatus, createSystemMessage } from '../model/ChatModels';
import { LlmSettingsDialog } from './LlmSettingsDialog';
import { DynamicsDialog } from './DynamicsDialog';

const TAG = 'ChatPage';
const LIVE2D_HEIGHT_PCT = 40;
const DRAWER_WIDTH_PCT = 38;

@Entry
@Component
struct ChatPage {
  // ---- 主题 ----
  @State themeName: string = 'light';
  @State systemDark: boolean = false;
  @State colorMode: string = 'light';

  // ---- 聊天状态 ----
  @State messages: ChatMessage[] = [];
  @State inputText: string = '';
  @State inputHeight: number = 40;
  @State agentState: string = '';
  @State live2dReady: boolean = false;
  @State modelLoaded: string = '';
  @State thinkingFrame: number = 0;
  @State hasDynamicsUnread: boolean = false;
  @State dynamicsUnreadCount: number = 0;

  // ---- 抽屉/弹层 ----
  @State drawerOpen: boolean = false;
  @State debugOpen: boolean = false;
  @State debugEntries: DebugTraceEntry[] = [];
  @State showPreferences: boolean = false;
  @State showLlmSettings: boolean = false;
  @State showDynamics: boolean = false;

  // ---- 偏好表单 ----
  @State prefsRelationship: string = '';
  @State prefsSpeakingStyle: string = '';
  @State prefsPersonalityText: string = '';
  @State prefsCustomContext: string = '';
  @State prefsLoading: boolean = false;

  // ---- 动态 ----
  @State dynamicsInput: string = '';
  @State dynamicsSubmitting: boolean = false;

  private transport: WsTransport | null = null;
  private live2d: Live2DFacade | null = null;
  private thinkingTimer: number = -1;

  aboutToAppear(): void {
    RawfileScheme.registerScheme();
    // 恢复颜色模式
    this.loadColorMode();
    if (!AuthService.state.isLoggedIn) {
      router.replaceUrl({ url: 'pages/LoginPage' });
      return;
    }
    const username = AuthService.state.username;
    const token = AuthService.state.messageToken;
    addDebugTrace('auth', 'chat page appear', `user=${username}`);

    const callbacks: WsCallbacks = {
      onAgentMessage: (payload: AgentMessagePayload) => {
        this.handleAgentMessage(payload);
      },
      onAgentStateChanged: (state: string) => {
        this.agentState = state;
        addDebugTrace('agent', 'state changed', state);
      },
      onError: (errorText: string) => {
        addDebugTrace('ws', 'error', errorText);
        this.pushSystemMessage(errorText);
      },
    };

    const live2dEvents: Live2DEvents = {
      onModelLoaded: (ok: boolean, error?: string) => {
        this.live2dReady = ok;
        this.modelLoaded = ok ? '模型已加载' : `加载失败: ${error ?? ''}`;
        addDebugTrace('live2d', ok ? 'model loaded' : 'model load failed', error);
      },
      onTouch: (touchArea: string, raw: string) => {
        this.reportTouch(touchArea, raw);
      },
      onAudioStopped: () => {
        addDebugTrace('audio', 'server audio stopped');
      },
      onAudioFinished: () => {
        addDebugTrace('audio', 'server audio finished');
      },
    };

    this.live2d = new Live2DFacade(live2dEvents);
    this.transport = new WsTransport(username, token, callbacks);
    this.setupLlmMode();
    this.loadHistory();
    this.transport.start();
    this.pushSystemMessage(`已登录：${username}`);
    this.refreshDynamicsUnread();
  }

  aboutToDisappear(): void {
    if (this.thinkingTimer !== -1) {
      clearInterval(this.thinkingTimer);
      this.thinkingTimer = -1;
    }
    if (this.transport !== null) {
      this.transport.stop();
      this.transport = null;
    }
    if (this.live2d !== null) {
      this.live2d.stopServerAudio();
      this.live2d = null;
    }
  }

  // ==================== 主题 ====================
  private async loadColorMode(): Promise<void> {
    const saved = await SecurityStore.getString(COLOR_MODE_STORAGE_KEY, 'light');
    this.colorMode = (saved === 'dark' || saved === 'system') ? saved : 'light';
    this.themeName = this.resolveThemeName();
  }

  private resolveThemeName(): string {
    const resolved = resolveTheme(this.colorMode as ColorMode, this.systemDark);
    return resolved.name;
  }

  private getTheme(): AppTheme {
    return resolveTheme(this.colorMode as ColorMode, this.systemDark);
  }

  private handleColorMode(mode: string): void {
    this.colorMode = mode;
    this.themeName = this.resolveThemeName();
    SecurityStore.setString(COLOR_MODE_STORAGE_KEY, mode).catch(() => {
      addDebugTrace('theme', 'save color mode failed', mode);
    });
  }

  // ==================== 消息 ====================
  private pushSystemMessage(text: string): void {
    const m = createSystemMessage(text, `sys-${Date.now()}`);
    this.messages = [...this.messages, m];
  }

  private async loadHistory(): Promise<void> {
    const result = await HistoryService.fetchHistory(0);
    if (result.messages.length > 0) {
      this.messages = [...result.messages, ...this.messages];
    }
  }

  private async setupLlmMode(): Promise<void> {
    if (this.transport === null) {
      return;
    }
    const types = await LlmConfigStore.getEnabledTypes();
    this.transport.setClientMode({ types: types });
  }

  private handleAgentMessage(payload: AgentMessagePayload): void {
    const uuid = payload.uuid ?? `agent-${Date.now()}`;
    const text = payload.text ?? '';
    const displayInChat = payload.display_in_chat !== false;

    if (displayInChat && text.length > 0 && payload.is_ephemeral !== true) {
      const m: ChatMessage = {
        uuid: uuid, type: 'text', content: text, isUser: false,
        timestamp: Date.now(), sendStatus: 'submitted',
        audioAvailable: payload.audio !== null && payload.audio !== undefined && payload.audio.length > 0,
        audioLocalUri: payload.audio ?? '', audioPlayState: 'idle',
      };
      this.messages = [...this.messages, m];
    }
    if (payload.expression !== null && payload.expression !== undefined && this.live2d !== null) {
      this.live2d.setExpression(payload.expression);
    }
    if (this.live2d !== null) {
      const audioChunk = payload.audio ?? '';
      if (audioChunk.length > 0) {
        this.live2d.feedAudioChunk(audioChunk, false);
      }
      if (payload.is_final_package === true) {
        this.live2d.feedAudioChunk('', true);
      }
    }
  }

  private reportTouch(touchArea: string, raw: string): void {
    if (this.transport === null || touchArea.length === 0) {
      return;
    }
    let frequency: Record<string, number> | null = null;
    try {
      const parsed = JSON.parse(raw) as Record<string, Object>;
      if (parsed['clickFrequency'] !== undefined) {
        const rawFreq = parsed['clickFrequency'] as Record<string, Object>;
        const freq: Record<string, number> = {};
        for (const k of Object.keys(rawFreq)) {
          freq[k] = Number(rawFreq[k] ?? 0);
        }
        frequency = freq;
      }
    } catch (e) {
      addDebugTrace('live2d', 'touch parse failed', (e as Error).message);
    }
    const payload: SendTouchPayload = { touch_area: touchArea };
    if (frequency !== null) {
      payload.click_frequency = frequency;
    }
    this.transport.sendWithAck('user_touch', payload, 10000, `t-${Date.now()}`);
  }

  private sendMessage(): void {
    const text = this.inputText.trim();
    if (text.length === 0 || this.transport === null) {
      return;
    }
    const m: ChatMessage = {
      uuid: `u-${Date.now()}`, type: 'text', content: text, isUser: true,
      timestamp: Date.now(), sendStatus: 'waiting',
      audioAvailable: false, audioLocalUri: '', audioPlayState: 'idle',
    };
    this.messages = [...this.messages, m];
    this.inputText = '';
    this.transport.submitUserText(text, false, 10000, m.uuid)
      .then((result) => {
        this.updateLocalMessage(m.uuid, result.ok ? 'submitted' : 'failed');
      });
  }

  private async sendImage(): Promise<void> {
    if (this.transport === null) {
      return;
    }
    const picked = await ImagePicker.pickAndRead();
    if (picked === null) {
      return;
    }
    const uuid = `img-${Date.now()}`;
    const m: ChatMessage = {
      uuid: uuid, type: 'image', content: picked.uri, isUser: true,
      timestamp: Date.now(), sendStatus: 'waiting',
      audioAvailable: false, audioLocalUri: '', audioPlayState: 'idle',
    };
    this.messages = [...this.messages, m];
    const result = await this.transport.submitUserImage(
      picked.imageBase64, picked.mimeType, picked.uri, 10000, uuid);
    this.updateLocalMessage(uuid, result.ok ? 'submitted' : 'failed');
  }

  private updateLocalMessage(uuid: string, status: SendStatus): void {
    this.messages = this.messages.map((m: ChatMessage) => {
      if (m.uuid === uuid) {
        const updated: ChatMessage = {
          uuid: m.uuid, type: m.type, content: m.content, isUser: m.isUser,
          timestamp: m.timestamp, sendStatus: status,
          audioAvailable: m.audioAvailable, audioLocalUri: m.audioLocalUri,
          audioPlayState: m.audioPlayState,
        };
        return updated;
      }
      return m;
    });
  }

  // ==================== 动态未读 ====================
  private async refreshDynamicsUnread(): Promise<void> {
    try {
      const status = await DynamicsService.getUnreadStatus();
      this.hasDynamicsUnread = status.has_unread;
      this.dynamicsUnreadCount = status.unread_count;
    } catch (e) {
      addDebugTrace('dynamics', 'unread fetch failed', (e as Error).message);
    }
  }

  // ==================== 抽屉操作 ====================
  private toggleDrawer(): void {
    this.drawerOpen = !this.drawerOpen;
  }

  private handleToggleDebug(): void {
    this.debugOpen = !this.debugOpen;
    this.drawerOpen = false;
    this.debugEntries = getDebugTrace().slice(-50).reverse();
  }

  private handleOpenPreferences(): void {
    this.showPreferences = true;
    this.drawerOpen = false;
    this.loadPreferences();
  }

  private handleOpenLlmSettings(): void {
    this.showLlmSettings = true;
    this.drawerOpen = false;
  }

  private handleOpenDynamics(): void {
    this.showDynamics = true;
    this.drawerOpen = false;
  }

  private async handleLogout(): Promise<void> {
    this.drawerOpen = false;
    await AuthService.logout();
    router.replaceUrl({ url: 'pages/LoginPage' });
  }

  // ==================== 偏好弹层 ====================
  private async loadPreferences(): Promise<void> {
    this.prefsLoading = true;
    try {
      const prefs = await PreferencesService.get();
      if (prefs !== null) {
        this.prefsRelationship = prefs.relationship;
        this.prefsSpeakingStyle = prefs.speaking_style;
        this.prefsPersonalityText = prefs.personality_text;
        this.prefsCustomContext = prefs.custom_context;
      }
    } catch (e) {
      addDebugTrace('prefs', 'load failed', (e as Error).message);
    }
    this.prefsLoading = false;
  }

  private async savePreferences(): Promise<void> {
    const ok = await PreferencesService.overwrite({
      relationship: this.prefsRelationship,
      speaking_style: this.prefsSpeakingStyle,
      personality_text: this.prefsPersonalityText,
      personality_traits: [],
      custom_context: this.prefsCustomContext,
    });
    addDebugTrace('prefs', ok ? 'saved' : 'save failed');
  }

  // ==================== 动态弹层 ====================
  private async publishDynamic(): Promise<void> {
    const content = this.dynamicsInput.trim();
    if (content.length === 0 || this.dynamicsSubmitting) {
      return;
    }
    this.dynamicsSubmitting = true;
    const post = await DynamicsService.createDynamic(content);
    this.dynamicsSubmitting = false;
    if (post !== null) {
      this.dynamicsInput = '';
      addDebugTrace('dynamics', 'publish ok');
    } else {
      addDebugTrace('dynamics', 'publish failed');
    }
  }

  // ==================== 思考气泡计时 ====================
  private updateThinking(): void {
    if (this.agentState !== 'thinking') {
      this.thinkingFrame = 0;
      if (this.thinkingTimer !== -1) {
        clearInterval(this.thinkingTimer);
        this.thinkingTimer = -1;
      }
      return;
    }
    if (this.thinkingTimer !== -1) {
      return;
    }
    this.thinkingTimer = setInterval(() => {
      this.thinkingFrame = (this.thinkingFrame + 1) % 3;
    }, 500);
  }

  build() {
    Stack() {
      // 主内容
      Column() {
        // Live2D 层（背景图 + WebView + 加载覆盖 + 思考气泡）
        Stack() {
          // 背景图
          Image($rawfile('ui/bg2.jpg'))
            .width('100%')
            .height('100%')
            .objectFit(ImageFit.Cover)

          // Live2D WebView
          if (this.live2d !== null) {
            Web({ src: 'live2d://live2d.html', controller: this.live2d.controller })
              .width('100%')
              .height('100%')
              .javaScriptAccess(true)
              .domStorageAccess(true)
              .backgroundColor(Color.Transparent)
              .onControllerAttached(() => {
                RawfileScheme.bindHandler(this.live2d!.controller);
              })
              .onPageEnd(() => {
                this.live2d?.bindBridgeAndReload();
              })
              .onErrorReceive((event) => {
                hilog.error(0x0000, TAG, 'webview error: %{public}s', String(event.error));
              })
          }

          // 主题覆盖层
          Column().width('100%').height('100%').backgroundColor(this.getTheme().live2dOverlay)

          // 加载覆盖层（"天依正在过来..."）
          if (!this.live2dReady) {
            Column({ space: 8 }) {
              Text('天依正在过来...')
                .fontSize(13)
                .fontColor(this.getTheme().text)
            }
            .width('100%')
            .height('100%')
            .justifyContent(FlexAlign.Center)
            .alignItems(HorizontalAlign.Center)
          }

          // 思考气泡动画
          if (this.agentState === 'thinking') {
            Image($rawfile('ui/thinking_bubble1.png'))
              .width(92)
              .height(92)
              .position({ x: '62%', y: '25%' })
          }
        }
        .width('100%')
        .height(`${LIVE2D_HEIGHT_PCT}%`)

        // 消息列表
        List() {
          ForEach(this.messages, (m: ChatMessage) => {
            ListItem() {
              Row() {
                if (m.type === 'system') {
                  Text(m.content)
                    .fontSize(14)
                    .fontColor(this.getTheme().systemMessageText)
                    .width('100%')
                    .textAlign(TextAlign.Center)
                    .padding({ left: 24, right: 24, top: 5, bottom: 5 })
                } else if (m.isUser) {
                  Column() {
                    if (m.type === 'image') {
                      Image(m.content)
                        .width(180).height(180).borderRadius(BUBBLE_RADIUS)
                        .objectFit(ImageFit.Cover)
                        .margin({ left: BUBBLE_PADDING_H, top: BUBBLE_PADDING_V, bottom: BUBBLE_PADDING_V })
                    } else {
                      Text(m.content)
                        .fontSize(MSG_FONT_SIZE)
                        .padding({ left: BUBBLE_PADDING_H, right: BUBBLE_PADDING_H, top: BUBBLE_PADDING_V, bottom: BUBBLE_PADDING_V })
                        .borderRadius(BUBBLE_RADIUS)
                        .borderRadius({ topLeft: BUBBLE_RADIUS, topRight: BUBBLE_RADIUS, bottomLeft: BUBBLE_RADIUS, bottomRight: BUBBLE_TAIL_RADIUS })
                        .backgroundColor(this.getTheme().userBubble)
                        .fontColor(this.getTheme().userBubbleText)
                        .constraintSize({ maxWidth: BUBBLE_MAX_WIDTH })
                    }
                  }
                  .layoutWeight(1)
                  .alignItems(HorizontalAlign.End)
                } else {
                  Column() {
                    if (m.type === 'image') {
                      Image(m.content)
                        .width(180).height(180).borderRadius(BUBBLE_RADIUS)
                        .objectFit(ImageFit.Cover)
                        .margin({ right: BUBBLE_PADDING_H, top: BUBBLE_PADDING_V, bottom: BUBBLE_PADDING_V })
                    } else {
                      Text(m.content)
                        .fontSize(MSG_FONT_SIZE)
                        .padding({ left: BUBBLE_PADDING_H, right: BUBBLE_PADDING_H, top: BUBBLE_PADDING_V, bottom: BUBBLE_PADDING_V })
                        .borderRadius(BUBBLE_RADIUS)
                        .borderRadius({ topLeft: BUBBLE_RADIUS, topRight: BUBBLE_RADIUS, bottomLeft: BUBBLE_TAIL_RADIUS, bottomRight: BUBBLE_RADIUS })
                        .backgroundColor(this.getTheme().botBubble)
                        .fontColor(this.getTheme().bubbleText)
                        .constraintSize({ maxWidth: BUBBLE_MAX_WIDTH })
                    }
                  }
                  .layoutWeight(1)
                  .alignItems(HorizontalAlign.Start)
                }
              }
              .width('100%')
              .padding({ top: 4, bottom: 4 })
            }
          }, (m: ChatMessage) => m.uuid)
        }
        .width('100%')
        .layoutWeight(1)
        .padding({ top: 10, bottom: 10 })
        .backgroundColor(this.getTheme().chatList)

        // 输入栏（安卓 inputContainer：bg + borderTop）
        Row({ space: 0 }) {
          TextInput({
            placeholder: '给天依发消息...', text: this.inputText,
          })
            .layoutWeight(1)
            .height(this.inputHeight)
            .borderRadius(20)
            .backgroundColor(this.getTheme().inputBackground)
            .fontColor(this.getTheme().inputText)
            .placeholderColor(this.getTheme().placeholder)
            .margin({ right: 10, left: 5, top: 5, bottom: 5 })
            .onChange((v: string) => { this.inputText = v; })

          // 图片按钮（image_button_activate/un）
          Image(this.canSendImage() ? $rawfile('ui/image_button_activate.png') : $rawfile('ui/image_button_un.png'))
            .width(30).height(30)
            .margin({ left: 5, right: 5 })
            .onClick(() => { this.sendImage(); })

          // 发送按钮（send_button_activate/un）
          Image(this.canSend() ? $rawfile('ui/send_button_activate.png') : $rawfile('ui/send_button_un.png'))
            .width(30).height(30)
            .margin({ left: 5, right: 5 })
            .onClick(() => { this.sendMessage(); })
        }
        .width('100%')
        .padding({ bottom: 10 })
        .backgroundColor(this.getTheme().inputBar)
        .border({ width: { top: 1 }, color: this.getTheme().inputBorder })
      }
      .width('100%')
      .height('100%')

      // 菜单按钮（绝对定位左上）
      Image($rawfile('ui/menu.png'))
        .width(22).height(22)
        .position({ x: 10, y: 10 + 0 })
        .padding(7)
        .borderRadius(18)
        .backgroundColor('rgba(255,255,255,0.72)')
        .onClick(() => { this.toggleDrawer(); })

      // 调试面板（debugOpen 时覆盖层）
      if (this.debugOpen) {
        Column() {
          Row() {
            Text(`发送调试日志（最近 ${this.debugEntries.length}）`)
              .fontSize(12).fontWeight(FontWeight.Bold).fontColor(this.getTheme().debugHeader)
            Blank()
            Text('清空')
              .fontSize(12).fontColor(this.getTheme().debugAction)
              .onClick(() => { clearDebugTrace(); this.debugEntries = []; })
          }
          .width('100%').padding({ left: 10, right: 10, top: 8, bottom: 6 })

          List() {
            ForEach(this.debugEntries, (e: DebugTraceEntry) => {
              ListItem() {
                Text(`${formatDebugTs(e.ts)} [${e.scope}] ${e.message}${e.detail !== undefined ? ' ' + e.detail : ''}`)
                  .fontSize(11).fontColor(this.getTheme().debugText)
              }
            }, (e: DebugTraceEntry) => String(e.id) + e.message)
          }
          .width('100%').layoutWeight(1)
          .padding({ left: 10, right: 10 })
        }
        .width('100%')
        .height('90%')
        .position({ x: 8, y: 54 })
        .borderRadius(10)
        .backgroundColor(this.getTheme().debugBackground)
        .border({ width: 1, color: this.getTheme().debugBorder })
        .zIndex(70)
      }

      // 抽屉遮罩
      if (this.drawerOpen) {
        Column()
          .width('100%').height('100%')
          .backgroundColor('rgba(0,0,0,0.45)')
          .onClick(() => { this.drawerOpen = false; })
          .zIndex(90)
      }

      // 抽屉（左侧滑出，align 安卓 drawer）
      if (this.drawerOpen) {
        Column() {
          Text('菜单').fontSize(19).fontWeight(FontWeight.Bold).fontColor(this.getTheme().text)
          Text('洛天依 Agent').fontSize(12).fontColor(this.getTheme().textMuted).margin({ top: 3, bottom: 18 })

          // 调试
          Row() {
            Text(this.debugOpen ? '收起调试界面' : '打开调试界面')
              .fontSize(14).fontWeight(FontWeight.Medium).fontColor(this.getTheme().text)
          }
          .width('100%').height(42).borderRadius(8).padding({ left: 10 })
          .backgroundColor(this.getTheme().surfaceAlt).margin({ bottom: 10 })
          .onClick(() => { this.handleToggleDebug(); })

          // 偏好
          Row() {
            Text('配置偏好').fontSize(14).fontWeight(FontWeight.Medium).fontColor(this.getTheme().text)
          }
          .width('100%').height(42).borderRadius(8).padding({ left: 10 })
          .backgroundColor(this.getTheme().surfaceAlt).margin({ bottom: 10 })
          .onClick(() => { this.handleOpenPreferences(); })

          // LLM
          Row() {
            Text('LLM 模型设置').fontSize(14).fontWeight(FontWeight.Medium).fontColor(this.getTheme().text)
          }
          .width('100%').height(42).borderRadius(8).padding({ left: 10 })
          .backgroundColor(this.getTheme().surfaceAlt).margin({ bottom: 10 })
          .onClick(() => { this.handleOpenLlmSettings(); })

          // 动态 + 未读角标
          Row() {
            Text('动态').fontSize(14).fontWeight(FontWeight.Medium).fontColor(this.getTheme().text)
            Blank()
            if (this.hasDynamicsUnread) {
              Text(this.dynamicsUnreadCount > 99 ? '99+' : String(this.dynamicsUnreadCount))
                .fontSize(11).fontWeight(FontWeight.Bold)
                .fontColor(this.themeName === 'dark' ? '#0F1419' : '#ffffff')
                .padding({ left: 6, right: 6, top: 2, bottom: 2 })
                .borderRadius(11)
                .backgroundColor(this.getTheme().accent)
            }
          }
          .width('100%').height(42).borderRadius(8).padding({ left: 10, right: 10 })
          .backgroundColor(this.getTheme().surfaceAlt).margin({ bottom: 10 })
          .onClick(() => { this.handleOpenDynamics(); })

          // 颜色模式分区
          Column() {
            Text('颜色模式').fontSize(12).fontWeight(FontWeight.Bold).fontColor(this.getTheme().textMuted)
              .margin({ bottom: 8 })
            ForEach(COLOR_MODE_LABEL_LIST as ColorModeLabelExtra[], (item: ColorModeLabelExtra) => {
              Row() {
                Text(item.label)
                  .fontSize(13)
                  .fontColor(this.colorMode === item.name ? this.getTheme().accentText : this.getTheme().textSoft)
                  .fontWeight(this.colorMode === item.name ? FontWeight.Bold : FontWeight.Normal)
              }
              .width('100%').height(34).borderRadius(8).padding({ left: 10 })
              .margin({ bottom: 6 })
              .backgroundColor(this.colorMode === item.name ? this.getTheme().accentSoft : this.getTheme().surface)
              .border({ width: 1, color: this.colorMode === item.name ? this.getTheme().accent : this.getTheme().border })
              .onClick(() => { this.handleColorMode(item.name); })
            }, (item: ColorModeLabelExtra) => item.name)
          }
          .width('100%')
          .margin({ top: 2, bottom: 8 })
          .padding({ top: 10 })
          .border({ width: { top: 1 }, color: this.getTheme().border })

          Blank()

          // 登出
          Row() {
            Text('登出').fontSize(14).fontWeight(FontWeight.Medium).fontColor(this.getTheme().dangerText)
          }
          .width('100%').height(42).borderRadius(8).padding({ left: 10 })
          .backgroundColor(this.getTheme().dangerSurface)
          .onClick(() => { this.handleLogout(); })
        }
        .width(`${DRAWER_WIDTH_PCT}%`)
        .height('100%')
        .padding({ left: 12, right: 12, top: 18, bottom: 16 })
        .backgroundColor(this.getTheme().surface)
        .border({ width: { right: 1 }, color: this.getTheme().border })
        .align(Alignment.Start)
        .position({ x: 0, y: 0 })
        .zIndex(100)
      }

      // 偏好弹层
      if (this.showPreferences) {
        Column() {
          Row() {
            Text('配置偏好').fontSize(18).fontWeight(FontWeight.Bold).fontColor(this.getTheme().text)
            Blank()
            Text('✕').fontSize(18).fontColor(this.getTheme().textMuted)
              .onClick(() => { this.showPreferences = false; })
          }
          .width('100%').margin({ bottom: 16 })

          TextInput({ placeholder: '关系（如：朋友/恋人）', text: this.prefsRelationship })
            .fontColor(this.getTheme().inputText).placeholderColor(this.getTheme().placeholder)
            .backgroundColor(this.getTheme().inputBackground)
            .border({ width: 1, color: this.getTheme().inputBorder }).borderRadius(10)
            .margin({ top: 12 }).onChange((v: string) => { this.prefsRelationship = v; })
          TextInput({ placeholder: '说话风格', text: this.prefsSpeakingStyle })
            .fontColor(this.getTheme().inputText).placeholderColor(this.getTheme().placeholder)
            .backgroundColor(this.getTheme().inputBackground)
            .border({ width: 1, color: this.getTheme().inputBorder }).borderRadius(10)
            .margin({ top: 12 }).onChange((v: string) => { this.prefsSpeakingStyle = v; })
          TextInput({ placeholder: '人设补充文本', text: this.prefsPersonalityText })
            .fontColor(this.getTheme().inputText).placeholderColor(this.getTheme().placeholder)
            .backgroundColor(this.getTheme().inputBackground)
            .border({ width: 1, color: this.getTheme().inputBorder }).borderRadius(10)
            .margin({ top: 12 }).onChange((v: string) => { this.prefsPersonalityText = v; })
          TextArea({ placeholder: '自定义背景上下文', text: this.prefsCustomContext })
            .height(90)
            .fontColor(this.getTheme().inputText).placeholderColor(this.getTheme().placeholder)
            .backgroundColor(this.getTheme().inputBackground)
            .border({ width: 1, color: this.getTheme().inputBorder }).borderRadius(10)
            .margin({ top: 12 }).onChange((v: string) => { this.prefsCustomContext = v; })

          Button('保存偏好')
            .width('100%').height(44).margin({ top: 20 }).borderRadius(22)
            .backgroundColor(this.getTheme().accent).fontColor(this.getTheme().text)
            .onClick(() => { this.savePreferences(); })
        }
        .width('85%').padding(20).borderRadius(12)
        .backgroundColor(this.getTheme().surface)
        .position({ x: '7%', y: '25%' })
        .zIndex(110)
      }

      // LLM 设置弹层
      if (this.showLlmSettings) {
        LlmSettingsDialog({
          onClose: () => { this.showLlmSettings = false; },
        })
      }

      // 动态弹层
      if (this.showDynamics) {
        DynamicsDialog({
          onClose: () => {
            this.showDynamics = false;
            this.refreshDynamicsUnread();
          },
        })
      }
    }
    .width('100%')
    .height('100%')
    .backgroundColor(this.getTheme().root)
  }

  private canSend(): boolean {
    return this.inputText.trim().length > 0;
  }

  private canSendImage(): boolean {
    return true;
  }
}

/** 颜色模式标签（扩展接口，ArkTS 字面量约束） */
interface ColorModeLabelExtra {
  name: string;
  label: string;
}
