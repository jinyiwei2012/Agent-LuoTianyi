import { promptAction } from '@kit.ArkUI';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { SecurityStore } from '../service/SecurityStore';
import { LlmConfigStore, LlmModuleConfig, LlmCapabilities } from '../service/LlmConfigStore';
import { LlmProviderService, ModelType } from '../service/LlmProviderService';
import { AppTheme, THEME_LIGHT, THEME_DARK } from '../common/Theme';

const TAG = 'LlmSettingsDialog';

@Component
export struct LlmSettingsDialog {
  onClose: () => void = () => {
  };
  @State themeName: string = 'light';
  @State types: ModelType[] = [];
  @State configs: Record<string, Object> = {};
  @State loading: boolean = true;
  @State error: string = '';
  @State savedTip: string = '';

  aboutToAppear(): void {
    this.loadAll();
  }

  private theme(): AppTheme {
    return this.themeName === 'dark' ? THEME_DARK : THEME_LIGHT;
  }

  private async loadAll(): Promise<void> {
    this.loading = true;
    this.error = '';
    const serverUrl = await SecurityStore.getServerUrl();
    const saved = await LlmConfigStore.getAll();
    this.configs = saved;
    try {
      const response = await LlmProviderService.fetchModelTypes(serverUrl);
      this.types = response.types;
    } catch (e) {
      this.error = `加载模型类型失败: ${(e as Error).message}`;
    }
    this.loading = false;
  }

  private handleChange(moduleId: string, cfg: LlmModuleConfig): void {
    const next: Record<string, Object> = {};
    for (const k of Object.keys(this.configs)) {
      next[k] = this.configs[k];
    }
    next[moduleId] = cfg;
    this.configs = next;
  }

  private async handleSave(): Promise<void> {
    await LlmConfigStore.saveAll(this.configs);
    this.savedTip = '已保存（约 1-2 分钟后随新会话生效）';
    promptAction.showToast({ message: 'LLM 配置已保存' });
  }

  @Builder
  moduleCard(item: ModelType) {
    Column({ space: 8 }) {
      Row() {
        Text(item.type)
          .fontSize(13).fontWeight(FontWeight.Bold).fontColor(this.theme().text)
        Blank()
      }.width('100%')
      TextInput({ placeholder: 'Provider', text: item.type })
        .fontSize(12).fontColor(this.theme().text).placeholderColor(this.theme().placeholder)
        .backgroundColor(this.theme().inputBackground)
        .border({ width: 1, color: this.theme().inputBorder }).borderRadius(8)
        .height(40)
    }
    .width('100%').padding(10).borderRadius(8)
    .backgroundColor(this.theme().surface).margin({ bottom: 10 })
  }

  build() {
    Stack() {
      Column()
        .width('100%').height('100%').backgroundColor('rgba(0,0,0,0.45)')
        .onClick(() => { this.onClose(); })

      Column() {
        Row() {
          Text('LLM 模型设置').fontSize(18).fontWeight(FontWeight.Bold).fontColor(this.theme().text)
          Blank()
          Text('✕').fontSize(18).fontColor(this.theme().textMuted).onClick(() => { this.onClose(); })
        }
        .width('100%').margin({ bottom: 12 })

        if (this.loading) {
          Text('加载中...').fontSize(13).fontColor(this.theme().textMuted)
        } else if (this.error.length > 0) {
          Text(this.error).fontSize(13).fontColor(this.theme().dangerText).padding(12)
        } else {
          Scroll() {
            Column() {
              ForEach(this.types, (item: ModelType) => {
                this.moduleCard(item)
              }, (item: ModelType) => item.type)
              Button('保存全部配置')
                .width('90%').height(44).margin({ top: 12 })
                .backgroundColor(this.theme().accent).fontColor(this.theme().text)
                .onClick(() => { this.handleSave(); })
              if (this.savedTip.length > 0) {
                Text(this.savedTip).fontSize(12).fontColor(this.theme().textMuted).margin({ top: 10 })
              }
            }
          }
          .layoutWeight(1)
        }
      }
      .width('90%').height('82%').padding(16).borderRadius(12)
      .backgroundColor(this.theme().surface)
      .position({ x: '5%', y: '9%' })
      .zIndex(5)
    }
    .width('100%').height('100%')
    .backgroundColor('rgba(0,0,0,0.1)')
  }
}
