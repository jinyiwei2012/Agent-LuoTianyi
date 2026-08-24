/**
 * 主题系统（1:1 对齐 app/utils/theme.ts——light/dark 全量色板 + resolveTheme）。
 */
export type ColorMode = 'light' | 'dark' | 'system';
export type ResolvedThemeName = 'light' | 'dark';

export interface AppTheme {
  name: ResolvedThemeName;
  root: string;
  chatList: string;
  safeArea: string;
  inputBar: string;
  inputBorder: string;
  inputBackground: string;
  inputText: string;
  placeholder: string;
  surface: string;
  surfaceAlt: string;
  surfacePressed: string;
  elevated: string;
  border: string;
  text: string;
  textMuted: string;
  textSoft: string;
  accent: string;
  accentSoft: string;
  accentText: string;
  menuButton: string;
  userBubble: string;
  botBubble: string;
  bubbleText: string;
  userBubbleText: string;
  systemMessageText: string;
  dangerSurface: string;
  dangerText: string;
  debugBackground: string;
  debugBorder: string;
  debugHeader: string;
  debugText: string;
  debugAction: string;
  shadow: string;
  dim: string;
  live2dOverlay: string;
}

/** 颜色模式标签（对齐 COLOR_MODE_LABELS） */
interface ColorModeLabel {
  name: string;
  label: string;
}

export const COLOR_MODE_LABEL_LIST: ColorModeLabel[] = [
  { name: 'light', label: '浅色' },
  { name: 'dark', label: '深色' },
  { name: 'system', label: '跟随系统' },
];

export function colorModeLabel(mode: string): string {
  for (const item of COLOR_MODE_LABEL_LIST) {
    if (item.name === mode) {
      return item.label;
    }
  }
  return mode;
}

/** 存储键（对齐 COLOR_MODE_STORAGE_KEY） */
export const COLOR_MODE_STORAGE_KEY: string = 'color_mode';

function makeTheme(name: ResolvedThemeName, root: string, chatList: string, safeArea: string,
  inputBar: string, inputBorder: string, inputBackground: string, inputText: string,
  placeholder: string, surface: string, surfaceAlt: string, surfacePressed: string,
  elevated: string, border: string, text: string, textMuted: string, textSoft: string,
  accent: string, accentSoft: string, accentText: string, menuButton: string,
  userBubble: string, botBubble: string, bubbleText: string, userBubbleText: string,
  systemMessageText: string, dangerSurface: string, dangerText: string,
  debugBackground: string, debugBorder: string, debugHeader: string, debugText: string,
  debugAction: string, shadow: string, dim: string, live2dOverlay: string): AppTheme {
  const t: AppTheme = {
    name: name, root: root, chatList: chatList, safeArea: safeArea, inputBar: inputBar,
    inputBorder: inputBorder, inputBackground: inputBackground, inputText: inputText,
    placeholder: placeholder, surface: surface, surfaceAlt: surfaceAlt,
    surfacePressed: surfacePressed, elevated: elevated, border: border, text: text,
    textMuted: textMuted, textSoft: textSoft, accent: accent, accentSoft: accentSoft,
    accentText: accentText, menuButton: menuButton, userBubble: userBubble,
    botBubble: botBubble, bubbleText: bubbleText, userBubbleText: userBubbleText,
    systemMessageText: systemMessageText, dangerSurface: dangerSurface,
    dangerText: dangerText, debugBackground: debugBackground, debugBorder: debugBorder,
    debugHeader: debugHeader, debugText: debugText, debugAction: debugAction,
    shadow: shadow, dim: dim, live2dOverlay: live2dOverlay,
  };
  return t;
}

/** 浅色主题（1:1 对齐 theme.ts THEMES.light） */
export const THEME_LIGHT: AppTheme = makeTheme(
  'light', '#ffffff', '#E8E8E8', '#ffffff', '#f0f0f0', '#dddddd', '#ffffff',
  '#243447', '#999999', '#ffffff', '#f4f8fb', '#e8f6ff', '#ffffff', '#d9e1e8',
  '#243447', '#7b8794', '#4b5967', '#66CCFF', '#e8f6ff', '#1674a3',
  'rgba(255, 255, 255, 0.72)', '#ffffff', '#88EDFF', '#000000', '#000000',
  '#555555', '#fff1f1', '#c24141', '#0f1720', '#203244', '#b9d7ff', '#d9e4f0',
  '#79ffa8', '#000000', '#000000', 'rgba(0, 0, 0, 0)');

/** 深色主题（1:1 对齐 theme.ts THEMES.dark） */
export const THEME_DARK: AppTheme = makeTheme(
  'dark', '#0F1419', '#121A22', '#0F1419', '#151C24', '#2C3642', '#202A34',
  '#EEF4F8', '#7D8B99', '#18202A', '#202A34', '#12384B', '#202833', '#2E3946',
  '#EEF4F8', '#9AA8B7', '#C6D0DA', '#66CCFF', '#12384B', '#BDEEFF',
  'rgba(15, 20, 25, 0.78)', '#2B3542', '#0D5B73', '#F4FAFC', '#F4FAFC',
  '#9AA8B7', '#3A2024', '#FF9A9A', '#0A1118', '#2E4357', '#BDEEFF', '#D7E6F1',
  '#8EF2BE', '#000000', '#000000', 'rgba(19, 24, 30, 0.42)');

/** 解析主题：system 跟随系统深浅 */
export function resolveTheme(mode: ColorMode, systemDark: boolean): AppTheme {
  if (mode === 'system') {
    return systemDark ? THEME_DARK : THEME_LIGHT;
  }
  return mode === 'dark' ? THEME_DARK : THEME_LIGHT;
}

/** 兼容旧引用：默认浅色主题静态色板（原 Theme 类能力） */
export class Theme {
  static readonly root: string = THEME_LIGHT.root;
  static readonly chatList: string = THEME_LIGHT.chatList;
  static readonly safeArea: string = THEME_LIGHT.safeArea;
  static readonly inputBar: string = THEME_LIGHT.inputBar;
  static readonly inputBorder: string = THEME_LIGHT.inputBorder;
  static readonly inputBackground: string = THEME_LIGHT.inputBackground;
  static readonly inputText: string = THEME_LIGHT.inputText;
  static readonly placeholder: string = THEME_LIGHT.placeholder;
  static readonly surface: string = THEME_LIGHT.surface;
  static readonly surfaceAlt: string = THEME_LIGHT.surfaceAlt;
  static readonly border: string = THEME_LIGHT.border;
  static readonly text: string = THEME_LIGHT.text;
  static readonly textMuted: string = THEME_LIGHT.textMuted;
  static readonly textSoft: string = THEME_LIGHT.textSoft;
  static readonly accent: string = THEME_LIGHT.accent;
  static readonly accentSoft: string = THEME_LIGHT.accentSoft;
  static readonly accentText: string = THEME_LIGHT.accentText;
  static readonly userBubble: string = THEME_LIGHT.userBubble;
  static readonly botBubble: string = THEME_LIGHT.botBubble;
  static readonly bubbleText: string = THEME_LIGHT.bubbleText;
  static readonly userBubbleText: string = THEME_LIGHT.userBubbleText;
  static readonly systemMessageText: string = THEME_LIGHT.systemMessageText;
  static readonly dangerSurface: string = THEME_LIGHT.dangerSurface;
  static readonly dangerText: string = THEME_LIGHT.dangerText;
  static readonly debugBackground: string = THEME_LIGHT.debugBackground;
  static readonly debugBorder: string = THEME_LIGHT.debugBorder;
  static readonly debugHeader: string = THEME_LIGHT.debugHeader;
  static readonly debugText: string = THEME_LIGHT.debugText;
  static readonly debugAction: string = THEME_LIGHT.debugAction;
  static readonly shadow: string = THEME_LIGHT.shadow;
  static readonly dim: string = THEME_LIGHT.dim;
  static readonly live2dOverlay: string = THEME_LIGHT.live2dOverlay;
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
