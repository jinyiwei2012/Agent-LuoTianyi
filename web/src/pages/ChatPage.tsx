import { useEffect, useMemo, useRef, useState } from 'react';
import { Live2DView, type Live2DViewHandle } from '@/components/Live2DView';
import { useAffection } from '@/hooks/useAffection';
import { useChatLogic } from '@/hooks/useChatLogic';
import { useHistoryLogic } from '@/hooks/useHistoryLogic';
import type { ChatMessage } from '@/types/chat';
import type { AppTheme, ColorMode } from '@/utils/theme';
import { COLOR_MODE_LABELS } from '@/utils/theme';
import { clearDebugTrace, subscribeDebugTrace } from '@/utils/debug_trace';
import type { DebugTraceEntry } from '@/utils/debug_trace';
import { addDebugTrace } from '@/utils/debug_trace';

export type WorkspacePage = 'chat' | 'dynamics' | 'preferences' | 'llm';

export interface ChatPageProps {
  username: string;
  messageToken: string;
  theme: AppTheme;
  colorMode: ColorMode;
  activePage?: WorkspacePage;
  onColorModeChange: (mode: ColorMode) => void;
  onNavigate: (page: WorkspacePage) => void;
  onLogout: () => void;
  dynamicsUnreadCount: number;
}

const MESSAGE_TYPES = {
  text: 'text',
  image: 'image',
  sing: 'sing',
  system: 'system',
} as const;

function formatTime(timestamp?: number): string {
  if (!timestamp) {
    return '';
  }
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(timestamp);
}

function messageLabel(message: ChatMessage): string {
  if (message.type === MESSAGE_TYPES.image) {
    return message.isUser ? '你发送了一张图片' : '天依分享了一张图片';
  }
  if (message.type === MESSAGE_TYPES.sing) {
    return '天依正在唱歌';
  }
  if (message.type === MESSAGE_TYPES.system) {
    return '系统消息';
  }
  return message.content;
}

function sendStatusLabel(status: ChatMessage['sendStatus']): string {
  if (status === 'waiting') {
    return '发送中';
  }
  if (status === 'failed') {
    return '发送失败';
  }
  return '已送达';
}

function ChatMessageItem({ message, onAudio }: { message: ChatMessage; onAudio: (uuid: string) => void }) {
  const isAgent = !message.isUser && message.type !== MESSAGE_TYPES.system;
  return (
    <article className={`message-row ${message.isUser ? 'message-row--user' : 'message-row--agent'} ${message.type === MESSAGE_TYPES.system ? 'message-row--system' : ''}`}>
      <div className="message-avatar" aria-hidden="true">{message.isUser ? '你' : message.type === MESSAGE_TYPES.system ? '·' : '洛'}</div>
      <div className="message-column">
        <div className="message-meta"><span>{message.isUser ? '你' : message.type === MESSAGE_TYPES.system ? '系统' : '洛天依'}</span>{message.timestamp && <time dateTime={new Date(message.timestamp).toISOString()}>{formatTime(message.timestamp)}</time>}</div>
        <div className="message-bubble" data-owner={message.isUser ? 'user' : isAgent ? 'agent' : 'system'}>
          {message.type === MESSAGE_TYPES.image ? <img className="message-image" src={message.content} alt={messageLabel(message)} /> : <p>{message.content || (message.type === MESSAGE_TYPES.sing ? '♪' : '…')}</p>}
          <div className="message-actions">
            {isAgent && message.audioAvailable && <button className="message-action" type="button" onClick={() => onAudio(message.uuid)} aria-label={message.audioPlayState === 'playing' ? '停止播放语音' : '播放语音'}>{message.audioPlayState === 'playing' ? '停止' : '播放语音'}</button>}
            {message.isUser && message.sendStatus && <span className="message-status" data-status={message.sendStatus}>{sendStatusLabel(message.sendStatus)}</span>}
          </div>
        </div>
      </div>
    </article>
  );
}

function DebugPanel({ theme, entries, onClear, onClose }: { theme: AppTheme; entries: DebugTraceEntry[]; onClear: () => void; onClose: () => void }) {
  return (
    <aside className="debug-panel" style={{ backgroundColor: theme.debugBackground, borderColor: theme.debugBorder }} aria-label="调试日志">
      <div className="debug-panel__header"><div><span className="eyebrow" style={{ color: theme.debugHeader }}>TRACE CONSOLE</span><h2 style={{ color: theme.debugHeader }}>调试日志</h2></div><div className="cluster"><button className="debug-button" type="button" onClick={onClear}>清空</button><button className="debug-button" type="button" onClick={onClose}>关闭</button></div></div>
      <div className="debug-panel__body">{entries.length === 0 ? <p className="debug-empty" style={{ color: theme.debugText }}>暂无日志。连接、历史加载和模型事件会显示在这里。</p> : entries.slice().reverse().map((entry) => <div className="debug-entry" key={entry.id}><div><strong style={{ color: theme.debugAction }}>{entry.scope}</strong><time style={{ color: theme.debugText }}>{formatTime(entry.ts)}</time></div><p style={{ color: theme.debugText }}>{entry.message}</p>{entry.detail && <code style={{ color: theme.debugText }}>{entry.detail}</code>}</div>)}</div>
    </aside>
  );
}

export function ChatPage({ username, messageToken, theme, colorMode, activePage = 'chat', onColorModeChange, onNavigate, onLogout, dynamicsUnreadCount }: ChatPageProps) {
  const live2dRef = useRef<Live2DViewHandle>(null);
  const messageScrollRef = useRef<HTMLDivElement>(null);
  const historyLoadedRef = useRef(false);
  const shouldStickToBottomRef = useRef(true);
  const [menuOpen, setMenuOpen] = useState(false);
  const [debugOpen, setDebugOpen] = useState(false);
  const [debugEntries, setDebugEntries] = useState<DebugTraceEntry[]>([]);
  const [fileInputKey, setFileInputKey] = useState(0);
  const [inputFocused, setInputFocused] = useState(false);
  const { affection, refreshAffection } = useAffection(username, messageToken);
  const {
    inputText,
    messages,
    canSend,
    canSendImage,
    thinking,
    setInputText,
    addHistoryMessage,
    handleSendText,
    handleSendImageFromFile,
    handleToggleAgentAudio,
  } = useChatLogic(username, messageToken, { onExpression: (expression) => live2dRef.current?.setExpression(expression) });
  const { historyLoading, loadHistory } = useHistoryLogic(addHistoryMessage);
  const displayMessages = useMemo(() => messages.slice().reverse(), [messages]);

  useEffect(() => subscribeDebugTrace(setDebugEntries), []);

  useEffect(() => {
    if (historyLoadedRef.current) {
      return;
    }
    historyLoadedRef.current = true;
    void loadHistory(username, messageToken);
  }, [loadHistory, messageToken, username]);

  useEffect(() => {
    const scroll = messageScrollRef.current;
    if (scroll && shouldStickToBottomRef.current) {
      scroll.scrollTop = scroll.scrollHeight;
    }
  }, [messages.length]);

  const handleScroll = () => {
    const scroll = messageScrollRef.current;
    if (!scroll) {
      return;
    }
    const distanceFromBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight;
    shouldStickToBottomRef.current = distanceFromBottom < 80;
    if (scroll.scrollTop < 80 && !historyLoading) {
      void loadHistory(username, messageToken);
    }
  };

  const handleSend = async () => {
    shouldStickToBottomRef.current = true;
    await handleSendText();
  };

  const handleInputKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (canSend && !thinking) {
        void handleSend();
      }
    }
  };

  const handleFileChange = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    setFileInputKey((key) => key + 1);
    if (!file || !canSendImage) {
      return;
    }
    shouldStickToBottomRef.current = true;
    try {
      await handleSendImageFromFile(file);
    } catch (error) {
      const message = error instanceof Error ? error.message : '图片发送失败';
      addDebugTrace('ui', 'image send failed', { error: message });
    }
  };

  const affectionPercent = affection ? Math.min(100, Math.max(0, affection.score)) : 0;

  return (
    <div className="workspace-shell">
      <aside className="workspace-rail">
        <div className="workspace-rail__brand"><div className="brand-mark brand-mark--small" aria-hidden="true">洛</div><div><strong>AgentLuo</strong><span>WEB STAGE</span></div></div>
        <nav className="workspace-nav" aria-label="主导航">
          <button className={`workspace-nav__item ${activePage === 'chat' ? 'is-active' : ''}`} type="button" onClick={() => onNavigate('chat')}><span aria-hidden="true">对话</span><span>和天依聊天</span></button>
          <button className={`workspace-nav__item ${activePage === 'dynamics' ? 'is-active' : ''}`} type="button" onClick={() => onNavigate('dynamics')}><span aria-hidden="true">动态</span><span>天依动态</span>{dynamicsUnreadCount > 0 && <b className="nav-badge" aria-label={`${dynamicsUnreadCount} 条未读`}>{dynamicsUnreadCount > 99 ? '99+' : dynamicsUnreadCount}</b>}</button>
          <button className={`workspace-nav__item ${activePage === 'preferences' ? 'is-active' : ''}`} type="button" onClick={() => onNavigate('preferences')}><span aria-hidden="true">偏好</span><span>相处方式</span></button>
          <button className={`workspace-nav__item ${activePage === 'llm' ? 'is-active' : ''}`} type="button" onClick={() => onNavigate('llm')}><span aria-hidden="true">模型</span><span>模型设置</span></button>
        </nav>
        <div className="workspace-rail__footer"><div className="affection-mini"><div className="affection-mini__top"><span>今日好感</span><strong>{affection ? affection.score : '--'}</strong></div><div className="meter"><span style={{ width: `${affectionPercent}%` }} /></div><small>{affection?.level_cn ?? '正在读取'}</small></div><button className="workspace-nav__item workspace-nav__item--quiet" type="button" onClick={() => setDebugOpen(true)}><span aria-hidden="true">日志</span><span>调试面板</span></button><button className="workspace-nav__item workspace-nav__item--quiet" type="button" onClick={() => void onLogout()}><span aria-hidden="true">退出</span><span>退出登录</span></button></div>
      </aside>

      <section className="live2d-panel"><Live2DView ref={live2dRef} className="live2d-panel__view" /><div className="live2d-panel__note"><span className="status-dot status-dot--live" />点击表情会由对话自动驱动</div></section>

      <main className="chat-workspace">
        <header className="workspace-header"><div><p className="eyebrow">PRIVATE SESSION</p><h1>和洛天依聊天</h1></div><div className="workspace-header__actions"><button className="theme-control" type="button" onClick={() => setMenuOpen((open) => !open)} aria-expanded={menuOpen} aria-haspopup="menu">{username}<span aria-hidden="true">⌄</span></button>{menuOpen && <div className="workspace-menu" role="menu"><div className="workspace-menu__user"><span className="status-dot status-dot--live" />{affection ? `${affection.level_cn} · ${affection.score} 分` : '好感度读取中'}</div><label className="theme-select"><span>主题</span><select value={colorMode} onChange={(event) => onColorModeChange(event.target.value as ColorMode)}>{(['light', 'dark', 'system'] as ColorMode[]).map((mode) => <option key={mode} value={mode}>{COLOR_MODE_LABELS[mode]}</option>)}</select></label><button type="button" onClick={() => { setMenuOpen(false); void refreshAffection(); }}>刷新好感度</button><button type="button" onClick={() => { setMenuOpen(false); setDebugOpen(true); }}>打开调试面板</button></div>}</div></header>
        <div className="chat-status-line"><span className="status-dot status-dot--live" />在线 · WebSocket 会话已建立 <span className="chat-status-line__hint">{inputFocused ? '按 Enter 发送，Shift + Enter 换行' : '今天也要好好聊天'}</span></div>
        <div className="message-scroll" ref={messageScrollRef} onScroll={handleScroll} aria-live="polite" aria-label="聊天消息">
          {historyLoading && <div className="history-loader" role="status">正在加载更早的聊天记录…</div>}
          {displayMessages.length === 0 ? <div className="chat-empty"><div className="chat-empty__mark">洛</div><h2>今天想和天依聊什么？</h2><p>可以从一句问候开始，也可以发一张图片给她看看。</p></div> : <div className="message-list">{displayMessages.map((message) => <ChatMessageItem key={message.uuid} message={message} onAudio={(uuid) => void handleToggleAgentAudio(uuid)} />)}</div>}
          {thinking && <div className="thinking-row" role="status"><div className="message-avatar" aria-hidden="true">洛</div><div className="thinking-bubble"><span /> <span /> <span /><em>天依正在想</em></div></div>}
        </div>
        <footer className="composer"><div className="composer__hint">{inputText.length > 0 ? `${inputText.length} 字` : '输入消息'}</div><div className={`composer__box ${inputFocused ? 'is-focused' : ''}`}><textarea value={inputText} onChange={(event) => setInputText(event.target.value)} onKeyDown={handleInputKeyDown} onFocus={() => setInputFocused(true)} onBlur={() => setInputFocused(false)} rows={1} placeholder="写下你想说的话…" aria-label="消息输入框" /><div className="composer__actions"><label className="secondary-button secondary-button--compact" aria-label="发送图片"><span aria-hidden="true">图片</span><input key={fileInputKey} type="file" accept="image/*" onChange={(event) => void handleFileChange(event)} /></label><button className="primary-button primary-button--send" type="button" disabled={!canSend || thinking} onClick={() => void handleSend()}>{thinking ? '等待中' : '发送'}</button></div></div></footer>
      </main>
      {debugOpen && <DebugPanel theme={theme} entries={debugEntries} onClear={clearDebugTrace} onClose={() => setDebugOpen(false)} />}
    </div>
  );
}
