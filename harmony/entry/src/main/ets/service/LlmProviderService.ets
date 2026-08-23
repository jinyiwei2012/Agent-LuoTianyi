import { HttpClient } from './HttpClient';
import { http } from '@kit.NetworkKit';
import { hilog } from '@kit.PerformanceAnalysisKit';

const TAG = 'LlmProvider';

/** 模型能力（provider 元数据中的单模型） */
export interface ProviderModel {
  id: string;
  can_enable_thinking: boolean;
  can_use_json: boolean;
}

/** provider */
export interface ModelProvider {
  name: string;
  base_url: string;
  models: ProviderModel[];
}

/** 模型类型 */
export interface ModelType {
  type: string;
  description: string;
  providers: ModelProvider[];
}

/** /llm/providers 响应 */
export interface ModelTypesResponse {
  types: ModelType[];
}

/**
 * LLM provider 元数据服务（对齐 app utils/llm_client.ts）：
 * - fetchModelTypes:  GET <server>/llm/providers
 * - fetchModelsList:  GET <base_url>/models (Bearer) 探测校验，不耗时/不消耗 Token
 */
export class LlmProviderService {
  /** 获取客户端模型类型（服务端下发 LLM/VLM 支持项） */
  static async fetchModelTypes(serverBaseUrl: string): Promise<ModelTypesResponse> {
    const client = new HttpClient(serverBaseUrl);
    const r = await client.requestJson('/llm/providers', http.RequestMethod.GET, null);
    if (!r.ok) {
      hilog.error(0x0000, TAG, 'llm/providers failed: %{public}d', r.status);
      return { types: [] };
    }
    const rawList = r.data['types'];
    const types: ModelType[] = [];
    if (rawList instanceof Array) {
      for (let i = 0; i < rawList.length; i++) {
        types.push(rawList[i] as ModelType);
      }
    }
    return { types: types };
  }

  /** 探测校验：GET <base_url>/models，返回模型 id 列表；异常抛错 */
  static async fetchModelsList(baseUrl: string, apiKey: string): Promise<string[]> {
    const client = new HttpClient(baseUrl.replace(/\/+$/, ''));
    const r = await client.requestJson('/models', http.RequestMethod.GET, null,
      { 'Authorization': `Bearer ${apiKey}` });
    if (!r.ok) {
      throw new Error(`HTTP ${r.status}`);
    }
    const raw = r.data['data'];
    const ids: string[] = [];
    if (raw instanceof Array) {
      for (let i = 0; i < raw.length; i++) {
        const item = raw[i] as Record<string, Object>;
        if (item['id'] !== undefined) {
          const idStr = String(item['id']);
          if (idStr.length > 0) {
            ids.push(idStr);
          }
        }
      }
    }
    return ids;
  }
}
