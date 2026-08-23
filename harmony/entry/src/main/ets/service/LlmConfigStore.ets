import { SecurityStore } from './SecurityStore';
import { hilog } from '@kit.PerformanceAnalysisKit';

const TAG = 'LlmConfigStore';
const KEY = 'llm_modules_config';

/** 模型能力声明 */
export interface LlmCapabilities {
  can_enable_thinking: boolean;
  can_use_json: boolean;
}

/** LLM 模块配置（对齐 app LlmModuleConfig） */
export interface LlmModuleConfig {
  enabled: boolean;
  provider: string;
  model: string;
  baseUrl: string;
  apiKey: string;
  paramsText: string;
  modelCapabilities: LlmCapabilities;
}

/**
 * LLM 模块配置存储（对齐 app utils/llm_key_storage.ts）。
 * JSON 串存储于 preferences（SecurityStore），敏感字段为 apiKey——
 * 阶段 4 合规可升级安全存储。仅允许客户端托管 LLM/VLM 调用的场景使用。
 */
export class LlmConfigStore {
  private static async readRaw(): Promise<Record<string, Object>> {
    const raw = await SecurityStore.getString(KEY, '{}');
    try {
      return JSON.parse(raw) as Record<string, Object>;
    } catch (e) {
      hilog.warn(0x0000, TAG, 'parse config failed: %{public}s', (e as Error).message);
      return {};
    }
  }

  /** 获取全部模块配置（含 disabled），空即未配置 */
  static async getAll(): Promise<Record<string, Object>> {
    return await LlmConfigStore.readRaw();
  }

  /** 获取启用中的模块列表（types） */
  static async getEnabledTypes(): Promise<string[]> {
    const all = await LlmConfigStore.readRaw();
    const types: string[] = [];
    for (const key of Object.keys(all)) {
      const entry = all[key] as Record<string, Object>;
      if (entry !== undefined && entry['enabled'] === true) {
        types.push(key);
      }
    }
    return types;
  }

  /** 取单个模块配置 */
  static async getModuleConfig(moduleId: string): Promise<Record<string, Object> | null> {
    const all = await LlmConfigStore.readRaw();
    const entry = all[moduleId];
    return entry !== undefined ? entry as Record<string, Object> : null;
  }

  /** 保存全部配置 */
  static async saveAll(config: Record<string, Object>): Promise<void> {
    await SecurityStore.setString(KEY, JSON.stringify(config));
  }
}
