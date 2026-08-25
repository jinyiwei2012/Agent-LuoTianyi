import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AgentBinder } from '@/utils/binder';
import { MessageProcessor } from '@/utils/message_processor';
import { NetworkClient } from '@/utils/network_client';
import { ServerAudioPlayer } from '@/utils/server_audio_player';
import { AgentMessagePayload, ChatMessage, createSystemChatMessage } from '@/types/chat';
import { addDebugTrace } from '@/utils/debug_trace';

function createUuid(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export interface UseChatLogicOptions {
  /** 收到天依表情指令时回调（供 Live2D 组件消费） */
  onExpression?: (expression: string) => void;
}

export const useChatLogic = (
  username: string,
  messageToken: string,
  options: UseChatLogicOptions = {},
) => {
  const [inputText, setInputText] = useState('');
  const [thinking, setThinking] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [currentPlayingUuid, setCurrentPlayingUuid] = useState<string | null>(null);

  const networkClientRef = useRef<NetworkClient | null>(null);
  const binderRef = useRef<AgentBinder | null>(null);
  const messageProcessorRef = useRef<MessageProcessor | null>(null);
  const serverAudioPlayerRef = useRef<ServerAudioPlayer | null>(null);
  const onExpressionRef = useRef(options.onExpression);
  // 通过 effect 同步最新回调，避免 render 期间写 ref
  useEffect(() => {
    onExpressionRef.current = options.onExpression;
  }, [options.onExpression]);

  const updateMessageByUuid = useCallback((uuid: string, updater: (msg: ChatMessage) => ChatMessage) => {
    setMessages((prev) => prev.map((msg) => (msg.uuid === uuid ? updater(msg) : msg)));
  }, []);

  const appendOrMergeAgentMessage = useCallback((payload: AgentMessagePayload) => {
    const convUuid = payload.uuid || createUuid('agent');
    const expression = payload.expression;
    if (expression) {
      onExpressionRef.current?.(expression);
    }

    setMessages((prev) => {
      const index = prev.findIndex((msg) => msg.uuid === convUuid && !msg.isUser);

      if (payload.display_in_chat === false) {
        return prev;
      }

      // Some packets only carry state/expression updates; do not render empty bubbles.
      if (!payload.text && !payload.audio && index < 0) {
        return prev;
      }

      if (index >= 0) {
        const target = prev[index];
        const merged: ChatMessage = {
          ...target,
          // 服务端约定文本只在每句话的首包携带（global_speaking_worker 首个分片带 text）。
          // 同 uuid 的重复文本包（at-least-once 重发）直接忽略，避免"同一句话显示两次"。
          content: payload.text && !target.content ? payload.text : target.content,
          audioAvailable: payload.audio ? true : target.audioAvailable,
          audioBase64: payload.audio || target.audioBase64,
        };
        const next = [...prev];
        next[index] = merged;
        return next;
      }

      const newMsg: ChatMessage = {
        uuid: convUuid,
        type: 'text',
        content: payload.text || '',
        isUser: false,
        timestamp: Date.now(),
        audioAvailable: !!payload.audio,
        audioBase64: payload.audio || undefined,
        audioPlayState: 'idle',
      };

      return [newMsg, ...prev];
    });
  }, []);

  const appendSystemMessage = useCallback((text: string) => {
    if (!text) {
      return;
    }
    const message = createSystemChatMessage(text, createUuid('system'));
    setMessages((prev) => [message, ...prev]);
  }, []);

  useEffect(() => {
    if (!username || !messageToken) {
      return;
    }

    const networkClient = new NetworkClient();
    networkClientRef.current = networkClient;

    const binder = new AgentBinder(
      {
        sendText: async (uuid, text) => {
          await messageProcessorRef.current?.sendText(uuid, text);
        },
        sendImage: async (uuid, imageBase64, mimeType) => {
          await messageProcessorRef.current?.sendImage(uuid, imageBase64, mimeType);
        },
        sendProactiveText: async (uuid, text) => {
          await messageProcessorRef.current?.sendProactiveText(uuid, text);
        },
        sendTouch: async (touchArea, clickFrequency, touchMeta) => {
          await messageProcessorRef.current?.sendTouch(touchArea, clickFrequency, touchMeta);
        },
        sendTyping: async (textLength) => {
          await messageProcessorRef.current?.sendTypingEvent(textLength);
        },
        sendImageSelecting: async () => {
          await messageProcessorRef.current?.sendImageSelecting();
        },
        sendImageSelectingCancel: async () => {
          await messageProcessorRef.current?.sendImageSelectingCancel();
        },
        playLocalTts: async (convUuid) => {
          addDebugTrace('audio-ui', 'binder playLocalTts called', { convUuid });
          return (await messageProcessorRef.current?.playLocalTtsByUuid(convUuid)) || false;
        },
        stopLocalTts: async () => {
          addDebugTrace('audio-ui', 'binder stopLocalTts called');
          await messageProcessorRef.current?.stopLocalTts();
        },
      },
      {
        onAgentMessage: (payload) => {
          appendOrMergeAgentMessage(payload);
        },
        onMessageStatus: (uuid, status) => {
          addDebugTrace('ui', 'message status update', { uuid, status });
          updateMessageByUuid(uuid, (msg) => ({ ...msg, sendStatus: status }));
        },
        onAgentThinking: (isThinking) => {
          setThinking(isThinking);
        },
        onLocalTtsState: (_event, convUuid) => {
          updateMessageByUuid(convUuid, (msg) => ({ ...msg, audioPlayState: 'idle' }));
          setCurrentPlayingUuid((prev) => (prev === convUuid ? null : prev));
        },
        onErrorText: (text) => {
          addDebugTrace('ui', 'error text', { text });
          appendSystemMessage(text);
        },
      },
    );

    binderRef.current = binder;

    // Web 服务器音频播放器：替代移动端 WebView 的 feedAudioChunk / stopServerAudio 桥
    const serverAudioPlayer = new ServerAudioPlayer({
      onFinished: () => {
        messageProcessorRef.current?.onServerAudioFinished();
      },
    });
    serverAudioPlayerRef.current = serverAudioPlayer;

    const processor = new MessageProcessor(
      networkClient,
      binder,
      (base64Audio, isFinal) => {
        serverAudioPlayer.feedChunk(base64Audio, isFinal);
      },
      () => {
        serverAudioPlayer.stop();
      },
    );

    messageProcessorRef.current = processor;

    networkClient.connectWs(username, messageToken, {
      onAgentMessage: (payload) => {
        processor.onAgentMessage(payload);
      },
      onAgentStateChanged: (state) => {
        processor.onAgentStateChanged(state);
      },
      onError: (errorText) => {
        binder.emitErrorText(errorText);
      },
      onLlmRequest: (payload) => processor.processLlmRequest(payload),
      getLlmMode: () => processor.getLlmMode(),
    });

    return () => {
      processor.stop();
      networkClient.disconnectWs();
      serverAudioPlayer.stop();
      messageProcessorRef.current = null;
      binderRef.current = null;
      networkClientRef.current = null;
      serverAudioPlayerRef.current = null;
    };
  }, [appendOrMergeAgentMessage, appendSystemMessage, messageToken, updateMessageByUuid, username]);

  const canSend = useMemo(() => inputText.trim().length > 0, [inputText]);
  const canSendImage = true;

  const handleInputChange = useCallback((text: string) => {
    setInputText(text);
    const trimmedLength = text.trim().length;
    // 清空输入时也发送 text_length=0 事件，通知服务端"用户已清空输入"并立即提取，而非继续等待补全
    void binderRef.current?.sendTyping(trimmedLength);
  }, []);

  const handleSendText = useCallback(async () => {
    if (!canSend) {
      return;
    }

    const uuid = createUuid('user');
    const text = inputText;
    setInputText('');
    addDebugTrace('ui', 'send text tapped', { uuid, textLength: text.length });

    setMessages((prev) => [
      {
        uuid,
        type: 'text',
        content: text,
        isUser: true,
        timestamp: Date.now(),
        sendStatus: 'waiting',
      },
      ...prev,
    ]);

    await binderRef.current?.sendText(uuid, text);
  }, [canSend, inputText]);

  /** Web 版：UI 层把用户选择的 File 传进来，这里用 FileReader 读为 base64 再发送。 */
  const handleSendImageFromFile = useCallback(async (file: File) => {
    // 通知服务端用户开始选择图片，延长等待时间
    await binderRef.current?.sendImageSelecting();

    const mimeType = file.type || 'image/jpeg';
    const base64 = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onloadend = () => {
        const result = reader.result;
        if (typeof result === 'string') {
          resolve(result);
        } else {
          reject(new Error('读取图片失败'));
        }
      };
      reader.onerror = () => reject(new Error('读取图片失败'));
      reader.readAsDataURL(file);
    });

    const uuid = createUuid('user-img');
    addDebugTrace('ui', 'send image selected', { uuid, mimeType, size: base64.length });

    setMessages((prev) => [
      {
        uuid,
        type: 'image',
        content: base64,
        isUser: true,
        timestamp: Date.now(),
        sendStatus: 'waiting',
      },
      ...prev,
    ]);

    await binderRef.current?.sendImage(uuid, base64, mimeType);
  }, []);

  const handleToggleAgentAudio = useCallback(
    async (uuid: string) => {
      addDebugTrace('audio-ui', 'tap audio button', { uuid, currentPlayingUuid });
      const target = messages.find((msg) => msg.uuid === uuid && !msg.isUser);
      if (!target || !target.audioAvailable) {
        addDebugTrace('audio-ui', 'tap ignored: target missing or audio unavailable', {
          uuid,
          found: !!target,
          audioAvailable: target?.audioAvailable,
        });
        return;
      }

      addDebugTrace('audio-ui', 'audio target resolved', {
        uuid,
        audioBase64Length: target.audioBase64?.length,
        audioAvailable: target.audioAvailable,
      });

      if (target.audioBase64) {
        messageProcessorRef.current?.setLocalAudioPath(uuid, target.audioBase64);
      }

      if (currentPlayingUuid === uuid) {
        await binderRef.current?.stopLocalTts();
        return;
      }

      const ok = await binderRef.current?.playLocalTts(uuid);
      if (!ok) {
        addDebugTrace('audio-ui', 'playLocalTts returned false', { uuid });
        return;
      }

      if (currentPlayingUuid) {
        updateMessageByUuid(currentPlayingUuid, (msg) => ({ ...msg, audioPlayState: 'idle' }));
      }

      updateMessageByUuid(uuid, (msg) => ({ ...msg, audioPlayState: 'playing' }));
      setCurrentPlayingUuid(uuid);
    },
    [currentPlayingUuid, messages, updateMessageByUuid],
  );

  const addHistoryMessage = useCallback((newMessages: ChatMessage[]) => {
    for (const msg of newMessages) {
      if (!msg.isUser && msg.audioAvailable && msg.audioBase64) {
        messageProcessorRef.current?.setLocalAudioPath(msg.uuid, msg.audioBase64);
      }
    }

    setMessages((prev) => {
      // 按 uuid 去重：历史消息与实时消息（或分页重叠）可能包含同一条消息，避免重复渲染
      const existingUuids = new Set(prev.map((msg) => msg.uuid));
      const normalized = newMessages
        .filter((msg) => !existingUuids.has(msg.uuid))
        .map((msg) => ({
          ...msg,
          sendStatus: msg.isUser ? 'submitted' : msg.sendStatus,
          audioPlayState: msg.audioPlayState || 'idle',
        }));
      return [...prev, ...normalized.reverse()];
    });
  }, []);

  return {
    inputText,
    messages,
    canSend,
    canSendImage,
    thinking,
    setInputText: handleInputChange,
    addHistoryMessage,
    handleSendText,
    handleSendImageFromFile,
    handleToggleAgentAudio,
  };
};
