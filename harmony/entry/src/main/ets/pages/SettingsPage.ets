import { router, promptAction } from '@kit.ArkUI';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { AuthService } from '../service/AuthService';
import { SecurityStore } from '../service/SecurityStore';
import { PreferencesService, UserPreferences } from '../service/PreferencesService';

const TAG = 'SettingsPage';

@Entry
@Component
struct SettingsPage {
  @State serverUrl: string = '';
  @State relationship: string = '';
  @State speakingStyle: string = '';
  @State customContext: string = '';
  @State personalityText: string = '';
  @State loadingPrefs: boolean = true;
  @State savingPrefs: boolean = false;

  aboutToAppear(): void {
    this.loadServerUrl();
    this.loadPreferences();
  }

  private async loadServerUrl(): Promise<void> {
    this.serverUrl = await SecurityStore.getServerUrl();
  }

  private async loadPreferences(): Promise<void> {
    this.loadingPrefs = true;
    const prefs = await PreferencesService.get();
    if (prefs !== null) {
      this.relationship = prefs.relationship;
      this.speakingStyle = prefs.speaking_style;
      this.customContext = prefs.custom_context;
      this.personalityText = prefs.personality_text;
    }
    this.loadingPrefs = false;
  }

  private async savePreferences(): Promise<void> {
    if (this.savingPrefs) {
      return;
    }
    this.savingPrefs = true;
    const prefs: UserPreferences = {
      relationship: this.relationship,
      speaking_style: this.speakingStyle,
      personality_text: this.personalityText,
      personality_traits: [],
      custom_context: this.customContext,
    };
    const ok = await PreferencesService.overwrite(prefs);
    if (ok) {
      promptAction.showToast({ message: '偏好已保存' });
    } else {
      promptAction.showToast({ message: '保存失败' });
    }
    this.savingPrefs = false;
  }

  private async saveServerUrl(): Promise<void> {
    await SecurityStore.setServerUrl(this.serverUrl);
    AuthService.clearCachedPublicKey();
    promptAction.showToast({ message: '服务器地址已保存' });
  }

  private async logout(): Promise<void> {
    await AuthService.logout();
    router.replaceUrl({ url: 'pages/LoginPage' });
  }

  build() {
    Scroll() {
      Column({ space: 16 }) {
        Row() {
          Text('设置').fontSize(20).fontWeight(FontWeight.Bold)
          Blank()
          Button('返回').fontSize(13).onClick(() => {
            router.back();
          })
        }.width('100%')

        // 服务器地址
        Column({ space: 10 }) {
          Text('服务器地址').fontSize(15).fontWeight(FontWeight.Bold)
          TextInput({ placeholder: 'https://example.com', text: this.serverUrl })
            .onChange((v: string) => { this.serverUrl = v; })
          Button('保存服务器地址').backgroundColor('#007DFF').onClick(() => {
            this.saveServerUrl();
          })
        }
        .width('100%')
        .padding(12)
        .borderRadius(8)
        .backgroundColor('#FFFFFF')

        // LLM 模型设置入口
        Column() {
          Row() {
            Text('LLM 模型设置').fontSize(15)
            Blank()
            Button('前往').fontSize(13).onClick(() => {
              router.pushUrl({ url: 'pages/LlmSettingsPage' });
            })
          }.width('100%')
        }
        .width('100%')
        .padding(12)
        .borderRadius(8)
        .backgroundColor('#FFFFFF')

        // 偏好设置
        Column({ space: 10 }) {
          Text('相处模式偏好').fontSize(15).fontWeight(FontWeight.Bold)
          if (this.loadingPrefs) {
            Text('加载中...').fontSize(13).fontColor('#999999')
          } else {
            TextInput({ placeholder: '关系（如：朋友/恋人）', text: this.relationship })
              .onChange((v: string) => { this.relationship = v; })
            TextInput({ placeholder: '说话风格', text: this.speakingStyle })
              .onChange((v: string) => { this.speakingStyle = v; })
            TextInput({ placeholder: '人设补充文本', text: this.personalityText })
              .onChange((v: string) => { this.personalityText = v; })
            TextArea({ placeholder: '自定义背景上下文', text: this.customContext })
              .height(100)
              .onChange((v: string) => { this.customContext = v; })
            Button(this.savingPrefs ? '保存中...' : '保存偏好')
              .backgroundColor('#007DFF')
              .onClick(() => { this.savePreferences(); })
          }
        }
        .width('100%')
        .padding(12)
        .borderRadius(8)
        .backgroundColor('#FFFFFF')

        // 登出
        Button('退出登录')
          .backgroundColor('#E53935')
          .width('100%')
          .onClick(() => { this.logout(); })
      }
      .width('100%')
      .padding(16)
    }
    .width('100%')
    .height('100%')
    .backgroundColor('#F5F5F5')
  }
}
