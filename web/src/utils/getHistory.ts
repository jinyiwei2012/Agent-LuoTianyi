import { ChatMessage } from '@/types/chat';
import { server_config } from '@/config';
import { addDebugTrace } from './debug_trace';

export interface HistoryResponse {
  messages: ChatMessage[];
  startIndex: number;
}

export interface ImageResponse {
  success: boolean;
  error?: string;
  newClientPath?: string; // Web 版为 data URL / Blob URL
}

/** 服务端 /history 返回的单条消息原始结构 */
interface HistoryRawItem {
  uuid?: string;
  content: string;
  source?: string;
  type: ChatMessage['type'];
  timestamp?: number;
}

// Web 版：无本地文件系统，历史音频不落盘，直接由 message_processor 在线播放，
// 因此不尝试 attach 本地音频路径（见 Web 方案 §7.2）。

export async function getHistory(username: string, token: string, count: number, end_index: number): Promise<HistoryResponse> {
  try {
    const params = new URLSearchParams({
      username: username,
      count: count.toString(),
      end_index: end_index.toString(),
    });
    const url = `${server_config.BASE_URL}/history?${params.toString()}`;
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
    });
    const data = await response.json();
    if (!response.ok) {
      addDebugTrace('history', 'fetch failed', { detail: data.detail || '未知错误' });
      return {
        messages: [],
        startIndex: 0,
      };
    }

    const messages: ChatMessage[] = data.history.map((msg: HistoryRawItem) => {
      return {
        uuid: msg.uuid || 'unknown_id',
        content: msg.content,
        isUser: msg.source === 'user',
        type: msg.type,
        timestamp: msg.timestamp,
      };
    });
    return {
      messages,
      startIndex: data.start_index,
    };
  } catch (error) {
    addDebugTrace('history', 'getHistory error', { error: String(error) });
    return {
      messages: [],
      startIndex: 0,
    };
  }
}

export async function getImage(username: string, token: string, message_id: string): Promise<ImageResponse> {
  try {
    const response = await fetch(`${server_config.BASE_URL}/get_image`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        username: username,
        token,
        uuid: message_id,
      }),
    });

    if (!response.ok) {
      let errorMessage = '获取图片失败';
      try {
        const errorData = await response.json();
        errorMessage = errorData.detail || errorMessage;
      } catch {
        // ignore json parse error and fallback to status text
        errorMessage = response.statusText || errorMessage;
      }
      return {
        success: false,
        error: errorMessage,
      };
    }

    const blob = await response.blob();
    // Web 版：将图片转为 data URL 直接用于显示，无需写入文件系统。
    const dataUrl: string = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onloadend = () => {
        const result = reader.result;
        if (typeof result !== 'string') {
          reject(new Error('图片转换失败'));
          return;
        }
        resolve(result);
      };
      reader.onerror = () => reject(new Error('读取图片失败'));
      reader.readAsDataURL(blob);
    });

    return {
      success: true,
      newClientPath: dataUrl,
    };
  } catch (error) {
    addDebugTrace('history', 'getImage error', { error: String(error) });
    return {
      success: false,
      error: error instanceof Error ? error.message : '获取图片失败',
    };
  }
}

export async function updateImagePath(username: string, token: string, message_id: string, newClientPath: string): Promise<boolean> {
  try {
    const response = await fetch(`${server_config.BASE_URL}/update_image_client_path`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        username: username,
        token: token,
        uuid: message_id,
        image_client_path: newClientPath,
      }),
    });
    if (!response.ok) {
      addDebugTrace('history', 'updateImagePath failed', { status: response.statusText });
      return false;
    }
    return true;
  } catch (error) {
    addDebugTrace('history', 'updateImagePath error', { error: String(error) });
    return false;
  }
}
