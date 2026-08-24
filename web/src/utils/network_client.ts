import { AgentMessagePayload } from '@/types/chat';
import { addDebugTrace } from './debug_trace';
import { WebSocketTransport } from './ws_transport';

interface SendResult {
  ok: boolean;
  request_id: string;
  error?: string;
  drop?: boolean;
}

interface ConnectCallbacks {
  onAgentMessage: (payload: AgentMessagePayload) => void;
  onAgentStateChanged: (state: string) => void;
  onError: (errorText: string) => void;

  onLlmRequest?: (payload: Record<string, unknown>) => Promise<Record<string, unknown> | null>;
  getLlmMode?: () => Promise<{ types: string[] }>;
}

function sanitizeBase64(input: string) {
  const idx = input.indexOf(',');
  return idx >= 0 ? input.slice(idx + 1) : input;
}

export class NetworkClient {
  private transport: WebSocketTransport | null = null;

  connectWs(username: string, token: string, callbacks: ConnectCallbacks) {
    this.disconnectWs();
    addDebugTrace('network', 'connectWs', { username });
    this.transport = new WebSocketTransport(username, token, callbacks);
    this.transport.start();
  }

  disconnectWs() {
    if (this.transport) {
      addDebugTrace('network', 'disconnectWs');
      this.transport.stop();
      this.transport = null;
    }
  }

  sendChat(text: string, isProactive = false, clientMsgId?: string): Promise<SendResult> {
    if (!this.transport) {
      addDebugTrace('network', 'sendChat blocked: no transport');
      return Promise.resolve({
        ok: false,
        request_id: clientMsgId || `local-${Date.now()}`,
        error: 'not logged in',
        drop: true,
      });
    }
    addDebugTrace('network', 'sendChat', { textLength: text.length });
    return this.transport.submitUserText(text, isProactive, 10000, clientMsgId);
  }

  /**
   * Web 版：图片已在 UI 层通过 FileReader 读为 base64（可含 data URL 前缀），
   * 这里直接传给 WebSocket，无需文件系统读取。
   */
  async sendImage(imageBase64: string, mimeType: string, clientMsgId?: string): Promise<SendResult> {
    if (!this.transport) {
      addDebugTrace('network', 'sendImage blocked: no transport');
      return {
        ok: false,
        request_id: clientMsgId || `local-${Date.now()}`,
        error: 'not logged in',
        drop: true,
      };
    }

    try {
      addDebugTrace('network', 'sendImage', { mimeType });
      return this.transport.submitUserImage(
        sanitizeBase64(imageBase64),
        mimeType,
        `web-${Date.now()}.${(mimeType.split('/')[1] || 'jpg').replace('jpeg', 'jpg')}`,
        10000,
        clientMsgId,
      );
    } catch {
      addDebugTrace('network', 'sendImage failed');
      return {
        ok: false,
        request_id: clientMsgId || `local-${Date.now()}`,
        error: 'failed to read image file',
        drop: true,
      };
    }
  }

  sendTouch(
    touchArea: string | string[],
    clickFrequency?: Record<string, number>,
    touchMeta?: Record<string, unknown>,
    clientMsgId?: string,
  ): Promise<SendResult> {
    if (!this.transport) {
      addDebugTrace('network', 'sendTouch blocked: no transport');
      return Promise.resolve({
        ok: false,
        request_id: clientMsgId || `local-${Date.now()}`,
        error: 'not logged in',
        drop: true,
      });
    }
    addDebugTrace('network', 'sendTouch', { touchArea });
    return this.transport.submitUserTouch(touchArea, clickFrequency, touchMeta, 10000, clientMsgId);
  }

  sendTypingEvent(textLength: number, clientMsgId?: string): Promise<SendResult> {
    if (!this.transport) {
      addDebugTrace('network', 'sendTyping blocked: no transport');
      return Promise.resolve({
        ok: false,
        request_id: clientMsgId || `local-${Date.now()}`,
        error: 'not logged in',
        drop: true,
      });
    }
    addDebugTrace('network', 'sendTyping', { textLength });
    return this.transport.submitUserTyping(textLength, 10000, clientMsgId);
  }

  sendImageSelecting(clientMsgId?: string): Promise<SendResult> {
    if (!this.transport) {
      addDebugTrace('network', 'sendImageSelecting blocked: no transport');
      return Promise.resolve({
        ok: false,
        request_id: clientMsgId || `local-${Date.now()}`,
        error: 'not logged in',
        drop: true,
      });
    }
    addDebugTrace('network', 'sendImageSelecting');
    return this.transport.submitUserImageSelecting(5000, clientMsgId);
  }

  sendImageSelectingCancel(clientMsgId?: string): Promise<SendResult> {
    if (!this.transport) {
      addDebugTrace('network', 'sendImageSelectingCancel blocked: no transport');
      return Promise.resolve({
        ok: false,
        request_id: clientMsgId || `local-${Date.now()}`,
        error: 'not logged in',
        drop: true,
      });
    }
    addDebugTrace('network', 'sendImageSelectingCancel');
    return this.transport.submitUserImageSelectingCancel(5000, clientMsgId);
  }
}
