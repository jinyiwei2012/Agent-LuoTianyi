import { preferences } from '@kit.ArkData';
import { common } from '@kit.AbilityKit';
import { DEFAULT_BASE_URL } from '../common/Constants';
import { hilog } from '@kit.PerformanceAnalysisKit';

const TAG = 'SecurityStore';
const STORE_NAME = 'agentluo';

/**
 * 本地安全存储（preferences 实现）。
 * 对齐 app 端 AsyncStorage + SecureStore 的语义：
 *   - server_url    自定义服务器地址（无则用默认值）
 *   - username      登录用户名
 *   - auto_login    是否自动登录（'true'/'false'）
 *   - auto_login_token 自动登录 token（原始 message_token 由服务端签发，
 *                      此处仅存 token 不存密码）
 *
 * 注意：密码仅在内存中进行 RSA 加密后提交，绝不落盘。
 * 阶段 4（合规打磨）可升级为 @ohos.security.asset 系统安全存储。
 */
export class SecurityStore {
  private static store: preferences.Preferences | null = null;

  private static async getStore(): Promise<preferences.Preferences> {
    if (SecurityStore.store === null) {
      const ctx = common.getContext() as common.UIAbilityContext;
      SecurityStore.store = await preferences.getPreferences(ctx, STORE_NAME);
    }
    return SecurityStore.store;
  }

  static async getString(key: string, defaultValue: string = ''): Promise<string> {
    const store = await SecurityStore.getStore();
    const value = await store.get(key, defaultValue);
    return value as string;
  }

  static async setString(key: string, value: string): Promise<void> {
    const store = await SecurityStore.getStore();
    await store.put(key, value);
    await store.flush();
  }

  static async remove(key: string): Promise<void> {
    const store = await SecurityStore.getStore();
    await store.delete(key);
    await store.flush();
  }

  // ---- 语义化接口 ----

  static async getServerUrl(): Promise<string> {
    const url = await SecurityStore.getString('server_url', DEFAULT_BASE_URL);
    return url.trim().length > 0 ? url : DEFAULT_BASE_URL;
  }

  static async setServerUrl(url: string): Promise<void> {
    await SecurityStore.setString('server_url', url.trim());
  }

  static async getUsername(): Promise<string> {
    return await SecurityStore.getString('username', '');
  }

  static async setUsername(username: string): Promise<void> {
    await SecurityStore.setString('username', username);
  }

  static async getAutoLogin(): Promise<boolean> {
    const flag = await SecurityStore.getString('auto_login', 'false');
    return flag === 'true';
  }

  static async setAutoLogin(enabled: boolean): Promise<void> {
    await SecurityStore.setString('auto_login', enabled ? 'true' : 'false');
  }

  static async getAutoLoginToken(): Promise<string> {
    return await SecurityStore.getString('auto_login_token', '');
  }

  static async setAutoLoginToken(token: string): Promise<void> {
    await SecurityStore.setString('auto_login_token', token);
  }

  static async clearCredentials(): Promise<void> {
    await SecurityStore.remove('server_url');
    await SecurityStore.remove('username');
    await SecurityStore.remove('auto_login');
    await SecurityStore.remove('auto_login_token');
    hilog.info(0x0000, TAG, 'credentials cleared');
  }
}
