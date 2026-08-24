import { useEffect, useMemo, useState } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { ChatPage, type WorkspacePage } from '@/pages/ChatPage';
import { DynamicsPage } from '@/pages/DynamicsPage';
import { LlmSettingsPage } from '@/pages/LlmSettingsPage';
import { LoginPage } from '@/pages/LoginPage';
import { PreferencesPage } from '@/pages/PreferencesPage';
import { COLOR_MODE_STORAGE_KEY, resolveTheme } from '@/utils/theme';
import type { ColorMode } from '@/utils/theme';
import { auth } from '@/components/auth';
import { getDynamicUnreadStatus } from '@/utils/dynamics';
import { addDebugTrace } from '@/utils/debug_trace';

function getStoredColorMode(): ColorMode {
  try {
    const stored = window.localStorage.getItem(COLOR_MODE_STORAGE_KEY);
    return stored === 'dark' || stored === 'system' || stored === 'light' ? stored : 'system';
  } catch {
    return 'system';
  }
}

function LoadingScreen() {
  return <main className="app-loading"><div className="app-loading__mark">洛</div><p>正在打开天依的房间…</p><span className="loading-line" /></main>;
}

export default function App() {
  const authState = useAuth();
  const [colorMode, setColorMode] = useState<ColorMode>(() => getStoredColorMode());
  const [page, setPage] = useState<WorkspacePage>('chat');
  const [unreadCount, setUnreadCount] = useState(0);
  const [systemScheme, setSystemScheme] = useState<'light' | 'dark'>(() => window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const theme = useMemo(() => resolveTheme(colorMode, systemScheme), [colorMode, systemScheme]);

  useEffect(() => {
    window.localStorage.setItem(COLOR_MODE_STORAGE_KEY, colorMode);
    document.documentElement.dataset.theme = theme.name;
    for (const [key, value] of Object.entries(theme)) {
      if (key !== 'name') {
        document.documentElement.style.setProperty(`--color-${key.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`, value);
      }
    }
  }, [colorMode, theme]);

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const listener = (event: MediaQueryListEvent) => setSystemScheme(event.matches ? 'dark' : 'light');
    media.addEventListener('change', listener);
    return () => media.removeEventListener('change', listener);
  }, []);

  useEffect(() => {
    if (!authState.isLoggedIn || !auth.username || !auth.message_token) {
      return;
    }
    const refresh = async () => {
      try {
        const status = await getDynamicUnreadStatus(auth.username, auth.message_token);
        setUnreadCount(status.unread_count);
      } catch (error: unknown) {
        addDebugTrace('dynamics', 'unread refresh failed', { error: String(error) });
        // The feed owns its visible error state; a badge failure should not interrupt chat.
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 60000);
    return () => window.clearInterval(timer);
  }, [authState.isLoggedIn]);

  const handleLogout = async () => {
    await authState.logout();
    setPage('chat');
    setUnreadCount(0);
  };

  if (authState.isLoading) {
    return <LoadingScreen />;
  }

  if (!authState.isLoggedIn) {
    return <LoginPage theme={theme} colorMode={colorMode} onColorModeChange={setColorMode} publicKeyLoaded={authState.publicKeyLoaded} login={authState.login} register={authState.register} />;
  }

  const username = auth.username;
  const messageToken = auth.message_token;
  return (
    <div className="authenticated-app">
      <div className="chat-layer" aria-hidden={page !== 'chat'} inert={page !== 'chat'}>
        <ChatPage username={username} messageToken={messageToken} theme={theme} colorMode={colorMode} activePage={page} onColorModeChange={setColorMode} onNavigate={setPage} onLogout={handleLogout} dynamicsUnreadCount={unreadCount} />
      </div>
      {page === 'dynamics' && <div className="subpage-overlay" tabIndex={-1} autoFocus><DynamicsPage username={username} messageToken={messageToken} onBack={() => setPage('chat')} onUnreadCleared={() => setUnreadCount(0)} /></div>}
      {page === 'preferences' && <div className="subpage-overlay" tabIndex={-1} autoFocus><PreferencesPage username={username} messageToken={messageToken} onBack={() => setPage('chat')} /></div>}
      {page === 'llm' && <div className="subpage-overlay" tabIndex={-1} autoFocus><LlmSettingsPage onBack={() => setPage('chat')} /></div>}
    </div>
  );
}
