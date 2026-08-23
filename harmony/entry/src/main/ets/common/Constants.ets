/** 全局常量，对齐 app/config/index.ts 的 server_config */

/** 默认服务器地址（可在登录页/设置页覆盖，存于 SecurityStore） */
export const DEFAULT_BASE_URL: string = 'https://www-api.u3493359.nyat.app:11664';

/** HTTP 请求超时（毫秒） */
export const API_TIMEOUT_MS: number = 10000;

/** WS 心跳间隔（毫秒），对齐 ws_transport heartbeatIntervalMs */
export const HEARTBEAT_INTERVAL_MS: number = 10000;

/** 历史记录每次加载条数 */
export const LOAD_HISTORY_COUNT: number = 20;
