/**
 * 服务端 WebSocket 事件类型常量，与 server/src/interface/types.py WSEventType 一一对应。
 * 对齐 app/types/ws_events.ts。
 */
export class WsEventType {
  // 系统
  static readonly SYSTEM_READY: string = 'system_ready';
  static readonly AUTH_SUCCESS: string = 'auth_success';
  static readonly AUTH_FAILURE: string = 'auth_failure';
  static readonly SERVER_ERROR: string = 'error';
  static readonly SERVER_ACK: string = 'server_ack';
  static readonly AUTH_ERROR: string = 'auth_error';
  static readonly AUTH_OK: string = 'auth_ok';

  // 代理状态
  static readonly AGENT_STATE_CHANGED: string = 'agent_state_changed';
  static readonly AGENT_MESSAGE: string = 'agent_message';
  static readonly LLM_REQUEST: string = 'llm_request';
  static readonly LLM_RESPONSE: string = 'llm_response';

  // 用户事件（客户端发送）
  static readonly USER_MESSAGE: string = 'user_message';
  static readonly USER_IMAGE: string = 'user_image';
  static readonly USER_TEXT: string = 'user_text';
  static readonly USER_TYPING: string = 'user_typing';
  static readonly USER_AUTH: string = 'user_auth';
  static readonly USER_TOUCH: string = 'user_touch';
  static readonly USER_IMAGE_SELECTING: string = 'user_image_selecting';
  static readonly USER_IMAGE_SELECTING_CANCEL: string = 'user_image_selecting_cancel';

  // 心跳
  static readonly HB_PING: string = 'hb_ping';
  static readonly HB_PONG: string = 'hb_pong';

  // 其他
  static readonly DATE_DETECTED: string = 'date_detected';
}

/** 客户端能力声明，对齐 app WS_CLIENT_CAPABILITIES */
export const WS_CLIENT_CAPABILITIES: string[] = ['negative_ack_v1'];

/** WS 消息信封（服务端下发/客户端上行的外层包装） */
export interface WsEnvelope {
  type: string;
  client_msg_id?: string;
  reply_to?: string;
  ts?: number;
  payload: Record<string, Object>;
}

/** ACK 结果（sendWithAck 返回值） */
export interface AckResult {
  ok: boolean;
  request_id: string;
  error?: string;
  code?: string;
  retryable?: boolean;
  drop?: boolean;
}
