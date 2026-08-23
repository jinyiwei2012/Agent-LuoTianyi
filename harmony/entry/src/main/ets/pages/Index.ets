import { router } from '@kit.ArkUI';

@Entry
@Component
struct Index {
  @State message: string = 'Hello 洛天依';

  build() {
    Column({ space: 24 }) {
      Text(this.message)
        .fontSize(30)
        .fontWeight(FontWeight.Bold)
      Button('Live2D 验证')
        .onClick(() => {
          router.pushUrl({ url: 'pages/Live2DTestPage' });
        })
    }
    .width('100%')
    .height('100%')
    .justifyContent(FlexAlign.Center)
  }
}
