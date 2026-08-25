import { HttpClient } from './HttpClient';
import { SecurityStore } from './SecurityStore';
import { AuthService } from './AuthService';
import { ChatMessage } from '../model/ChatModels';
import { LOAD_HISTORY_COUNT } from '../common/Constants';
import { http } from '@kit.NetworkKit';
import { hilog } from '@kit.PerformanceAnalysisKit';

const TAG = 'HistoryService';
/** 本地历史缓存 key 前缀（按用户名隔离） */
const HISTORY_CACHE_KEY_PREFIX = 'chat_history_cache:';
/** 本地缓存最多保留的消息条数（防止 preferences 膨胀） */
const CACHE_KEEP_COUNT = 100;

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

  /**
   * 读取本地缓存的历史消息（冷启动秒显用，随后仍以服务端数据为准刷新）。
   * 缓存按用户名隔离；损坏时静默返回空。
   */
  static async getCachedMessages(username: string): Promise<ChatMessage[]> {
    if (username.length === 0) {
      return [];
    }
    try {
      const raw = await SecurityStore.getString(HISTORY_CACHE_KEY_PREFIX + username, '');
      if (raw.length === 0) {
        return [];
      }
      const parsed = JSON.parse(raw) as ChatMessage[];
      return parsed instanceof Array ? parsed : [];
    } catch (e) {
      hilog.debug(0x0000, TAG, 'cache read failed: %{public}s', (e as Error).message);
      return [];
    }
  }

  /**
   * 把服务端拉到的历史写入本地缓存（仅保留最近 CACHE_KEEP_COUNT 条）。
   * 写入失败不影响主流程。
   */
  static async saveCachedMessages(username: string, messages: ChatMessage[]): Promise<void> {
    if (username.length === 0 || messages.length === 0) {
      return;
    }
    try {
      const keep = messages.slice(-CACHE_KEEP_COUNT);
      await SecurityStore.setString(HISTORY_CACHE_KEY_PREFIX + username, JSON.stringify(keep));
    } catch (e) {
      hilog.debug(0x0000, TAG, 'cache write failed: %{public}s', (e as Error).message);
    }
  }
}
