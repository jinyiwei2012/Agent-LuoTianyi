import { http } from '@kit.NetworkKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { API_TIMEOUT_MS } from '../common/Constants';

const TAG = 'HttpClient';

/** REST 响应封装 */
export interface HttpResult {
  ok: boolean;
  status: number;
  data: Record<string, Object>;
  error?: string;
}

/**
 * REST 客户端（@ohos.net.http），对齐 app 端 fetch + server_config。
 * 端点：/auth/public_key /auth/login /auth/register /auth/auto_login
 */
export class HttpClient {
  private baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
  }

  private urlFor(path: string): string {
    return `${this.baseUrl}${path}`;
  }

  async requestJson(path: string, method: http.RequestMethod, body: string | null,
    extraHeader?: Record<string, string>): Promise<HttpResult> {
    const httpRequest = http.createHttp();
    try {
      const headerMap: Record<string, string> = {
        'Content-Type': 'application/json',
      };
      if (extraHeader !== undefined) {
        for (const key of Object.keys(extraHeader)) {
          headerMap[key] = extraHeader[key];
        }
      }
      const options: http.HttpRequestOptions = {
        method: method,
        header: headerMap,
        readTimeout: API_TIMEOUT_MS,
        connectTimeout: API_TIMEOUT_MS,
      };
      if (body !== null) {
        options.extraData = body;
      }
      const response = await httpRequest.request(this.urlFor(path), options);
      const status = response.responseCode;
      let data: Record<string, Object> = {};
      try {
        data = JSON.parse(response.result as string) as Record<string, Object>;
      } catch (e) {
        hilog.warn(0x0000, TAG, 'response not json: %{public}s', (e as Error).message);
      }
      return {
        ok: status >= 200 && status < 300,
        status: status,
        data: data,
      };
    } catch (e) {
      hilog.error(0x0000, TAG, 'request failed: %{public}s', (e as Error).message);
      return {
        ok: false,
        status: 0,
        data: {},
        error: (e as Error).message,
      };
    } finally {
      httpRequest.destroy();
    }
  }

  /** GET /auth/public_key -> PEM 公钥字符串 */
  async fetchPublicKey(): Promise<string | null> {
    const r = await this.requestJson('/auth/public_key', http.RequestMethod.GET, null);
    if (!r.ok) {
      hilog.error(0x0000, TAG, 'public_key http error: %{public}d', r.status);
      return null;
    }
    const pem = r.data['public_key'] as string;
    if (typeof pem !== 'string' || pem.length === 0) {
      hilog.error(0x0000, TAG, 'public_key payload invalid');
      return null;
    }
    return pem;
  }

  /** POST /auth/login -> {message_token, login_token} */
  async login(username: string, encryptedPassword: string): Promise<HttpResult> {
    return await this.requestJson('/auth/login', http.RequestMethod.POST,
      JSON.stringify({ username: username, password: encryptedPassword }));
  }

  /** POST /auth/register */
  async register(username: string, encryptedPassword: string, inviteCode: string): Promise<HttpResult> {
    return await this.requestJson('/auth/register', http.RequestMethod.POST,
      JSON.stringify({ username: username, password: encryptedPassword, invite_code: inviteCode }));
  }

  /** POST /auth/auto_login -> {login_token, user_id, message_token} */
  async autoLogin(username: string, token: string): Promise<HttpResult> {
    return await this.requestJson('/auth/auto_login', http.RequestMethod.POST,
      JSON.stringify({ username: username, token: token }));
  }
}
