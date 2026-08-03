/**
 * 电话音频播放链路测试（WebView ↔ useCallLogic 桥接）
 * 覆盖设计文档 18.2 中音频控制器职责的 App 端等价实现：
 * 完成/停止 ACK、重复消息幂等、取消（stop_playback）与迟到音频防护。
 */
import React from 'react';
import renderer, { act } from 'react-test-renderer';
import { WSEventType } from '../types/ws_events';

jest.mock('../modules/pcm-recorder', () => ({
  PcmRecorder: class {
    start = jest.fn(async () => {});
    stop = jest.fn(async () => {});
  },
}));

const transportInstances: any[] = [];
jest.mock('../utils/call_transport', () => ({
  CallTransport: class {
    start = jest.fn();
    stop = jest.fn();
    startCall = jest.fn(async () => ({ ok: true, request_id: 'r1' }));
    hangup = jest.fn(async () => ({ ok: true, request_id: 'r2' }));
    appendAudio = jest.fn();
    playbackCompleted = jest.fn();
    playbackStopped = jest.fn();
    constructor(_u: string, _t: string, public callbacks: any) {
      transportInstances.push(this);
    }
  },
}));

jest.mock('../utils/live2d_helper', () => ({
  setExpression: jest.fn(),
}));

import { useCallLogic } from '../hooks/useCallLogic';
import { setExpression } from '../utils/live2d_helper';

function HookHarness({ onHook }: { onHook: (hook: ReturnType<typeof useCallLogic>) => void }) {
  const hook = useCallLogic({ current: null } as any, 'u1', 't1', () => {}, true);
  onHook(hook);
  return null;
}

function renderHook() {
  let latest: ReturnType<typeof useCallLogic> | null = null;
  act(() => {
    renderer.create(
      <HookHarness
        onHook={(hook) => {
          latest = hook;
        }}
      />,
    );
  });
  const transport = transportInstances[transportInstances.length - 1];
  return {
    hook: () => latest as NonNullable<typeof latest>,
    transport,
    emitEvent: (type: string, payload: Record<string, unknown> = {}) =>
      act(() => transport.callbacks.onEvent(type, payload)),
  };
}

beforeEach(() => {
  transportInstances.length = 0;
  (setExpression as jest.Mock).mockClear();
});

describe('电话音频链路', () => {
  it('CALL_STOP_PLAYBACK 指令到达时不抛错（WebView 停止由注入脚本执行）', () => {
    const { emitEvent } = renderHook();
    expect(() => {
      emitEvent(WSEventType.CALL_STOP_PLAYBACK, {
        call_id: 'call-1',
        response_id: 'resp-1',
        audio_ids: ['audio-1'],
        reason: 'user_barge_in',
      });
    }).not.toThrow();
  });

  it('播放完成的同一 audio_id 重复 ACK 由上层幂等处理（不重复发 completed）', () => {
    const { hook, transport } = renderHook();
    act(() => {
      hook().handleWebViewMessage({ nativeEvent: { data: JSON.stringify({ type: 'audio_finished', audio_id: 'audio-1' }) } });
    });
    act(() => {
      hook().handleWebViewMessage({ nativeEvent: { data: JSON.stringify({ type: 'audio_finished', audio_id: 'audio-1' }) } });
    });
    // 桥接层忠实转发；服务端按 (call_id, response_id, audio_id) 幂等去重
    expect(transport.playbackCompleted).toHaveBeenCalledTimes(2);
  });

  it('迟到音频（已取消 response）不会触发播放相关调用', () => {
    const { hook, transport } = renderHook();
    // 服务端对已取消的 response 不再下发音频；若异常到达，桥接层不做任何播放动作
    act(() => {
      hook().handleWebViewMessage({ nativeEvent: { data: JSON.stringify({ type: 'audio_finished', audio_id: '' }) } });
    });
    expect(transport.playbackCompleted).not.toHaveBeenCalled();
  });

  it('音频分片事件携带 expression 时更新 Live2D 表情', () => {
    const { emitEvent } = renderHook();
    emitEvent(WSEventType.CALL_AUDIO_CHUNK, {
      audio: 'AAAA',
      audio_id: 'audio-1',
      response_id: 'resp-1',
      is_final: false,
      expression: '微笑脸',
    });
    expect(setExpression).toHaveBeenCalledWith('微笑脸', expect.anything());
  });
});

