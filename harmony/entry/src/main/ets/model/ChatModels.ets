/** 聊天消息模型，对齐 app/types/chat.ts */

export type MessageType = 'text' | 'image' | 'sing' | 'system';
export type SendStatus = 'waiting' | 'submitted' | 'failed';
export type AudioPlayState = 'idle' | 'playing';

/** 聊天消息（UI 层展示模型） */
export interface ChatMessage {
  uuid: string;
  type: MessageType;
  content: string;
  isUser: boolean;
  timestamp: number;
  sendStatus: SendStatus;
  audioAvailable: boolean;
  audioLocalUri: string;
  audioPlayState: AudioPlayState;
}

/** 服务端 agent_message payload，对齐 AgentMessagePayload */
export interface AgentMessagePayload {
  uuid?: string;
  text?: string;
  audio?: string | null;
  expression?: string | null;
  is_final_package?: boolean;
  audio_error?: boolean;
  error_code?: string | null;
  display_in_chat?: boolean;
  is_ephemeral?: boolean;
}

/** 系统消息辅助构造 */
export function createSystemMessage(text: string, uuid: string, timestamp: number = Date.now()): ChatMessage {
  const m: ChatMessage = {
    uuid: uuid,
    type: 'system',
    content: text,
    isUser: false,
    timestamp: timestamp,
    sendStatus: 'submitted' as SendStatus,
    audioAvailable: false,
    audioLocalUri: '',
    audioPlayState: 'idle' as AudioPlayState,
  };
  return m;
}
