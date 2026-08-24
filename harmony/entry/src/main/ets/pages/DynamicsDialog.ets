import { promptAction } from '@kit.ArkUI';
import { DynamicsService, DynamicPost, getDynamicSourceLabel } from '../service/DynamicsService';
import { AppTheme, THEME_LIGHT, THEME_DARK } from '../common/Theme';

const TAG = 'DynamicsDialog';

@Component
export struct DynamicsDialog {
  onClose: () => void = () => {
  };
  @State themeName: string = 'light';
  @State posts: DynamicPost[] = [];
  @State loading: boolean = true;
  @State inputContent: string = '';
  @State submitting: boolean = false;

  private theme(): AppTheme {
    return this.themeName === 'dark' ? THEME_DARK : THEME_LIGHT;
  }

  aboutToAppear(): void {
    this.load();
  }

  private async load(): Promise<void> {
    this.loading = true;
    const result = await DynamicsService.getDynamics(20, null);
    this.posts = result.items;
    this.loading = false;
  }

  private async publish(): Promise<void> {
    const content = this.inputContent.trim();
    if (content.length === 0 || this.submitting) {
      return;
    }
    this.submitting = true;
    const post = await DynamicsService.createDynamic(content);
    this.submitting = false;
    if (post !== null) {
      this.inputContent = '';
      this.posts = [post, ...this.posts];
      promptAction.showToast({ message: '已发布' });
    } else {
      promptAction.showToast({ message: '发布失败' });
    }
  }

  private formatTime(ts: string | null): string {
    if (ts === null || ts.length === 0) {
      return '';
    }
    return ts.length >= 10 ? ts.substring(0, 10) : ts;
  }

  build() {
    Stack() {
      Column()
        .width('100%').height('100%').backgroundColor('rgba(0,0,0,0.45)')
        .onClick(() => { this.onClose(); })

      Column() {
        Row() {
          Text('动态').fontSize(18).fontWeight(FontWeight.Bold).fontColor(this.theme().text)
          Blank()
          Text('✕').fontSize(18).fontColor(this.theme().textMuted).onClick(() => { this.onClose(); })
        }
        .width('100%').margin({ bottom: 12 })

        // 发布区
        Row({ space: 8 }) {
          TextInput({ placeholder: '分享一条生活动态...', text: this.inputContent })
            .layoutWeight(1).height(40)
            .fontColor(this.theme().inputText).placeholderColor(this.theme().placeholder)
            .backgroundColor(this.theme().inputBackground)
            .border({ width: 1, color: this.theme().inputBorder }).borderRadius(20)
            .onChange((v: string) => { this.inputContent = v; })
          Button('发布')
            .height(40).borderRadius(20)
            .backgroundColor(this.theme().accent).fontColor(this.theme().text)
            .onClick(() => { this.publish(); })
        }
        .width('100%').margin({ bottom: 12 })

        if (this.loading) {
          Text('加载中...').fontSize(13).fontColor(this.theme().textMuted)
        } else if (this.posts.length === 0) {
          Text('暂无动态').fontSize(13).fontColor(this.theme().textMuted).margin({ top: 24 })
        } else {
          List() {
            ForEach(this.posts, (post: DynamicPost) => {
              ListItem() {
                Column({ space: 6 }) {
                  Row() {
                    Text(post.author_name).fontSize(14).fontWeight(FontWeight.Bold).fontColor(this.theme().text)
                    Blank()
                    Text(getDynamicSourceLabel(post.source_type))
                      .fontSize(11).fontColor(this.theme().accentText)
                      .padding({ left: 6, right: 6, top: 2, bottom: 2 })
                      .borderRadius(6)
                      .backgroundColor(this.theme().accentSoft)
                  }.width('100%')
                  Text(post.content).fontSize(13).fontColor(this.theme().text).width('100%')
                  Text(this.formatTime(post.created_at)).fontSize(11).fontColor(this.theme().textMuted)
                }
                .width('100%').padding(10).borderRadius(8)
                .backgroundColor(this.theme().surfaceAlt).margin({ bottom: 8 })
              }
            }, (post: DynamicPost) => post.id)
          }
          .layoutWeight(1)
        }
      }
      .width('88%').height('82%').padding(16).borderRadius(12)
      .backgroundColor(this.theme().surface)
      .position({ x: '6%', y: '9%' })
      .zIndex(5)
    }
    .width('100%').height('100%')
    .backgroundColor('rgba(0,0,0,0.1)')
  }
}
