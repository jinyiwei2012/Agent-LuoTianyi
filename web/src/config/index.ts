// Web 前端全局配置
// 在此处修改服务器地址等全局配置

const CUSTOM_SERVER_URL_KEY = 'custom_server_url';

export const server_config: {
  BASE_URL: string;
  API_TIMEOUT: number;
  LOAD_HISTORY_COUNT: number;
} = {
  // 服务器基础 URL - 默认值（与 app 端保持一致）
  BASE_URL: 'https://www-api.u3493359.nyat.app:11664',

  API_TIMEOUT: 10000, // 10秒超时
  LOAD_HISTORY_COUNT: 20, // 每次加载历史记录的条数
};

/** 从 localStorage 加载自定义服务器地址 */
export async function loadSavedServerUrl(): Promise<void> {
  try {
    const savedUrl = window.localStorage.getItem(CUSTOM_SERVER_URL_KEY);
    if (savedUrl) {
      server_config.BASE_URL = savedUrl;
      console.log(`Loaded saved server URL: ${savedUrl}`);
    }
  } catch (e) {
    console.warn('Failed to load saved server URL:', e);
  }
}

/** 保存自定义服务器地址 */
export function saveCustomServerUrl(url: string): void {
  try {
    window.localStorage.setItem(CUSTOM_SERVER_URL_KEY, url);
    server_config.BASE_URL = url;
  } catch (e) {
    console.warn('Failed to save server URL:', e);
  }
}

export default server_config;
