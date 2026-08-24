import { router, promptAction } from '@kit.ArkUI';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { SecurityStore } from '../service/SecurityStore';
import { LlmConfigStore, LlmModuleConfig, LlmCapabilities } from '../service/LlmConfigStore';
import { LlmProviderService, ModelType, ModelProvider, ProviderModel } from '../service/LlmProviderService';
import { Theme } from '../common/Theme';

const TAG = 'LlmSettingsPage';

/** 页面渲染的编辑项 */
@Component
struct LlmModuleForm {
  moduleId: string = '';
  onChange: (moduleId: string, config: LlmModuleConfig) => void = () => {
  };
  @Prop modelType: ModelType | null = null;
  @Prop initializedConfig: LlmModuleConfig | null = null;
  @State moduleEnabled: boolean = false;
  @State providerName: string = '';
  @State baseUrl: string = '';
  @State model: string = '';
  @State apiKey: string = '';
  @State paramsText: string = '';
  @State canThinking: boolean = false;
  @State canJson: boolean = false;
  @State probing: boolean = false;
  @State probeResult: string = '';

  aboutToAppear(): void {
    const c = this.initializedConfig;
    if (c !== null) {
      this.moduleEnabled = c.enabled;
      this.providerName = c.provider;
      this.baseUrl = c.baseUrl;
      this.model = c.model;
      this.apiKey = c.apiKey;
      this.paramsText = c.paramsText;
      this.canThinking = c.modelCapabilities.can_enable_thinking;
      this.canJson = c.modelCapabilities.can_use_json;
    } else if (this.modelType !== null && this.modelType.providers.length > 0) {
      const firstProvider = this.modelType.providers[0];
      this.providerName = firstProvider.name;
      this.baseUrl = firstProvider.base_url;
      this.model = firstProvider.models.length > 0 ? firstProvider.models[0].id : '';
      this.canThinking = firstProvider.models.length > 0 ? firstProvider.models[0].can_enable_thinking : false;
      this.canJson = firstProvider.models.length > 0 ? firstProvider.models[0].can_use_json : false;
    }
  }

  private emit(): void {
    const caps: LlmCapabilities = {
      can_enable_thinking: this.canThinking,
      can_use_json: this.canJson,
    };
    const cfg: LlmModuleConfig = {
      enabled: this.moduleEnabled,
      provider: this.providerName,
      model: this.model,
      baseUrl: this.baseUrl,
      apiKey: this.apiKey,
      paramsText: this.paramsText,
      modelCapabilities: caps,
    };
    this.onChange(this.moduleId, cfg);
  }

  private async probe(): Promise<void> {
    if (this.baseUrl.trim().length === 0 || this.apiKey.trim().length === 0) {
      this.probeResult = '请先填写 baseUrl 和 apiKey';
      return;
    }
    this.probing = true;
    this.probeResult = '探测中...';
    try {
      const models = await LlmProviderService.fetchModelsList(this.baseUrl, this.apiKey);
      this.probeResult = `成功，可用模型 ${models.length} 个`;
    } catch (e) {
      this.probeResult = `探测失败: ${(e as Error).message}`;
    } finally {
      this.probing = false;
    }
  }

  build() {
    Column({ space: 8 }) {
      Row() {
        Text(this.modelType !== null && this.modelType.description.length > 0
          ? this.modelType.description : this.moduleId)
          .fontSize(15)
          .fontWeight(FontWeight.Bold)
        Blank()
        Toggle({ type: ToggleType.Switch, isOn: this.moduleEnabled })
          .onChange((isOn: boolean) => {
            this.moduleEnabled = isOn;
            this.emit();
          })
      }.width('100%')

      TextInput({ placeholder: 'Provider 名称', text: this.providerName })
        .onChange((v: string) => { this.providerName = v; this.emit(); })

      TextInput({ placeholder: 'Base URL (https://api.example.com)', text: this.baseUrl })
        .onChange((v: string) => { this.baseUrl = v; this.emit(); })

      TextInput({ placeholder: '模型 ID (如 qwen-plus)', text: this.model })
        .onChange((v: string) => { this.model = v; this.emit(); })

      TextInput({ placeholder: 'API Key', text: this.apiKey })
        .type(InputType.Password)
        .onChange((v: string) => { this.apiKey = v; this.emit(); })

      TextInput({ placeholder: '参数 JSON (可选)', text: this.paramsText })
        .onChange((v: string) => { this.paramsText = v; this.emit(); })

      Row({ space: 12 }) {
        Checkbox({ name: 'thinking', group: `th-${this.moduleId}` })
          .select(this.canThinking)
          .onChange((v: boolean) => { this.canThinking = v; this.emit(); })
        Text('支持 thinking').fontSize(13)
        Checkbox({ name: 'json', group: `js-${this.moduleId}` })
          .select(this.canJson)
          .onChange((v: boolean) => { this.canJson = v; this.emit(); })
        Text('支持 JSON').fontSize(13)
        Blank()
        Button(this.probing ? '探测中...' : '探测')
          .height(32)
          .fontSize(13)
          .onClick(() => { this.probe(); })
      }.width('100%')

      if (this.probeResult.length > 0) {
        Text(this.probeResult).fontSize(12).fontColor(Theme.textMuted)
      }
    }
    .width('100%')
    .padding(12)
    .borderRadius(8)
    .backgroundColor(Theme.surface)
    .margin({ bottom: 12 })
  }
}

@Entry
@Component
struct LlmSettingsPage {
  @State types: ModelType[] = [];
  @State configs: Record<string, Object> = {};
  @State loading: boolean = true;
  @State error: string = '';
  @State savedTip: string = '';

  aboutToAppear(): void {
    this.loadAll();
  }

  private async loadAll(): Promise<void> {
    this.loading = true;
    this.error = '';
    this.savedTip = '';
    const serverUrl = await SecurityStore.getServerUrl();
    const saved = await LlmConfigStore.getAll();
    this.configs = saved;
    try {
      const response = await LlmProviderService.fetchModelTypes(serverUrl);
      this.types = response.types;
      this.error = '';
    } catch (e) {
      this.error = `加载模型类型失败: ${(e as Error).message}`;
    } finally {
      this.loading = false;
    }
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
    hilog.info(0x0000, TAG, 'saved llm configs');
  }

  @Builder
  moduleCard(item: ModelType) {
    LlmModuleForm({
      moduleId: item.type,
      modelType: item,
      initializedConfig: this.configs[item.type] !== undefined
        ? this.configs[item.type] as LlmModuleConfig
        : null,
      onChange: (moduleId: string, cfg: LlmModuleConfig) => {
        this.handleChange(moduleId, cfg);
      },
    })
  }

  build() {
    Column() {
      Row() {
        Text('LLM 模型设置').fontSize(18).fontWeight(FontWeight.Bold)
        Blank()
        Button('返回').fontSize(13).onClick(() => {
          router.back();
        })
      }
      .width('100%')
      .height(48)
      .padding({ left: 16, right: 16 })

      if (this.loading) {
        Text('加载中...').margin({ top: 40 }).fontColor(Theme.textMuted)
      } else if (this.error.length > 0) {
        Text(this.error).fontColor(Theme.dangerText).margin({ top: 24 }).padding(16)
        Button('重试').backgroundColor(Theme.accentSoft).fontColor(Theme.accentText).onClick(() => { this.loadAll(); }).margin({ top: 12 })
      } else {
        Scroll() {
          Column() {
            ForEach(this.types, (item: ModelType) => {
              this.moduleCard(item)
            }, (item: ModelType) => item.type)

            Button('保存全部配置')
              .width('90%')
              .height(46)
              .margin({ top: 16 })
              .borderRadius(23)
              .backgroundColor(Theme.accent)
              .fontColor(Theme.text)
              .onClick(() => { this.handleSave(); })

            if (this.savedTip.length > 0) {
              Text(this.savedTip).fontSize(12).fontColor(Theme.textMuted).margin({ top: 12 })
            }
          }
          .width('100%')
          .padding(16)
        }
        .layoutWeight(1)
        .align(Alignment.Top)
      }
    }
    .width('100%')
    .height('100%')
    .backgroundColor(Theme.chatList)
  }
}
