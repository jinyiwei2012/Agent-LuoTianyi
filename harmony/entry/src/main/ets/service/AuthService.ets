import { hilog } from '@kit.PerformanceAnalysisKit';
import { HttpClient } from './HttpClient';
import { SecurityStore } from './SecurityStore';
import { encryptPasswordWithPem } from './CryptoUtil';

const TAG = 'AuthService';

/** 认证状态（app 端 auth 单例 + useAuth 状态合成） */
export class AuthState {
  isLoggedIn: boolean = false;
  isLoading: boolean = false;
  username: string = '';
  messageToken: string = '';
  publicKeyLoaded: boolean = false;
}

export interface AuthOutcome {
  success: boolean;
  message: string;
}

/** 加密失败描述，对齐 app 端 describeEncryptFailure 文案 */
function describeEncryptFailure(failure: string): string {
  return `无法加密密码（${failure}），请检查网络或服务器配置`;
}

/**
 * 认证服务：登录/注册/自动登录/登出。
 * 对齐 app hooks/useAuth.ts 流程（RSA 加密 -> REST -> 存储 -> 状态）。
 */
export class AuthService {
  private static http: HttpClient | null = null;
  private static cachedPublicKeyPem: string | null = null;

  static state: AuthState = new AuthState();

  private static getHttp(): HttpClient {
    // URL 变化可能发生（设置页），每次调用时从 store 读取最新地址
    return new HttpClient('');
  }

  private static async withHttp<T>(fn: (client: HttpClient) => Promise<T>): Promise<T> {
    const url = await SecurityStore.getServerUrl();
    const client = new HttpClient(url);
    return await fn(client);
  }

  /** 获取并缓存公钥（对齐 getPublicKey + fetchPublicKeyWithRetry 的语义，简化单次重试） */
  static async getPublicKey(): Promise<string | null> {
    if (AuthService.cachedPublicKeyPem !== null) {
      return AuthService.cachedPublicKeyPem;
    }
    const pem = await AuthService.withHttp(async (client) => {
      return await client.fetchPublicKey();
    });
    if (pem !== null) {
      AuthService.cachedPublicKeyPem = pem;
      AuthService.state.publicKeyLoaded = true;
    }
    return pem;
  }

  /** 清除公钥缓存（服务器地址变更后），对齐 clearCachedPublicKey */
  static clearCachedPublicKey(): void {
    AuthService.cachedPublicKeyPem = null;
    AuthService.state.publicKeyLoaded = false;
  }

  /** 加密密码：需要公钥；失败返回结构化结果 */
  static async encryptPassword(password: string): Promise<
    { ok: true, encrypted: string } | { ok: false, reason: string }
  > {
    const pem = await AuthService.getPublicKey();
    if (pem === null) {
      return { ok: false, reason: 'network' };
    }
    const result = encryptPasswordWithPem(password, pem);
    if (!result.ok) {
      return { ok: false, reason: 'crypto' };
    }
    return result;
  }

  static async login(username: string, password: string, autoLoginEnabled: boolean): Promise<AuthOutcome> {
    const name = username.trim();
    if (name.length === 0 || password.trim().length === 0) {
      return { success: false, message: '用户名或密码不能为空' };
    }
    AuthService.state.isLoading = true;
    try {
      const encrypted = await AuthService.encryptPassword(password);
      if (!encrypted.ok) {
        return { success: false, message: describeEncryptFailure(encrypted.reason) };
      }
      const r = await AuthService.withHttp(async (client) => {
        return await client.login(name, encrypted.encrypted);
      });
      if (!r.ok) {
        const detail = r.data['detail'];
        return { success: false, message: detail !== undefined ? String(detail) : '登录失败' };
      }
      const messageToken = String(r.data['message_token'] ?? '');
      const loginToken = String(r.data['login_token'] ?? '');

      if (autoLoginEnabled) {
        await SecurityStore.setAutoLogin(true);
        await SecurityStore.setUsername(name);
        await SecurityStore.setAutoLoginToken(loginToken);
      } else {
        await SecurityStore.setAutoLogin(false);
        await SecurityStore.remove('username');
        await SecurityStore.remove('auto_login_token');
      }

      AuthService.state.isLoggedIn = true;
      AuthService.state.username = name;
      AuthService.state.messageToken = messageToken;
      hilog.info(0x0000, TAG, 'login ok: %{public}s', name);
      return { success: true, message: '登录成功' };
    } catch (e) {
      hilog.error(0x0000, TAG, 'login error: %{public}s', (e as Error).message);
      return { success: false, message: '登录失败，请联系管理员' };
    } finally {
      AuthService.state.isLoading = false;
    }
  }

  static async register(username: string, password: string, confirmPassword: string,
    inviteCode: string): Promise<AuthOutcome> {
    const name = username.trim();
    if (name.length === 0) {
      return { success: false, message: '用户名不能为空' };
    }
    if (password.trim().length === 0) {
      return { success: false, message: '密码不能为空' };
    }
    if (password !== confirmPassword) {
      return { success: false, message: '两次密码不一致' };
    }
    if (inviteCode.trim().length === 0) {
      return { success: false, message: '邀请码不能为空' };
    }
    AuthService.state.isLoading = true;
    try {
      const encrypted = await AuthService.encryptPassword(password);
      if (!encrypted.ok) {
        return { success: false, message: describeEncryptFailure(encrypted.reason) };
      }
      const r = await AuthService.withHttp(async (client) => {
        return await client.register(name, encrypted.encrypted, inviteCode.trim());
      });
      if (!r.ok) {
        const detail = r.data['detail'];
        return { success: false, message: detail !== undefined ? String(detail) : '注册失败' };
      }
      return { success: true, message: '注册成功，请登录' };
    } catch (e) {
      hilog.error(0x0000, TAG, 'register error: %{public}s', (e as Error).message);
      return { success: false, message: '注册失败，请联系管理员' };
    } finally {
      AuthService.state.isLoading = false;
    }
  }

  /** 自动登录：读取存储凭据 -> auto_login 端点 -> 更新 token */
  static async tryAutoLogin(): Promise<boolean> {
    const enabled = await SecurityStore.getAutoLogin();
    if (!enabled) {
      return false;
    }
    const username = await SecurityStore.getUsername();
    const token = await SecurityStore.getAutoLoginToken();
    if (username.length === 0 || token.length === 0) {
      return false;
    }
    AuthService.state.isLoading = true;
    try {
      const r = await AuthService.withHttp(async (client) => {
        return await client.autoLogin(username, token);
      });
      if (!r.ok) {
        hilog.warn(0x0000, TAG, 'auto login rejected status=%{public}d', r.status);
        return false;
      }
      const newToken = String(r.data['login_token'] ?? '');
      const userId = String(r.data['user_id'] ?? '');
      const messageToken = String(r.data['message_token'] ?? '');
      if (newToken.length > 0) {
        await SecurityStore.setAutoLoginToken(newToken);
      }
      AuthService.state.isLoggedIn = true;
      AuthService.state.username = userId.length > 0 ? userId : username;
      AuthService.state.messageToken = messageToken;
      hilog.info(0x0000, TAG, 'auto login ok');
      return true;
    } catch (e) {
      hilog.error(0x0000, TAG, 'auto login error: %{public}s', (e as Error).message);
      return false;
    } finally {
      AuthService.state.isLoading = false;
    }
  }

  static async logout(): Promise<void> {
    AuthService.clearCachedPublicKey();
    await SecurityStore.clearCredentials();
    AuthService.state.isLoggedIn = false;
    AuthService.state.username = '';
    AuthService.state.messageToken = '';
    hilog.info(0x0000, TAG, 'logout');
  }
}
