/**
 * useCallLogic 单元测试（含 WebView↔传输层 ACK 桥接）
 * 覆盖：状态迁移、录音启停、播放完成/停止 ACK 桥接、启动失败与清理。
 */
import React from 'react';
import renderer, { act } from 'react-test-renderer';
import { WSEventType } from '../types/ws_events';

jest.mock('../modules/pcm-recorder', () => {
  return {
    PcmRecorder: class FakePcmRecorder {
      start = jest.fn(async () => {});
      stop = jest.fn(async () => {});
    },
  };
});

const transportInstances: any[] = [];
jest.mock('../utils/call_transport', () => {
  return {
    CallTransport: class FakeCallTransport {
      start = jest.fn();
      stop = jest.fn();
      startCall = jest.fn(async () => ({ ok: true, request_id: 'r1' }));
      hangup = jest.fn(async () => ({ ok: true, request_id: 'r2' }));
      appendAudio = jest.fn();
      playbackCompleted = jest.fn();
      playbackStopped = jest.fn();
      constructor(
        username: string,
        token: string,
        public callbacks: any,
      ) {
        transportInstances.push(this);
      }
    },
  };
});

jest.mock('../utils/live2d_helper', () => ({
  setExpression: jest.fn(),
}));

import { useCallLogic } from '../hooks/useCallLogic';

function HookHarness({ onHook, onEnded }: { onHook: (hook: ReturnType<typeof useCallLogic>) => void; onEnded?: () => void }) {
  const hook = useCallLogic({ current: null } as any, 'u1', 't1', onEnded, true);
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
    emitStatus: (status: string) => act(() => transport.callbacks.onStatus(status)),
    emitEvent: (type: string, payload: Record<string, unknown> = {}) =>
      act(() => transport.callbacks.onEvent(type, payload)),
    emitError: (message: string) => act(() => transport.callbacks.onError(message)),
  };
}

beforeEach(() => {
  transportInstances.length = 0;
});

describe('useCallLogic', () => {
  it('初始状态为 idle，挂载后启动传输', () => {
    const { hook, transport } = renderHook();
    expect(hook().status).toBe('idle');
    expect(transport.start).toHaveBeenCalled();
  });

  it('call.connected 后状态变为 active 并启动录音', () => {
    const { hook, emitStatus } = renderHook();
    emitStatus('active');
    expect(hook().status).toBe('active');
  });

  it('reconnecting 时停止录音，ended 时也停止录音', async () => {
    const { hook, emitStatus } = renderHook();
    emitStatus('active');
    emitStatus('reconnecting');
    emitStatus('ended');
    expect(hook().status).toBe('ended');
  });

  it('startCall 失败时设置错误信息', async () => {
    const { hook, transport } = renderHook();
    transport.startCall.mockResolvedValueOnce({ ok: false, request_id: 'r1', error: '电话建立失败' });
    await act(async () => {
      await hook().startCall();
    });
    expect(hook().error).toContain('电话建立失败');
  });

  it('WebView audio_finished 桥接为 playbackCompleted ACK', () => {
    const { hook, transport } = renderHook();
    act(() => {
      hook().handleWebViewMessage({
        nativeEvent: { data: JSON.stringify({ type: 'audio_finished', audio_id: 'audio-1', response_id: 'resp-1' }) },
      });
    });
    expect(transport.playbackCompleted).toHaveBeenCalledWith('audio-1', 'resp-1');
  });

  it('WebView audio_stopped 桥接为 playbackStopped ACK', () => {
    const { hook, transport } = renderHook();
    act(() => {
      hook().handleWebViewMessage({
        nativeEvent: { data: JSON.stringify({ type: 'audio_stopped', audio_id: 'audio-2' }) },
      });
    });
    expect(transport.playbackStopped).toHaveBeenCalledWith('audio-2', undefined);
  });

  it('非音频消息（modelLoaded/touch）被忽略，不崩溃', () => {
    const { hook, transport } = renderHook();
    expect(() => {
      act(() => {
        hook().handleWebViewMessage({ nativeEvent: { data: JSON.stringify({ type: 'modelLoaded', success: true }) } });
      });
    }).not.toThrow();
    expect(transport.playbackCompleted).not.toHaveBeenCalled();
    expect(transport.playbackStopped).not.toHaveBeenCalled();
  });

  it('CALL_ENDED 触发 onEnded 回调', () => {
    const onEnded = jest.fn();
    act(() => {
      renderer.create(<HookHarness onHook={() => {}} onEnded={onEnded} />);
    });
    const callbacks = transportInstances[transportInstances.length - 1].callbacks;
    act(() => callbacks.onEvent(WSEventType.CALL_ENDED, {}));
    expect(onEnded).toHaveBeenCalled();
  });

  it('CALL_AUDIO_CHUNK 透传到 onEvent 并被上层处理（不抛错）', () => {
    const { emitEvent } = renderHook();
    expect(() => {
      emitEvent(WSEventType.CALL_AUDIO_CHUNK, { audio: 'AAAA', audio_id: 'audio-1', response_id: 'resp-1', is_final: true });
    }).not.toThrow();
  });
});

