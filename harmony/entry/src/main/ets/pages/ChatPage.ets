import { router } from '@kit.ArkUI';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { AuthService } from '../service/AuthService';
import { WsTransport, WsCallbacks, SendTextPayload } from '../service/WsTransport';
import { AgentMessagePayload, ChatMessage, SendStatus, createSystemMessage } from '../model/ChatModels';

const TAG = 'ChatPage';

@Entry
@Component
struct ChatPage {
  @State messages: ChatMessage[] = [];
  @State inputText: string = '';
  @State agentState: string = '等待中';
  @State connectionText: string = '未连接';
  private transport: WsTransport | null = null;

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
        this.agentState = state;
      },
      onError: (errorText: string) => {
        this.connectionText = errorText;
        this.pushSystemMessage(errorText);
      },
    };

    this.transport = new WsTransport(username, token, callbacks);
    this.transport.start();
    this.connectionText = '连接中...';
    this.pushSystemMessage(`已登录：${username}`);
  }

  aboutToDisappear(): void {
    if (this.transport !== null) {
      this.transport.stop();
      this.transport = null;
    }
  }

  private pushSystemMessage(text: string): void {
    const m = createSystemMessage(text, `sys-${Date.now()}`);
    this.messages = [...this.messages, m];
  }

  private handleAgentMessage(payload: AgentMessagePayload): void {
    const text = payload.text ?? '';
    const uuid = payload.uuid ?? `agent-${Date.now()}`;
    if (payload.is_ephemeral === true) {
      hilog.info(0x0000, TAG, 'ephemeral message, skip display: %{public}s', uuid);
      return;
    }
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
    const textPayload: SendTextPayload = { text: text };
    this.transport.sendWithAck('user_text', textPayload, 10000, m.uuid)
      .then((result) => {
        hilog.info(0x0000, TAG, 'sendWithAck ok=%{public}s', String(result.ok));
        this.updateLocalMessage(m.uuid, result.ok ? 'submitted' : 'failed');
      });
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
        Text(this.agentState).fontSize(12).fontColor('#999999').margin({ right: 8 })
        Text(this.connectionText).fontSize(12).fontColor(
          this.connectionText === '未连接' ? '#E53935' : '#4CAF50')
      }
      .width('100%')
      .height(48)
      .padding({ left: 16, right: 16 })

      // 消息列表
      List() {
        ForEach(this.messages, (m: ChatMessage) => {
          ListItem() {
            Row() {
              if (m.isUser) {
                Blank().width(60)
                Column() {
                  Text(m.content)
                    .fontSize(15)
                    .padding(12)
                    .borderRadius(12)
                    .backgroundColor('#007DFF')
                    .fontColor(Color.White)
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
