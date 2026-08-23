import { webSocket } from '@kit.NetworkKit';
import { util } from '@kit.ArkTS';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { WsEventType, WS_CLIENT_CAPABILITIES, WsEnvelope, AckResult } from '../model/WsEvents';
import { AgentMessagePayload } from '../model/ChatModels';
import { SecurityStore } from './SecurityStore';
import { LlmConfigStore } from './LlmConfigStore';
import { buildChatCompletionsPayload, callLlmProvider } from './LlmClient';
import { HEARTBEAT_INTERVAL_MS } from '../common/Constants';

const TAG = 'WsTransport';

/** 连接回调，对齐 app utils/network_client.ts ConnectCallbacks */
export interface WsCallbacks {
  onAgentMessage: (payload: AgentMessagePayload) => void;
  onAgentStateChanged: (state: string) => void;
  onError: (errorText: string) => void;
}

interface AckWaiter {
  resolve: (r: AckResult) => void;
  timer: number;
}

/** rejectAllWaiters 选项（显式 interface） */
interface RejectOptions {
  code?: string;
  retryable?: boolean;
  drop?: boolean;
}

/** payload 容器（ArkTS 要求对象字面量匹配显式声明的 interface） */
/**
 * 上行消息信封（对齐 WsEnvelope 结构）。payload 运行时为 JSON 可变对象，
 * 具体结构由各发送方法的显式 payload interface 决定。
 */
interface WsSendMessage {
  type: string;
  client_msg_id?: string;
  ts?: number;
  payload: Object;
}

/** user_auth payload */
export interface SendAuthPayload {
  username: string;
  token: string;
  capabilities: string[];
}

/** hb_ping payload */
export interface SendPingPayload {
  ping_id: number;
}

/** user_text payload */
export interface SendTextPayload {
  text: string;
}

/** user_touch payload（对齐 app ws_transport submitUserTouch 的字段） */
export interface SendTouchPayload {
  touch_area: string;
  click_frequency?: Record<string, number>;
}

/** llm_response 成功负载 */
interface LlmResponseOk {
  request_id: string;
  content: string;
  usage: Object | null;
}

/** llm_response 失败负载 */
interface LlmResponseFail {
  request_id: string;
  error: string;
}

/**
 * WebSocket 传输层，对齐 app utils/ws_transport.ts。
 * 链路：connect -> user_auth -> auth_ok -> 心跳(hb_ping 10s) -> 消息分发
 * - sendWithAck: client_msg_id + server_ack 配对，超时 10s
 * - 断线重连：指数退避 2^n 上限 30s；auth_error 后不再重连
 */
export class WsTransport {
  private username: string;
  private token: string;
  private callbacks: WsCallbacks;

  private ws: webSocket.WebSocket | null = null;
  private isStopped = false;
  private isConnected = false;
  private isAuthed = false;
  private authRejected = false;
  private reconnectAttempts = 0;
  private reconnectTimer: number = -1;
  private heartbeatTimer: number = -1;
  private pingId = 0;
  private ackWaiters: Map<string, AckWaiter> = new Map<string, AckWaiter>();

  constructor(username: string, token: string, callbacks: WsCallbacks) {
    this.username = username;
    this.token = token;
    this.callbacks = callbacks;
  }

  async start(): Promise<void> {
    this.isStopped = false;
    await this.connect();
  }

  stop(): void {
    this.isStopped = true;
    this.stopHeartbeat();
    this.clearReconnectTimer();
    this.rejectAllWaiters('websocket stopped', { code: 'STOPPED', retryable: false, drop: true });
    if (this.ws !== null) {
      try {
        this.ws.close();
      } catch (e) {
        hilog.warn(0x0000, TAG, 'close failed: %{public}s', (e as Error).message);
      }
    }
    this.ws = null;
    this.isConnected = false;
    this.isAuthed = false;
  }

  private buildWsUrl(baseUrl: string): string {
    const trimmed = baseUrl.replace(/\/+$/, '');
    if (trimmed.startsWith('https://')) {
      return `wss://${trimmed.substr('https://'.length)}/chat_ws`;
    }
    if (trimmed.startsWith('http://')) {
      return `ws://${trimmed.substr('http://'.length)}/chat_ws`;
    }
    throw new Error('base_url must start with http:// or https://');
  }

  private async connect(): Promise<void> {
    if (this.isStopped) {
      return;
    }
    const baseUrl = await SecurityStore.getServerUrl();
    const wsUrl = this.buildWsUrl(baseUrl);
    hilog.info(0x0000, TAG, 'connecting to %{public}s', wsUrl);

    const ws = webSocket.createWebSocket();
    this.ws = ws;

    ws.on('open', () => {
      hilog.info(0x0000, TAG, 'ws open');
      this.isConnected = true;
      this.reconnectAttempts = 0;
      this.sendAuth();
      this.startHeartbeat();
    });

    ws.on('message', (err, data) => {
      if (err) {
        hilog.error(0x0000, TAG, 'message error: %{public}s', err.message);
        return;
      }
      let raw: string;
      if (typeof data === 'string') {
        raw = data;
      } else {
        raw = new util.TextDecoder().decodeToString(new Uint8Array(data));
      }
      this.handleServerMessage(raw);
    });

    ws.on('close', (err, closeResult) => {
      hilog.warn(0x0000, TAG, 'ws close code=%{public}d, reason=%{public}s',
        closeResult.code ?? -1, closeResult.reason ?? '');
      this.isConnected = false;
      this.isAuthed = false;
      this.stopHeartbeat();
      this.rejectAllWaiters('websocket disconnected', { code: 'DISCONNECTED', retryable: true, drop: false });
      if (this.isStopped) {
        return;
      }
      if (this.authRejected) {
        return;
      }
      this.scheduleReconnect();
    });

    ws.on('error', (err) => {
      hilog.error(0x0000, TAG, 'ws error: %{public}s', err.message);
    });

    try {
      await ws.connect(wsUrl);
    } catch (e) {
      hilog.error(0x0000, TAG, 'connect failed: %{public}s', (e as Error).message);
      // connect 失败（无 open 事件），尝试重连
      this.scheduleReconnect();
    }
  }

  private sendAuth(): void {
    const payload: SendAuthPayload = {
      username: this.username,
      token: this.token,
      capabilities: WS_CLIENT_CAPABILITIES,
    };
    const message: WsSendMessage = {
      type: WsEventType.USER_AUTH,
      client_msg_id: `auth-${Date.now()}`,
      ts: Date.now(),
      payload: payload,
    };
    this.sendRaw(message);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (!this.isConnected || !this.isAuthed) {
        return;
      }
      this.pingId += 1;
      const payload: SendPingPayload = { ping_id: this.pingId };
      const message: WsSendMessage = {
        type: WsEventType.HB_PING,
        client_msg_id: `ping-${this.pingId}`,
        ts: Date.now(),
        payload: payload,
      };
      this.sendRaw(message);
    }, HEARTBEAT_INTERVAL_MS);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== -1) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = -1;
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== -1) {
      clearTimeout(this.reconnectTimer);
    }
    const delay = Math.min(Math.pow(2, Math.max(this.reconnectAttempts, 1)), 30) * 1000;
    this.reconnectAttempts += 1;
    hilog.info(0x0000, TAG, 'reconnect in %{public}d ms (attempt %{public}d)', delay, this.reconnectAttempts);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = -1;
      this.connect();
    }, delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== -1) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = -1;
    }
  }

  private sendRaw(message: WsSendMessage): boolean {
    if (this.ws === null) {
      return false;
    }
    try {
      this.ws.send(JSON.stringify(message));
      return true;
    } catch (e) {
      hilog.error(0x0000, TAG, 'send failed: %{public}s', (e as Error).message);
      return false;
    }
  }

  /** 发送并等待 server_ack（超时 10s），返回 ACK 结果 */
  sendWithAck(eventType: string, payload: Object, timeoutMs: number,
    clientMsgId?: string): Promise<AckResult> {
    const requestId = clientMsgId !== undefined && clientMsgId.length > 0
      ? clientMsgId
      : `c-${Date.now()}-${Math.random().toString(36).substr(2, 8)}`;

    return new Promise<AckResult>((resolve) => {
      const timer = setTimeout(() => {
        this.ackWaiters.delete(requestId);
        resolve({
          ok: false,
          request_id: requestId,
          error: 'Wait server ack timeout',
        });
      }, timeoutMs);

      const waiter: AckWaiter = {
        resolve: resolve,
        timer: timer,
      };
      this.ackWaiters.set(requestId, waiter);

      const message: WsSendMessage = {
        type: eventType,
        client_msg_id: requestId,
        ts: Date.now(),
        payload: payload,
      };
      const sent = this.sendRaw(message);
      if (!sent) {
        clearTimeout(timer);
        this.ackWaiters.delete(requestId);
        resolve({
          ok: false,
          request_id: requestId,
          error: 'websocket disconnected before send',
          code: 'DISCONNECTED',
          retryable: true,
          drop: false,
        });
      }
    });
  }

  private handleServerMessage(raw: string): void {
    try {
      const envelope = JSON.parse(raw) as WsEnvelope;
      const eventType = envelope.type || '';
      const payload = envelope.payload || {};

      if (eventType === WsEventType.SYSTEM_READY) {
        this.sendAuth();
        return;
      }

      if (eventType === WsEventType.AUTH_OK) {
        this.authRejected = false;
        this.isAuthed = true;
        this.reconnectAttempts = 0;
        hilog.info(0x0000, TAG, 'auth ok');
        return;
      }

      if (eventType === WsEventType.AUTH_ERROR) {
        this.isAuthed = false;
        this.authRejected = true;
        const msg = String(payload['message'] || '鉴权失败');
        this.rejectAllWaiters('authentication rejected', {
          code: 'AUTH_REJECTED',
          retryable: false,
          drop: true,
        });
        this.clearReconnectTimer();
        this.stopHeartbeat();
        this.callbacks.onError(msg);
        try {
          this.ws?.close();
        } catch (e) {
          hilog.warn(0x0000, TAG, 'close after auth error: %{public}s', (e as Error).message);
        }
        return;
      }

      if (eventType === WsEventType.SERVER_ACK) {
        const replyTo = envelope.reply_to || '';
        if (replyTo.length > 0 && this.ackWaiters.has(replyTo)) {
          const waiter = this.ackWaiters.get(replyTo);
          if (waiter !== undefined) {
            clearTimeout(waiter.timer);
            this.ackWaiters.delete(replyTo);
            waiter.resolve({
              ok: payload['ok'] === true,
              request_id: replyTo,
              error: payload['error'] !== undefined ? String(payload['error']) : undefined,
            });
          }
        }
        return;
      }

      if (eventType === WsEventType.AGENT_STATE_CHANGED) {
        this.callbacks.onAgentStateChanged(String(payload['state'] || 'waiting'));
        return;
      }

      if (eventType === WsEventType.LLM_REQUEST) {
        // llm_request 委托：读取本地 LLM 配置 -> OpenAI 兼容调用 -> llm_response
        this.handleLlmRequest(payload);
        return;
      }

      if (eventType === WsEventType.AGENT_MESSAGE) {
        this.callbacks.onAgentMessage(payload as AgentMessagePayload);
        return;
      }

      if (eventType === WsEventType.SERVER_ERROR) {
        const message = String(payload['message'] || '协议错误');
        hilog.error(0x0000, TAG, 'server error: %{public}s', message);
        this.callbacks.onError(message);
        return;
      }

      hilog.debug(0x0000, TAG, 'recv unhandled type: %{public}s', eventType);
    } catch (e) {
      hilog.error(0x0000, TAG, 'parse error: %{public}s', (e as Error).message);
      this.callbacks.onError('收到无法解析的 WebSocket 消息');
    }
  }

  /**
   * 处理 llm_request 委托（对齐 app message_processor handleLlmRequest）：
   * 读模块配置 -> 构建 payload -> 直接调用用户自备的 OpenAI 兼容接口 ->
   * 回传 llm_response{request_id, content, usage}；异常回传错误。
   */
  private handleLlmRequest(payload: Record<string, Object>): void {
    const requestId = String(payload['request_id'] ?? '');
    this.performLlmRequest(requestId, payload).then((responsePayload: Object) => {
      const message: WsSendMessage = {
        type: WsEventType.LLM_RESPONSE,
        ts: Date.now(),
        payload: responsePayload,
      };
      this.sendRaw(message);
    });
  }

  private async performLlmRequest(requestId: string, payload: Record<string, Object>): Promise<Object> {
    try {
      const moduleId = String(payload['type'] ?? '');
      const cfg = await LlmConfigStore.getModuleConfig(moduleId);
      if (cfg === null) {
        const fail: LlmResponseFail = {
          request_id: requestId,
          error: `未配置模块 ${moduleId} 的 LLM 参数`,
        };
        return fail;
      }
      const baseUrl = String(cfg['baseUrl'] ?? '').replace(/\/+$/, '');
      if (baseUrl.length === 0) {
        const fail: LlmResponseFail = { request_id: requestId, error: 'baseUrl 为空' };
        return fail;
      }
      const apiKey = String(cfg['apiKey'] ?? '');
      const model = String(cfg['model'] ?? '');
      const paramsText = String(cfg['paramsText'] ?? '');
      let cachedParams: Record<string, Object> = {};
      if (paramsText.length > 0) {
        try {
          const parsed = JSON.parse(paramsText) as Record<string, Object>;
          cachedParams = parsed;
        } catch (e) {
          hilog.warn(0x0000, TAG, 'paramsText parse failed: %{public}s', (e as Error).message);
        }
      }
      const caps = cfg['modelCapabilities'] as Record<string, Object> | null ?? {};
      const useJson = payload['use_json'] === true && caps['can_use_json'] === true;
      const enableThinking = payload['enable_thinking'] === true && caps['can_enable_thinking'] === true;

      // 合并 params：服务端指定 + 本地缓存
      const mergedParams: Record<string, Object> = {};
      const remoteParams = payload['params'] as Record<string, Object> | null ?? {};
      for (const k of Object.keys(remoteParams)) {
        mergedParams[k] = remoteParams[k];
      }
      for (const k of Object.keys(cachedParams)) {
        mergedParams[k] = cachedParams[k];
      }

      const body = buildChatCompletionsPayload({
        prompt: String(payload['prompt'] ?? ''),
        model: model,
        params: mergedParams,
        enableThinking: enableThinking,
        useJson: useJson,
        imageBase64: typeof payload['image_base64'] === 'string' ? String(payload['image_base64']) : undefined,
      });

      const result = await callLlmProvider({
        url: `${baseUrl}/chat/completions`,
        apiKey: apiKey,
        body: body,
      });

      const ok: LlmResponseOk = {
        request_id: requestId,
        content: result.content,
        usage: result.usage,
      };
      return ok;
    } catch (e) {
      const fail: LlmResponseFail = {
        request_id: requestId,
        error: e instanceof Error ? e.message : String(e),
      };
      return fail;
    }
  }

  private rejectAllWaiters(errorText: string, options: RejectOptions): void {
    this.ackWaiters.forEach((waiter: AckWaiter, requestId: string) => {
      clearTimeout(waiter.timer);
      const r: AckResult = {
        ok: false,
        request_id: requestId,
        error: errorText,
      };
      if (options.code !== undefined) {
        r.code = options.code;
      }
      if (options.retryable !== undefined) {
        r.retryable = options.retryable;
      }
      if (options.drop !== undefined) {
        r.drop = options.drop;
      }
      waiter.resolve(r);
    });
    this.ackWaiters.clear();
  }
}
