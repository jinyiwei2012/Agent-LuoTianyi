/**
 * CallScreen 组件测试
 * 覆盖：状态文案、挂断按钮、未接通蒙版与错误提示。
 */
import React from 'react';
import renderer, { act } from 'react-test-renderer';
import type { ReactTestRenderer } from 'react-test-renderer';
import { CallStatus } from '../types/call';

jest.mock('react-native', () => ({
  Platform: { OS: 'web' },
  PermissionsAndroid: {
    PERMISSIONS: { RECORD_AUDIO: 'android.permission.RECORD_AUDIO' },
    RESULTS: { GRANTED: 'granted' },
    request: jest.fn(async () => 'granted'),
  },
  Pressable: 'Pressable',
  StyleSheet: { create: (styles: Record<string, unknown>) => styles },
  Text: 'Text',
  View: 'View',
  useWindowDimensions: () => ({ width: 400, height: 800 }),
}));

jest.mock('react-native-webview', () => ({
  WebView: 'WebView',
}));

jest.mock('../config', () => ({
  server_config: { BASE_URL: 'http://127.0.0.1:60030' },
}));

jest.mock('../components/auth', () => ({
  auth: { username: 'u1', message_token: 't1' },
}));

jest.mock('react-native-safe-area-context', () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

jest.mock('expo-constants', () => ({
  __esModule: true,
  default: { expoConfig: { hostUri: 'localhost:8081' } },
}));

const mockHook = {
  status: 'requesting' as CallStatus,
  error: null as string | null,
  elapsedSeconds: 0,
  startCall: jest.fn(async () => undefined),
  hangup: jest.fn(async () => undefined),
  handleWebViewMessage: jest.fn(),
};
jest.mock('../hooks/useCallLogic', () => ({
  useCallLogic: () => mockHook,
}));

import CallScreen from '../app/call';

function renderScreen() {
  const onClose = jest.fn();
  let tree: ReactTestRenderer | null = null;
  act(() => {
    tree = renderer.create(<CallScreen onClose={onClose} />);
  });
  return { tree: tree as unknown as ReactTestRenderer, onClose };
}

function allTexts(root: ReactTestRenderer): string[] {
  const texts: string[] = [];
  root.root.findAll((node) => node.type === 'Text').forEach((node) => {
    const value = Array.isArray(node.props.children) ? node.props.children.join('') : String(node.props.children ?? '');
    if (value) texts.push(value);
  });
  return texts;
}

beforeEach(() => {
  mockHook.status = 'requesting';
  mockHook.error = null;
  mockHook.elapsedSeconds = 0;
});

describe('CallScreen', () => {
  it('请求中状态显示接通文案，且挂断按钮存在', () => {
    const { tree } = renderScreen();
    expect(allTexts(tree)).toContain('正在接通…');
    const hangup = tree.root.findAll((node) => node.props.accessibilityLabel === '挂断');
    expect(hangup.length).toBe(1);
  });

  it('active 状态显示通话时长并隐藏蒙版', () => {
    mockHook.status = 'active';
    mockHook.elapsedSeconds = 65;
    const { tree } = renderScreen();
    expect(allTexts(tree)).toContain('通话中 01:05');
    // 接通后蒙版（pointerEvents=none 的遮罩）不再渲染
    const masks = tree.root.findAll((node) => node.props.pointerEvents === 'none');
    expect(masks.some((m) => String(m.props.style?.[0]?.backgroundColor ?? '').includes('rgba'))).toBe(false);
  });

  it('reconnecting 状态显示恢复文案', () => {
    mockHook.status = 'reconnecting';
    const { tree } = renderScreen();
    expect(allTexts(tree)).toContain('正在恢复通话…');
  });

  it('显示错误信息', () => {
    mockHook.error = '通话网络连接发生错误';
    const { tree } = renderScreen();
    expect(allTexts(tree)).toContain('通话网络连接发生错误');
  });

  it('挂断按钮点击调用 hangup', async () => {
    mockHook.status = 'active';
    const { tree } = renderScreen();
    const hangup = tree.root.findAll((node) => node.props.accessibilityLabel === '挂断')[0];
    await act(async () => {
      hangup.props.onPress();
    });
    expect(mockHook.hangup).toHaveBeenCalled();
  });
});

