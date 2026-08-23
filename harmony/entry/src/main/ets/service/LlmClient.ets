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

/** 消息内容（字符串或 content 数组） */
interface ChatMessagePart {
  type: string;
  text?: string;
  image_url?: ChatImageUrl;
}

interface ChatImageUrl {
  url: string;
  detail: string;
}

interface ChatMessageObj {
  role: string;
  content: Object;
}

/** 构建 OpenAI 兼容请求体 */
export function buildChatCompletionsPayload(options: BuildPayloadOptions): Record<string, Object> {
  const params = options.params ?? {};
  let messages: ChatMessageObj[];
  if (options.imageBase64 !== undefined && options.imageBase64.length > 0) {
    const textPart: ChatMessagePart = { type: 'text', text: options.prompt };
    const imageUrl: ChatImageUrl = { url: options.imageBase64, detail: 'auto' };
    const imagePart: ChatMessagePart = { type: 'image_url', image_url: imageUrl };
    const parts: ChatMessagePart[] = [textPart, imagePart];
    const userMsg: ChatMessageObj = { role: 'user', content: parts };
    messages = [userMsg];
  } else {
    const sysMsg: ChatMessageObj = { role: 'system', content: options.prompt };
    messages = [sysMsg];
  }

  const body: Record<string, Object> = {};
  body['model'] = options.model;
  body['messages'] = messages;
  body['max_tokens'] = params['max_tokens'] !== undefined ? params['max_tokens'] : 4096;
  body['temperature'] = params['temperature'] !== undefined ? params['temperature'] : 0.7;
  body['top_p'] = params['top_p'] !== undefined ? params['top_p'] : 0.9;
  // 其余用户参数全量透传（stop、presence_penalty 等）
  const inBody = (key: string): boolean => {
    return Object.keys(body).indexOf(key) >= 0;
  };
  for (const key of Object.keys(params)) {
    if (!inBody(key)) {
      body[key] = params[key];
    }
  }
  if (options.enableThinking === true) {
    const thinking: JsonField = { type: 'enabled' };
    body['thinking'] = thinking;
  }
  if (options.useJson === true) {
    const responseFormat: JsonField = { type: 'json_object' };
    body['response_format'] = responseFormat;
  }
  return body;
}

/** 单字段 JSON 对象（thinking/response_format 等） */
interface JsonField {
  type: string;
}

/** 调用选项（显式 interface） */
export interface CallProviderOptions {
  url: string;
  apiKey: string;
  body: Record<string, Object>;
}

/** OpenAI 兼容 chat/completions 响应结构 */
interface LlmChatResponse {
  choices?: Array<LlmChatChoice>;
  usage?: Object;
}

interface LlmChatChoice {
  message?: LlmChatMessage;
}

interface LlmChatMessage {
  content?: Object;
}

/** 调用 OpenAI 兼容接口 */
export async function callLlmProvider(options: CallProviderOptions): Promise<LlmResult> {
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
    const data = JSON.parse(String(response.result ?? '{}')) as LlmChatResponse;
    let content = '';
    if (data.choices !== undefined && data.choices.length > 0) {
      const raw = data.choices[0]?.message?.content;
      content = typeof raw === 'string' ? raw : '';
    }
    return { content: content, usage: data.usage ?? null };
  } catch (e) {
    hilog.error(0x0000, TAG, 'llm call failed: %{public}s', (e as Error).message);
    throw e as Error;
  } finally {
    httpRequest.destroy();
  }
}
