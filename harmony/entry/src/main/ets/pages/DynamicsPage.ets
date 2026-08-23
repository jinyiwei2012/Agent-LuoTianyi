import { router, promptAction } from '@kit.ArkUI';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { DynamicsService, DynamicPost, DynamicComment, getDynamicSourceLabel } from '../service/DynamicsService';

const TAG = 'DynamicsPage';

@Entry
@Component
struct DynamicsPage {
  @State posts: DynamicPost[] = [];
  @State loading: boolean = true;
  @State error: string = '';
  @State inputContent: string = '';
  @State submitting: boolean = false;

  // 当前展开评论的动态 id
  @State expandedPostId: string = '';
  @State comments: DynamicComment[] = [];
  @State commentInput: string = '';
  @State commentsLoading: boolean = false;

  aboutToAppear(): void {
    this.load();
  }

  private async load(): Promise<void> {
    this.loading = true;
    this.error = '';
    const result = await DynamicsService.getDynamics(20, null);
    this.posts = result.items;
    this.loading = false;
    hilog.info(0x0000, TAG, 'loaded %{public}d posts', this.posts.length);
  }

  private async publish(): Promise<void> {
    const content = this.inputContent.trim();
    if (content.length === 0) {
      promptAction.showToast({ message: '内容不能为空' });
      return;
    }
    if (this.submitting) {
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

  private async toggleComments(postId: string): Promise<void> {
    if (this.expandedPostId === postId) {
      this.expandedPostId = '';
      this.comments = [];
      return;
    }
    this.expandedPostId = postId;
    this.commentsLoading = true;
    this.comments = [];
    this.comments = await DynamicsService.getComments(postId);
    this.commentsLoading = false;
  }

  private async submitComment(): Promise<void> {
    const content = this.commentInput.trim();
    if (this.expandedPostId.length === 0 || content.length === 0) {
      return;
    }
    const comment = await DynamicsService.createComment(this.expandedPostId, content);
    if (comment !== null) {
      this.commentInput = '';
      this.comments = [...this.comments, comment];
      // 增加评论数
      this.posts = this.posts.map((p: DynamicPost) => {
        if (p.id === this.expandedPostId) {
          const np: DynamicPost = {
            id: p.id,
            author_type: p.author_type,
            author_id: p.author_id,
            author_name: p.author_name,
            owner_user_id: p.owner_user_id,
            visibility: p.visibility,
            content: p.content,
            source_type: p.source_type,
            allow_comment: p.allow_comment,
            comment_count: p.comment_count + 1,
            created_at: p.created_at,
          };
          return np;
        }
        return p;
      });
    }
  }

  private formatTime(ts: string | null): string {
    if (ts === null || ts.length === 0) {
      return '';
    }
    // 取日期部分（ISO 时间）
    return ts.length >= 10 ? ts.substring(0, 10) : ts;
  }

  build() {
    Column() {
      Row() {
        Text('动态').fontSize(20).fontWeight(FontWeight.Bold)
        Blank()
        Button('返回').fontSize(13).onClick(() => {
          router.back();
        })
      }
      .width('100%')
      .height(48)
      .padding({ left: 16, right: 16 })

      // 发布区
      Column({ space: 8 }) {
        TextArea({ placeholder: '分享一条生活动态...', text: this.inputContent })
          .height(80)
          .onChange((v: string) => { this.inputContent = v; })
        Button(this.submitting ? '发布中...' : '发布')
          .backgroundColor('#007DFF')
          .alignSelf(ItemAlign.End)
          .onClick(() => { this.publish(); })
      }
      .width('100%')
      .padding(12)
      .backgroundColor('#FFFFFF')

      if (this.loading) {
        Text('加载中...').margin({ top: 40 })
      } else if (this.error.length > 0) {
        Text(this.error).fontColor('#E53935').margin({ top: 24 }).padding(16)
      } else if (this.posts.length === 0) {
        Text('暂无动态').fontColor('#999999').margin({ top: 40 })
      } else {
        List() {
          ForEach(this.posts, (post: DynamicPost) => {
            ListItem() {
              Column({ space: 8 }) {
                Row() {
                  Text(post.author_name).fontSize(15).fontWeight(FontWeight.Bold)
                  Blank()
                  Text(getDynamicSourceLabel(post.source_type))
                    .fontSize(12)
                    .fontColor('#007DFF')
                    .padding({ left: 8, right: 8, top: 2, bottom: 2 })
                    .borderRadius(8)
                    .backgroundColor('#E3F2FD')
                }.width('100%')

                Text(post.content).fontSize(14).width('100%')

                Row() {
                  Text(this.formatTime(post.created_at)).fontSize(12).fontColor('#999999')
                  Blank()
                  Text(`评论 ${post.comment_count}`)
                    .fontSize(12)
                    .fontColor('#007DFF')
                    .onClick(() => { this.toggleComments(post.id); })
                }.width('100%')

                // 评论列表
                if (this.expandedPostId === post.id) {
                  Divider().margin({ top: 4, bottom: 4 })
                  if (this.commentsLoading) {
                    Text('加载评论中...').fontSize(12).fontColor('#999999')
                  } else {
                    ForEach(this.comments, (c: DynamicComment) => {
                      Column() {
                        Row() {
                          Text(c.author_name).fontSize(12).fontColor('#007DFF')
                          Text('  ').fontSize(12)
                        }
                        Text(c.content).fontSize(13)
                        Divider()
                      }
                      .width('100%')
                      .alignItems(HorizontalAlign.Start)
                    }, (c: DynamicComment) => c.id + (this.expandedPostId === post.id ? '' : 'x'))
                  }
                  Row({ space: 6 }) {
                    TextInput({ placeholder: '写下你的评论...', text: this.commentInput })
                      .layoutWeight(1)
                      .height(36)
                      .fontSize(13)
                      .onChange((v: string) => { this.commentInput = v; })
                    Button('发送').fontSize(13).height(36).onClick(() => {
                      this.submitComment();
                    })
                  }.margin({ top: 6 })
                }
              }
              .width('100%')
              .padding(12)
              .borderRadius(8)
              .backgroundColor('#FFFFFF')
              .margin({ bottom: 10 })
            }
          }, (post: DynamicPost) => post.id)
        }
        .width('100%')
        .layoutWeight(1)
        .padding(12)
      }
    }
    .width('100%')
    .height('100%')
    .backgroundColor('#F5F5F5')
  }
}
