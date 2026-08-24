/**
 * 主题系统（对齐 app/utils/theme.ts 的 light 主题色板）。
 * 鸿蒙端暂实现 light 单一主题（对齐安卓默认），后续可扩展 dark。
 */
export class Theme {
  /** 页面根背景 */
  static readonly root: string = '#ffffff';
  /** 聊天列表背景 */
  static readonly chatList: string = '#E8E8E8';
  /** 顶栏/安全区背景 */
  static readonly safeArea: string = '#ffffff';
  /** 输入栏背景 */
  static readonly inputBar: string = '#f0f0f0';
  static readonly inputBorder: string = '#dddddd';
  static readonly inputBackground: string = '#ffffff';
  static readonly inputText: string = '#243447';
  static readonly placeholder: string = '#999999';
  /** 卡片/浮层 */
  static readonly surface: string = '#ffffff';
  static readonly surfaceAlt: string = '#f4f8fb';
  static readonly border: string = '#d9e1e8';
  /** 主要文字 */
  static readonly text: string = '#243447';
  static readonly textMuted: string = '#7b8794';
  static readonly textSoft: string = '#4b5967';
  /** 强调色（天空蓝） */
  static readonly accent: string = '#66CCFF';
  static readonly accentSoft: string = '#e8f6ff';
  static readonly accentText: string = '#1674a3';
  /** 气泡 */
  static readonly userBubble: string = '#ffffff';
  static readonly botBubble: string = '#88EDFF';
  static readonly bubbleText: string = '#000000';
  static readonly userBubbleText: string = '#000000';
  static readonly systemMessageText: string = '#555555';
  /** 危险色 */
  static readonly dangerSurface: string = '#fff1f1';
  static readonly dangerText: string = '#c24141';
  /** Live2D 遮罩 */
  static readonly live2dOverlay: string = 'rgba(0, 0, 0, 0.05)';
  /** 菜单按钮 */
  static readonly menuButton: string = 'rgba(255, 255, 255, 0.72)';
  /** 遮罩层 */
  static readonly dim: string = 'rgba(0, 0, 0, 0.32)';
  /** 图像/占位 */
  static readonly debugBackground: string = '#f9f9f9';
  static readonly debugBorder: string = '#dddddd';
  static readonly debugHeader: string = '#4b5967';
  static readonly debugText: string = '#333333';
  static readonly debugAction: string = '#1674a3';
  static readonly shadow: string = '#000000';
}

/** 消息文本字号（对齐安卓 bubbleText 15） */
export const MSG_FONT_SIZE: number = 15;
/** 气泡 padding（对齐安卓 10/15） */
export const BUBBLE_PADDING_V: number = 10;
export const BUBBLE_PADDING_H: number = 15;
/** 气泡圆角（对齐安卓 10，尾巴圆角 2） */
export const BUBBLE_RADIUS: number = 10;
export const BUBBLE_TAIL_RADIUS: number = 2;
/** 消息最大宽度（对齐安卓 80%） */
export const BUBBLE_MAX_WIDTH: string = '80%';
/** Live2D 高度占比（对齐安卓 screenHeight * 0.4） */
export const LIVE2D_HEIGHT_RATIO: string = '40%';
