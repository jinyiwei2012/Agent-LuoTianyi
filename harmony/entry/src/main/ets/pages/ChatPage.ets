import { router, promptAction } from '@kit.ArkUI';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { AuthService } from '../service/AuthService';
import { WsTransport, WsCallbacks, SendTouchPayload } from '../service/WsTransport';
import { Live2DFacade, Live2DEvents } from '../service/Live2DFacade';
import { LlmConfigStore } from '../service/LlmConfigStore';
import { ImagePicker } from '../service/ImagePicker';
import { HistoryService } from '../service/HistoryService';
import { AgentMessagePayload, ChatMessage, SendStatus, createSystemMessage } from '../model/ChatModels';

const TAG = 'ChatPage';

@Entry
@Component
struct ChatPage {
  @State messages: ChatMessage[] = [];
  @State inputText: string = '';
  @State agentState: string = '等待中';
  @State connectionText: string = '未连接';
  @State modelLoaded: string = '';
  private transport: WsTransport | null = null;
  private live2d: Live2DFacade | null = null;
  private pageBound = false;

  aboutToAppear(): void {
    if (!AuthService.state.isLoggedIn) {
      router.replaceUrl({ url: 'pages/LoginPage' });
      return;
    }
    const username = AuthService.state.username;
    const token = AuthService.state.messageToken;
    hilog.info(0x0000, TAG, 'chat page appear, user=%{public}s', username);

    const callbacks: WsCallbacks = {
      onAgentMessage: (payload: AgentMessagePayload) => {
        this.handleAgentMessage(payload);
      },
      onAgentStateChanged: (state: string) => {
        this.agentState = state === 'thinking' ? '思考中...' : (state === 'speaking' ? '说话中...' : state);
      },
      onError: (errorText: string) => {
        this.connectionText = errorText;
        this.pushSystemMessage(errorText);
      },
    };

    const live2dEvents: Live2DEvents = {
      onModelLoaded: (ok: boolean, error?: string) => {
        if (this.live2d === null) {
          return;
        }
        if (!this.pageBound && ok) {
          // 模型就绪后补绑定桥（页面首次 onPageEnd 已 bind，这里作为兜底）
          this.pageBound = true;
        }
        this.modelLoaded = ok ? '模型就绪' : `模型加载失败: ${error ?? ''}`;
      },
      onTouch: (touchArea: string, raw: string) => {
        this.reportTouch(touchArea, raw);
      },
      onAudioStopped: () => {
        hilog.info(0x0000, TAG, 'server audio stopped');
      },
      onAudioFinished: () => {
        hilog.info(0x0000, TAG, 'server audio finished');
      },
    };

    this.live2d = new Live2DFacade(live2dEvents);
    this.transport = new WsTransport(username, token, callbacks);
    // 上传 llm_mode：客户端托管的 LLM 类型（PR53 对齐）
    this.setupLlmMode();
    // 加载历史记录（最近 LOAD_HISTORY_COUNT 条）
    this.loadHistory();
    this.transport.start();
    this.connectionText = '连接中...';
    this.pushSystemMessage(`已登录：${username}`);
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

  aboutToDisappear(): void {
    if (this.transport !== null) {
      this.transport.stop();
      this.transport = null;
    }
    if (this.live2d !== null) {
      this.live2d.stopServerAudio();
      this.live2d = null;
    }
  }

  private pushSystemMessage(text: string): void {
    const m = createSystemMessage(text, `sys-${Date.now()}`);
    this.messages = [...this.messages, m];
  }

  /** agent_message 路由：文本展示 + 表情切换 + 音频分块投喂 + 幂等包处理 */
  private handleAgentMessage(payload: AgentMessagePayload): void {
    const uuid = payload.uuid ?? `agent-${Date.now()}`;
    const text = payload.text ?? '';
    const displayInChat = payload.display_in_chat !== false;

    // 文本展示路径
    if (displayInChat && text.length > 0 && payload.is_ephemeral !== true) {
      const m: ChatMessage = {
        uuid: uuid,
        type: 'text',
        content: text,
        isUser: false,
        timestamp: Date.now(),
        sendStatus: 'submitted',
        audioAvailable: payload.audio !== null && payload.audio !== undefined && payload.audio.length > 0,
        audioLocalUri: payload.audio ?? '',
        audioPlayState: 'idle',
      };
      this.messages = [...this.messages, m];
      hilog.info(0x0000, TAG, 'agent message: %{public}s', text.substr(0, 60));
    }

    // 表情路径
    if (payload.expression !== null && payload.expression !== undefined && this.live2d !== null) {
      this.live2d.setExpression(payload.expression);
    }

    // 音频路径：分块 base64 -> WebView JS 播放引擎；final 包结束流
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

  /** 触摸上报：user_touch（对齐 useChatLogic sendTouch） */
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
      hilog.warn(0x0000, TAG, 'touch parse failed: %{public}s', (e as Error).message);
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
      uuid: `u-${Date.now()}`,
      type: 'text',
      content: text,
      isUser: true,
      timestamp: Date.now(),
      sendStatus: 'waiting',
      audioAvailable: false,
      audioLocalUri: '',
      audioPlayState: 'idle',
    };
    this.messages = [...this.messages, m];
    this.inputText = '';
    this.transport.submitUserText(text, false, 10000, m.uuid)
      .then((result) => {
        hilog.info(0x0000, TAG, 'submitUserText ok=%{public}s', String(result.ok));
        this.updateLocalMessage(m.uuid, result.ok ? 'submitted' : 'failed');
      });
  }

  /** 选图并发送 user_image（对齐 NetworkClient.sendImage） */
  private async sendImage(): Promise<void> {
    if (this.transport === null) {
      return;
    }
    const picked = await ImagePicker.pickAndRead();
    if (picked === null) {
      promptAction.showToast({ message: '未选择图片' });
      return;
    }
    const uuid = `img-${Date.now()}`;
    const m: ChatMessage = {
      uuid: uuid,
      type: 'image',
      content: picked.uri,
      isUser: true,
      timestamp: Date.now(),
      sendStatus: 'waiting',
      audioAvailable: false,
      audioLocalUri: '',
      audioPlayState: 'idle',
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
          uuid: m.uuid,
          type: m.type,
          content: m.content,
          isUser: m.isUser,
          timestamp: m.timestamp,
          sendStatus: status,
          audioAvailable: m.audioAvailable,
          audioLocalUri: m.audioLocalUri,
          audioPlayState: m.audioPlayState,
        };
        return updated;
      }
      return m;
    });
  }

  build() {
    Column() {
      // 顶栏
      Row() {
        Text('洛天依').fontSize(18).fontWeight(FontWeight.Bold)
        Blank()
        if (this.modelLoaded.length > 0) {
          Text(this.modelLoaded).fontSize(12).fontColor('#999999').margin({ right: 8 })
        }
        Text(this.agentState).fontSize(12).fontColor('#999999').margin({ right: 8 })
        Text(this.connectionText).fontSize(12).fontColor(
          this.connectionText === '未连接' ? '#E53935' : '#4CAF50')
        Text('设置')
          .fontSize(13)
          .fontColor('#007DFF')
          .margin({ left: 8 })
          .onClick(() => {
            router.pushUrl({ url: 'pages/SettingsPage' });
          })
      }
      .width('100%')
      .height(48)
      .padding({ left: 16, right: 16 })

      // Live2D 区域（WebView，模型 + 服务端音频播放引擎）
      if (this.live2d !== null) {
        Web({ src: $rawfile('live2d/live2d.html'), controller: this.live2d.controller })
          .width('100%')
          .height('42%')
          .javaScriptAccess(true)
          .domStorageAccess(true)
          .backgroundColor(Color.Transparent)
          .onPageEnd(() => {
            this.live2d?.bindBridge();
            this.pageBound = true;
            this.live2d?.debugLog('chapter page onPageEnd');
          })
          .onErrorReceive((event) => {
            hilog.error(0x0000, TAG, 'webview error: %{public}s', String(event.error));
          })
      }

      // 消息列表
      List() {
        ForEach(this.messages, (m: ChatMessage) => {
          ListItem() {
            Row() {
              if (m.isUser) {
                Blank().width(60)
                Column() {
                  if (m.type === 'image') {
                    Image(m.content)
                      .width(180)
                      .height(180)
                      .borderRadius(12)
                      .objectFit(ImageFit.Cover)
                  } else {
                    Text(m.content)
                      .fontSize(15)
                      .padding(12)
                      .borderRadius(12)
                      .backgroundColor('#007DFF')
                      .fontColor(Color.White)
                  }
                }.alignItems(HorizontalAlign.End)
              } else {
                Column() {
                  Text(m.content)
                    .fontSize(15)
                    .padding(12)
                    .borderRadius(12)
                    .backgroundColor('#F2F2F2')
                    .fontColor('#333333')
                }.alignItems(HorizontalAlign.Start)
                Blank().width(60)
              }
            }
            .width('100%')
            .padding({ top: 4, bottom: 4 })
          }
        }, (m: ChatMessage) => m.uuid)
      }
      .width('100%')
      .layoutWeight(1)
      .padding({ left: 12, right: 12 })

      // 输入区
      Row({ space: 8 }) {
        TextInput({ placeholder: '对洛天依说点什么...', text: this.inputText })
          .layoutWeight(1)
          .height(44)
          .borderRadius(22)
          .backgroundColor('#F2F2F2')
          .onChange((v: string) => {
            this.inputText = v;
          })
        Button('图片')
          .backgroundColor('#90A4AE')
          .height(44)
          .onClick(() => {
            this.sendImage();
          })
        Button('发送')
          .backgroundColor('#007DFF')
          .height(44)
          .onClick(() => {
            this.sendMessage();
          })
      }
      .width('100%')
      .padding({ left: 12, right: 12, top: 8, bottom: 16 })
    }
    .width('100%')
    .height('100%')
  }
}
