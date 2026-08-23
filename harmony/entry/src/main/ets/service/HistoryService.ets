import { HttpClient } from './HttpClient';
import { SecurityStore } from './SecurityStore';
import { AuthService } from './AuthService';
import { ChatMessage } from '../model/ChatModels';
import { LOAD_HISTORY_COUNT } from '../common/Constants';
import { http } from '@kit.NetworkKit';
import { hilog } from '@kit.PerformanceAnalysisKit';

const TAG = 'HistoryService';

/** 服务端 history 原始消息结构（对齐 app getHistory） */
interface HistoryRawMessage {
  uuid: string;
  content: string;
  source: string;
  type: string;
  timestamp: number;
}

/** 历史响应 */
export interface HistoryResponse {
  messages: ChatMessage[];
  startIndex: number;
}

/** 转换为 UI 层 ChatMessage */
function toChatMessage(raw: HistoryRawMessage): ChatMessage {
  const isUser = raw.source === 'user';
  const isSing = raw.type === 'sing';
  return {
    uuid: raw.uuid,
    type: isSing ? 'sing' : (raw.type === 'image' ? 'image' : 'text'),
    content: raw.content,
    isUser: isUser,
    timestamp: raw.timestamp,
    sendStatus: isUser ? 'submitted' : 'submitted',
    audioAvailable: false,
    audioLocalUri: '',
    audioPlayState: 'idle',
  };
}

/**
 * 历史记录服务（对齐 app utils/getHistory.ts）。
 * GET /history?username&count&end_index + Bearer token。
 * 阶段3 简化：音频/图片回连（get_image/attachLocalAudioIfExists）暂缓。
 */
export class HistoryService {
  static async fetchHistory(endIndex: number): Promise<HistoryResponse> {
    const username = AuthService.state.username;
    const token = AuthService.state.messageToken;
    if (username.length === 0 || token.length === 0) {
      return { messages: [], startIndex: 0 };
    }
    try {
      const url = await SecurityStore.getServerUrl();
      const client = new HttpClient(url);
      const path = `/history?username=${encodeURIComponent(username)}&count=${LOAD_HISTORY_COUNT}&end_index=${endIndex}`;
      const r = await client.requestJson(path, http.RequestMethod.GET, null,
        { 'Authorization': `Bearer ${token}` });
      if (!r.ok) {
        hilog.warn(0x0000, TAG, 'history failed: %{public}d', r.status);
        return { messages: [], startIndex: 0 };
      }
      const rawHistory = r.data['history'];
      const startIndex = Number(r.data['start_index'] ?? 0);
      const messages: ChatMessage[] = [];
      if (rawHistory instanceof Array) {
        for (let i = 0; i < rawHistory.length; i++) {
          messages.push(toChatMessage(rawHistory[i] as HistoryRawMessage));
        }
      }
      return { messages: messages, startIndex: startIndex };
    } catch (e) {
      hilog.error(0x0000, TAG, 'history error: %{public}s', (e as Error).message);
      return { messages: [], startIndex: 0 };
    }
  }
}
