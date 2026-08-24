import { useState } from 'react';
import type { AppTheme, ColorMode } from '@/utils/theme';
import { COLOR_MODE_LABELS } from '@/utils/theme';
import { clearCachedPublicKey } from '@/utils/crypto';
import { saveCustomServerUrl, server_config } from '@/config';

type AuthResult = { success: boolean; message: string };
type AuthTab = 'login' | 'register';

export interface LoginPageProps {
  theme: AppTheme;
  colorMode: ColorMode;
  onColorModeChange: (mode: ColorMode) => void;
  publicKeyLoaded: boolean;
  login: (username: string, password: string, autoLogin: boolean) => Promise<AuthResult>;
  register: (username: string, password: string, confirmPassword: string, inviteCode: string) => Promise<AuthResult>;
}

const COLOR_MODES: ColorMode[] = ['light', 'dark', 'system'];

export function LoginPage({
  theme,
  colorMode,
  onColorModeChange,
  publicKeyLoaded,
  login,
  register,
}: LoginPageProps) {
  const [tab, setTab] = useState<AuthTab>('login');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: 'error' | 'success'; text: string } | null>(null);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [autoLogin, setAutoLogin] = useState(true);
  const [registerUsername, setRegisterUsername] = useState('');
  const [registerPassword, setRegisterPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [inviteCode, setInviteCode] = useState('');
  const [serverUrl, setServerUrl] = useState(server_config.BASE_URL);
  const [showServer, setShowServer] = useState(false);

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    const result = tab === 'login'
      ? await login(username.trim(), password, autoLogin)
      : await register(registerUsername.trim(), registerPassword, confirmPassword, inviteCode.trim());
    setBusy(false);
    setNotice({ tone: result.success ? 'success' : 'error', text: result.message });
    if (result.success && tab === 'register') {
      setTab('login');
      setUsername(registerUsername.trim());
      setPassword('');
    }
  };

  const saveServer = () => {
    const nextUrl = serverUrl.trim().replace(/\/+$/, '');
    if (!nextUrl) {
      setNotice({ tone: 'error', text: '服务器地址不能为空' });
      return;
    }
    if (!URL.canParse(nextUrl)) {
      setNotice({ tone: 'error', text: '请输入完整的服务器地址，例如 https://example.com' });
      return;
    }
    saveCustomServerUrl(nextUrl);
    clearCachedPublicKey();
    setServerUrl(nextUrl);
    setShowServer(false);
    setNotice({ tone: 'success', text: '服务器地址已保存，下次请求会使用新地址' });
  };

  const serverHost = URL.canParse(server_config.BASE_URL) ? new URL(server_config.BASE_URL).host : server_config.BASE_URL;

  return (
    <main className="auth-page" style={{ backgroundColor: theme.root }}>
      <div className="auth-page__aurora auth-page__aurora--one" aria-hidden="true" />
      <div className="auth-page__aurora auth-page__aurora--two" aria-hidden="true" />
      <div className="auth-page__shell">
        <section className="auth-page__intro" aria-labelledby="auth-title">
          <div className="brand-mark" aria-hidden="true"><span>洛</span></div>
          <p className="eyebrow">AGENTLUO / WEB STAGE</p>
          <h1 id="auth-title">和天依，<br /><em>聊点真心话。</em></h1>
          <p className="auth-page__lead">一个为洛天依准备的安静房间。文字、<span className="nowrap">歌声和表情</span>，都在这里相遇。</p>
          <div className="auth-page__signal"><span className="status-dot status-dot--live" />Live2D 舞台已准备</div>
        </section>

        <section className="auth-card" aria-label={tab === 'login' ? '登录' : '注册'}>
          <div className="auth-card__topline">
            <div>
              <p className="eyebrow">WELCOME BACK</p>
              <h2>{tab === 'login' ? '欢迎回来' : '创建你的房间'}</h2>
            </div>
            <button className="icon-button" type="button" onClick={() => setShowServer(true)} aria-label="配置服务器地址">设置</button>
          </div>
          <div className="auth-tabs" role="tablist" aria-label="账号操作">
            <button type="button" role="tab" aria-selected={tab === 'login'} className={tab === 'login' ? 'is-active' : ''} onClick={() => { setTab('login'); setNotice(null); }}>登录</button>
            <button type="button" role="tab" aria-selected={tab === 'register'} className={tab === 'register' ? 'is-active' : ''} onClick={() => { setTab('register'); setNotice(null); }}>注册</button>
          </div>

          <form className="form-stack" onSubmit={submit}>
            {tab === 'login' ? (
              <>
                <label className="field"><span>用户名</span><input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" placeholder="输入你的用户名" /></label>
                <label className="field"><span>密码</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" placeholder="输入密码" /></label>
                <label className="check-field"><input type="checkbox" checked={autoLogin} onChange={(event) => setAutoLogin(event.target.checked)} /><span>记住我，下次自动登录</span></label>
              </>
            ) : (
              <>
                <label className="field"><span>用户名</span><input value={registerUsername} onChange={(event) => setRegisterUsername(event.target.value)} autoComplete="username" placeholder="给自己取一个名字" /></label>
                <label className="field"><span>密码</span><input type="password" value={registerPassword} onChange={(event) => setRegisterPassword(event.target.value)} autoComplete="new-password" placeholder="至少记得它" /></label>
                <label className="field"><span>确认密码</span><input type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} autoComplete="new-password" placeholder="再输入一次密码" /></label>
                <label className="field"><span>邀请码</span><input value={inviteCode} onChange={(event) => setInviteCode(event.target.value)} autoComplete="off" placeholder="向管理员获取邀请码" /></label>
              </>
            )}
            {notice && <p className={`form-notice form-notice--${notice.tone}`} role="status">{notice.text}</p>}
            <button className="primary-button primary-button--wide" type="submit" disabled={busy}>
              {busy ? '正在连接…' : !publicKeyLoaded ? (tab === 'login' ? '连接并进入房间' : '连接并创建账号') : tab === 'login' ? '进入房间' : '创建账号'}
            </button>
          </form>
          <div className="auth-card__footer">
            <span>当前服务器：{serverHost}</span>
            <label className="theme-select"><span>主题</span><select value={colorMode} onChange={(event) => onColorModeChange(event.target.value as ColorMode)}>{COLOR_MODES.map((mode) => <option key={mode} value={mode}>{COLOR_MODE_LABELS[mode]}</option>)}</select></label>
          </div>
        </section>
      </div>

      {showServer && <div className="modal-backdrop" role="presentation" onMouseDown={() => setShowServer(false)}><section className="modal-card" role="dialog" aria-modal="true" aria-labelledby="server-title" onMouseDown={(event) => event.stopPropagation()}><div className="modal-card__header"><div><p className="eyebrow">CONNECTION</p><h2 id="server-title">服务器地址</h2></div><button className="text-button" type="button" onClick={() => setShowServer(false)}>关闭</button></div><label className="field"><span>FastAPI 服务地址</span><input value={serverUrl} onChange={(event) => setServerUrl(event.target.value)} inputMode="url" /></label><p className="field-hint">修改后会清除旧公钥缓存，并从新地址重新获取安全密钥。</p><div className="modal-card__actions"><button className="secondary-button" type="button" onClick={() => setShowServer(false)}>取消</button><button className="primary-button" type="button" onClick={saveServer}>保存地址</button></div></section></div>}
    </main>
  );
}
