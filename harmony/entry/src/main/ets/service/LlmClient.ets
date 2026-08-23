import { http } from '@kit.NetworkKit';
import { hilog } from '@kit.PerformanceAnalysisKit';

const TAG = 'LlmClient';

/** OpenAI 兼容 chat/completions 调用的返回 */
export interface LlmResult {
  content: string;
  usage: Object | null;
}

/** 构建请求 body 的选项（对齐 app BuildPayloadOptions） */
export interface BuildPayloadOptions {
  prompt: string;
  model: string;
  params?: Record<string, Object>;
  enableThinking?: boolean;
  useJson?: boolean;
  imageBase64?: string;
}

/**
 * LLM 委托执行（对齐 app/utils/llm_client.ts）。
 * 服务端下发 llm_request（按客户端模型类型）后，客户端使用用户自己的
 * api-key 直接调用 OpenAI 兼容的 /chat/completions，结果回传服务端。
 */

/** 构建 OpenAI 兼容请求体 */
export function buildChatCompletionsPayload(options: BuildPayloadOptions): Record<string, Object> {
  const params = options.params ?? {};
  let messages: Object[];
  if (options.imageBase64 !== undefined && options.imageBase64.length > 0) {
    messages = [
      {
        role: 'user',
        content: [
          { type: 'text', text: options.prompt },
          { type: 'image_url', image_url: { url: options.imageBase64, detail: 'auto' } },
        ],
      },
    ];
  } else {
    messages = [{ role: 'system', content: options.prompt }];
  }

  const body: Record<string, Object> = {
    model: options.model,
    messages: messages,
    max_tokens: params['max_tokens'] !== undefined ? params['max_tokens'] : 4096,
    temperature: params['temperature'] !== undefined ? params['temperature'] : 0.7,
    top_p: params['top_p'] !== undefined ? params['top_p'] : 0.9,
  };
  // 其余用户参数全量透传（stop、presence_penalty 等）
  for (const key of Object.keys(params)) {
    if (!(key in body)) {
      body[key] = params[key];
    }
  }
  if (options.enableThinking === true) {
    body['thinking'] = { type: 'enabled' };
  }
  if (options.useJson === true) {
    body['response_format'] = { type: 'json_object' };
  }
  return body;
}

/** 调用 OpenAI 兼容接口 */
export async function callLlmProvider(options: {
  url: string,
  apiKey: string,
  body: Record<string, Object>
}): Promise<LlmResult> {
  const httpRequest = http.createHttp();
  try {
    const response = await httpRequest.request(options.url, {
      method: http.RequestMethod.POST,
      header: {
        'Authorization': `Bearer ${options.apiKey}`,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
      },
      extraData: JSON.stringify(options.body),
      readTimeout: 60000,
      connectTimeout: 10000,
    });
    if (response.responseCode < 200 || response.responseCode >= 300) {
      const text = String(response.result ?? '');
      throw new Error(`LLM provider returned HTTP ${response.responseCode}: ${text}`);
    }
    const data = JSON.parse(String(response.result ?? '{}')) as {
      choices?: Array<{ message?: { content?: Object } }>,
      usage?: Object
    };
    let content = '';
    if (data.choices !== undefined && data.choices.length > 0) {
      const raw = data.choices[0]?.message?.content;
      content = typeof raw === 'string' ? raw : '';
    }
    return { content: content, usage: data.usage ?? null };
  } catch (e) {
    hilog.error(0x0000, TAG, 'llm call failed: %{public}s', (e as Error).message);
    throw e;
  } finally {
    httpRequest.destroy();
  }
}
