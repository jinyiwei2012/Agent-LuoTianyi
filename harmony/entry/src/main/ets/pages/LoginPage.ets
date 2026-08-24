import { router } from '@kit.ArkUI';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { AuthService } from '../service/AuthService';
import { SecurityStore } from '../service/SecurityStore';
import { Theme } from '../common/Theme';

const TAG = 'LoginPage';

@Entry
@Component
struct LoginPage {
  @State activeTab: string = 'login';
  @State username: string = '';
  @State password: string = '';
  @State confirmPassword: string = '';
  @State inviteCode: string = '';
  @State autoLogin: boolean = false;
  @State loading: boolean = false;
  @State message: string = '';
  @State showServerDialog: boolean = false;
  @State serverUrl: string = '';

  aboutToAppear(): void {
    this.loadServerUrl();
    // 尝试自动登录
    this.tryAutoLoginFlow();
  }

  private async loadServerUrl(): Promise<void> {
    const url = await SecurityStore.getServerUrl();
    this.serverUrl = url;
  }

  private async tryAutoLoginFlow(): Promise<void> {
    const ok = await AuthService.tryAutoLogin();
    if (ok) {
      hilog.info(0x0000, TAG, 'auto login -> chat page');
      router.replaceUrl({ url: 'pages/ChatPage' });
    }
  }

  private async handleLogin(): Promise<void> {
    if (this.loading) {
      return;
    }
    this.message = '';
    this.loading = true;
    try {
      const result = await AuthService.login(this.username, this.password, this.autoLogin);
      if (result.success) {
        router.replaceUrl({ url: 'pages/ChatPage' });
      } else {
        this.message = result.message;
      }
    } finally {
      this.loading = false;
    }
  }

  private async handleRegister(): Promise<void> {
    if (this.loading) {
      return;
    }
    this.message = '';
    this.loading = true;
    try {
      const result = await AuthService.register(
        this.username, this.password, this.confirmPassword, this.inviteCode);
      this.message = result.message;
      if (result.success) {
        this.activeTab = 'login';
        this.password = '';
        this.confirmPassword = '';
        this.inviteCode = '';
      }
    } finally {
      this.loading = false;
    }
  }

  private async saveServerUrl(): Promise<void> {
    await SecurityStore.setServerUrl(this.serverUrl);
    AuthService.clearCachedPublicKey();
    this.showServerDialog = false;
    this.message = '服务器地址已保存';
  }

  @Builder
  tabButton(label: string, tab: string) {
    Text(label)
      .fontSize(16)
      .fontWeight(this.activeTab === tab ? FontWeight.Bold : FontWeight.Normal)
      .fontColor(this.activeTab === tab ? Theme.accentText : Theme.textMuted)
      .padding({ left: 20, right: 20, top: 10, bottom: 10 })
      .onClick(() => {
        this.activeTab = tab;
        this.message = '';
      })
  }

  @Builder
  inputField(placeholder: string, value: string, isPassword: boolean, onValue: (v: string) => void) {
    TextInput({ placeholder: placeholder, text: value })
      .type(isPassword ? InputType.Password : InputType.Normal)
      .width('100%')
      .height(48)
      .borderRadius(10)
      .backgroundColor(Theme.inputBackground)
      .fontColor(Theme.inputText)
      .placeholderColor(Theme.placeholder)
      .border({ width: 1, color: Theme.inputBorder })
      .margin({ top: 12 })
      .onChange((v: string) => {
        onValue(v);
      })
  }

  build() {
    Stack() {
      Column() {
        // 标题
        Text('洛天依')
          .fontSize(36)
          .fontWeight(FontWeight.Bold)
          .fontColor(Theme.accent)
          .margin({ top: 80, bottom: 8 })
        Text('与天依的每一句话')
          .fontSize(14)
          .fontColor(Theme.textMuted)
          .margin({ bottom: 40 })

        // Tab 栏
        Row() {
          this.tabButton('登录', 'login')
          this.tabButton('注册', 'register')
        }
        .padding(4)
        .borderRadius(12)
        .backgroundColor(Theme.surfaceAlt)
        .margin({ bottom: 20 })

        // 表单
        Column() {
          this.inputField('用户名', this.username, false, (v: string) => {
            this.username = v;
          })
          this.inputField('密码', this.password, true, (v: string) => {
            this.password = v;
          })
          if (this.activeTab === 'register') {
            this.inputField('确认密码', this.confirmPassword, true, (v: string) => {
              this.confirmPassword = v;
            })
            this.inputField('邀请码', this.inviteCode, false, (v: string) => {
              this.inviteCode = v;
            })
          }
        }.width('85%')

        // 自动登录（仅登录 tab）
        if (this.activeTab === 'login') {
          Row() {
            Toggle({ type: ToggleType.Switch, isOn: this.autoLogin })
              .selectedColor(Theme.accent)
              .onChange((isOn: boolean) => {
                this.autoLogin = isOn;
              })
            Text('自动登录').fontSize(14).fontColor(Theme.textMuted).margin({ left: 8 })
          }.width('85%').margin({ top: 14 })
        }

        // 主按钮
        Button(this.loading ? '请稍候...' : (this.activeTab === 'login' ? '登录' : '注册'))
          .width('85%')
          .height(48)
          .margin({ top: 24 })
          .borderRadius(24)
          .backgroundColor(Theme.accent)
          .fontColor(Theme.text)
          .onClick(() => {
            if (this.activeTab === 'login') {
              this.handleLogin();
            } else {
              this.handleRegister();
            }
          })

        // 提示信息
        if (this.message.length > 0) {
          Text(this.message)
            .fontSize(13)
            .fontColor(Theme.dangerText)
            .margin({ top: 16 })
            .textAlign(TextAlign.Center)
        }

        // 服务器设置入口
        Text('服务器设置')
          .fontSize(13)
          .fontColor(Theme.accent)
          .margin({ top: 40 })
          .onClick(() => {
            this.serverUrl = '';
            this.loadServerUrl().then(() => {
              this.showServerDialog = true;
            });
          })
      }
      .width('100%')
      .height('100%')
      .backgroundColor(Theme.root)

      // 服务器地址弹窗
      if (this.showServerDialog) {
        Column() {
          Text('服务器地址').fontSize(16).fontWeight(FontWeight.Bold).fontColor(Theme.text)
          TextInput({ placeholder: 'https://example.com', text: this.serverUrl })
            .type(InputType.Normal)
            .fontColor(Theme.inputText)
            .placeholderColor(Theme.placeholder)
            .backgroundColor(Theme.inputBackground)
            .border({ width: 1, color: Theme.inputBorder })
            .borderRadius(10)
            .onChange((v: string) => {
              this.serverUrl = v;
            })
            .margin({ top: 16 })
          Row({ space: 12 }) {
            Button('取消').backgroundColor(Theme.surfaceAlt).fontColor(Theme.textMuted).onClick(() => {
              this.showServerDialog = false;
            })
            Button('保存').backgroundColor(Theme.accent).fontColor(Theme.text).onClick(() => {
              this.saveServerUrl();
            })
          }.margin({ top: 20 })
        }
        .width('80%')
        .padding(20)
        .borderRadius(12)
        .backgroundColor(Theme.surface)
      }
    }
    .width('100%')
    .height('100%')
  }
}
