import { router } from '@kit.ArkUI';

/**
 * 启动页：对齐安卓 RootLayout 行为——启动后进入登录（自动登录在 LoginPage 处理，
 * 成功后直进 ChatPage）。无调试/多余入口。
 */
@Entry
@Component
struct Index {
  build() {
    Column({ space: 40 }) {
      Text('洛天依')
        .fontSize(36)
        .fontWeight(FontWeight.Bold)
      Text('与天依的每一句话')
        .fontSize(14)
        .fontColor('#7B8794')
      Button('开始使用')
        .width('70%')
        .height(48)
        .borderRadius(24)
        .backgroundColor('#66CCFF')
        .fontColor('#243447')
        .onClick(() => {
          router.pushUrl({ url: 'pages/LoginPage' });
        })
    }
    .width('100%')
    .height('100%')
    .justifyContent(FlexAlign.Center)
    .backgroundColor('#ffffff')
  }
}
