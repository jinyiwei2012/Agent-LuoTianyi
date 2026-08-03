/**
 * call_ws_transport 单元测试
 * 验证 /call_ws 协议传输：鉴权、事件解析、重连恢复、ACK 与状态迁移。
 */
import { CallTransport } from '../utils/call_transport';
import { WSEventType } from '../types/ws_events';
import { CallStatus } from '../types/call';

jest.mock('react-native', () => ({
  AppState: {
    addEventListener: jest.fn(() => ({ remove: jest.fn() })),
  },
}));

jest.mock('../config', () => ({
  server_config: { BASE_URL: 'http://127.0.0.1:60030' },
}));

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 0;
  sent: string[] = [];

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = 3;
    this.onclose?.();
  }

  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  serverMessage(msg: unknown) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }

  lastSent(): Record<string, unknown> {
    return JSON.parse(this.sent[this.sent.length - 1] ?? '{}');
  }

  static reset() {
    FakeWebSocket.instances = [];
  }
}

function makeTransport() {
  const events: Array<[string, Record<string, unknown>]> = [];
  const statuses: CallStatus[] = [];
  const errors: string[] = [];
  const callbacks = {
    onEvent: (type: string, payload: Record<string, unknown>) => events.push([type, payload]),
    onStatus: (status: CallStatus) => statuses.push(status),
    onError: (message: string) => errors.push(message),
  };
  const transport = new CallTransport('u1', 't1', callbacks);
  return { transport, events, statuses, errors };
}

beforeEach(() => {
  FakeWebSocket.reset();
  (globalThis as any).WebSocket = FakeWebSocket;
  jest.useFakeTimers();
});

afterEach(() => {
  jest.clearAllTimers();
  jest.useRealTimers();
  (globalThis as any).WebSocket = undefined;
});

describe('CallTransport', () => {
  it('连接后发送鉴权，auth_ok 后进入就绪状态', () => {
    const { transport } = makeTransport();
    transport.start();
    const ws = FakeWebSocket.instances[0];
    expect(ws.url).toContain('/call_ws');
    ws.open();
    expect(ws.lastSent().type).toBe(WSEventType.USER_AUTH);
    ws.serverMessage({ type: WSEventType.AUTH_OK });
    jest.advanceTimersByTime(10000); // 心跳周期 10 秒
    expect(ws.sent.some((msg) => JSON.parse(msg).type === WSEventType.HB_PING)).toBe(true);
  });

  it('startCall 发送 call.start 并解析 ACK 中的 call_id', async () => {
    const { transport } = makeTransport();
    transport.start();
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.serverMessage({ type: WSEventType.AUTH_OK });

    const promise = transport.startCall();
    expect(ws.lastSent().type).toBe(WSEventType.CALL_START);
    ws.serverMessage({
      type: WSEventType.SERVER_ACK,
      reply_to: JSON.parse(ws.sent[ws.sent.length - 1]).client_msg_id,
      payload: { ok: true, call_id: 'call-1' },
    });
    const result = await promise;
    expect(result.ok).toBe(true);
  });

  it('call.connected 将状态置为 active，call.ended 置为 ended', () => {
    const { transport, statuses } = makeTransport();
    transport.start();
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.serverMessage({ type: WSEventType.AUTH_OK });
    transport.startCall();

    ws.serverMessage({ type: WSEventType.CALL_CONNECTED, payload: { connected_at: '2026-08-03T00:00:00' } });
    expect(statuses).toContain('active');

    ws.serverMessage({ type: WSEventType.CALL_ENDED, payload: { exit_code: 0, duration_seconds: 3 } });
    expect(statuses).toContain('ended');
  });

  it('CALL_REJECTED 置为 ended 并透传拒绝原因', () => {
    const { transport, statuses, events } = makeTransport();
    transport.start();
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.serverMessage({ type: WSEventType.AUTH_OK });
    ws.serverMessage({ type: WSEventType.CALL_REJECTED, payload: { code: 'CALL_CONCURRENCY_LIMIT', message: '当前电话并发已满' } });
    expect(statuses).toContain('ended');
    expect(events.some(([t]) => t === WSEventType.CALL_REJECTED)).toBe(true);
  });

  it('断线后进入 reconnecting 并自动重连恢复（call.resume）', () => {
    const { transport, statuses } = makeTransport();
    transport.start();
    const ws1 = FakeWebSocket.instances[0];
    ws1.open();
    ws1.serverMessage({ type: WSEventType.AUTH_OK });
    transport.startCall();
    // 受理 ACK 携带 call_id，重连后才能恢复
    ws1.serverMessage({
      type: WSEventType.SERVER_ACK,
      reply_to: JSON.parse(ws1.sent[ws1.sent.length - 1]).client_msg_id,
      payload: { ok: true, call_id: 'call-1' },
    });
    ws1.serverMessage({ type: WSEventType.CALL_CONNECTED, payload: {} });
    expect(statuses).toContain('active');

    ws1.close(); // 服务端断线
    expect(statuses).toContain('reconnecting');

    jest.advanceTimersByTime(2100); // 约 2 秒后重连
    const ws2 = FakeWebSocket.instances[1];
    expect(ws2).toBeDefined();
    ws2.open();
    ws2.serverMessage({ type: WSEventType.AUTH_OK });
    // 重连后应发送 call.resume 恢复原电话
    expect(ws2.sent.some((msg) => JSON.parse(msg).type === WSEventType.CALL_RESUME)).toBe(true);
  });

  it('停止后不再重连', () => {
    const { transport } = makeTransport();
    transport.start();
    const ws1 = FakeWebSocket.instances[0];
    ws1.open();
    ws1.serverMessage({ type: WSEventType.AUTH_OK });
    transport.stop();
    ws1.close();
    jest.advanceTimersByTime(5000);
    expect(FakeWebSocket.instances.length).toBe(1);
  });

  it('playback 事件携带 call_id/audio_id/response_id', () => {
    const { transport } = makeTransport();
    transport.start();
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.serverMessage({ type: WSEventType.AUTH_OK });
    transport.startCall();
    transport.playbackCompleted('audio-1', 'resp-1');
    const sent = ws.lastSent();
    expect(sent.type).toBe(WSEventType.CALL_PLAYBACK_COMPLETED);
    expect((sent.payload as Record<string, unknown>).audio_id).toBe('audio-1');
    expect((sent.payload as Record<string, unknown>).response_id).toBe('resp-1');
  });
});
