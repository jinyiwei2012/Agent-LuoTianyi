import { HttpClient } from './HttpClient';
import { SecurityStore } from './SecurityStore';
import { AuthService } from './AuthService';
import { http } from '@kit.NetworkKit';
import { hilog } from '@kit.PerformanceAnalysisKit';

const TAG = 'Dynamics';

/** 动态（对齐 app DynamicPost） */
export interface DynamicPost {
  id: string;
  author_type: string;
  author_id: string;
  author_name: string;
  owner_user_id: string | null;
  visibility: string;
  content: string;
  source_type: string;
  allow_comment: boolean;
  comment_count: number;
  created_at: string | null;
}

/** 评论（对齐 app DynamicComment） */
export interface DynamicComment {
  id: string;
  dynamic_id: string;
  author_type: string;
  author_id: string;
  author_name: string;
  content: string;
  created_at: string | null;
}

/** 动态列表响应 */
export interface DynamicListResponse {
  items: DynamicPost[];
  has_more: boolean;
  next_cursor: string | null;
}

/** 评论列表响应 */
export interface DynamicCommentListResponse {
  items: DynamicComment[];
  has_more: boolean;
  next_cursor: string | null;
}

/** 动态来源展示标签 */
export function getDynamicSourceLabel(sourceType: string): string {
  if (sourceType === 'citywalk') {
    return '城市漫步';
  }
  if (sourceType === 'diary') {
    return '天依日记';
  }
  if (sourceType === 'song_learned') {
    return '学会新歌';
  }
  if (sourceType === 'system_notice') {
    return '系统通知';
  }
  if (sourceType === 'user_post') {
    return '生活动态';
  }
  return sourceType.length > 0 ? sourceType : '动态';
}

/**
 * 动态服务（对齐 app utils/dynamics.ts）。
 * 端点：GET /dynamics | POST /dynamics | GET/POST /dynamics/{id}/comments
 */
export class DynamicsService {
  private static async withClient(): Promise<HttpClient | null> {
    const username = AuthService.state.username;
    const token = AuthService.state.messageToken;
    if (username.length === 0 || token.length === 0) {
      return null;
    }
    const url = await SecurityStore.getServerUrl();
    return new HttpClient(url);
  }

  private static authHeader(): Record<string, string> {
    return { 'Authorization': `Bearer ${AuthService.state.messageToken}` };
  }

  private static parsePost(raw: Record<string, Object>): DynamicPost {
    return {
      id: raw['id'] !== undefined ? String(raw['id']) : '',
      author_type: raw['author_type'] !== undefined ? String(raw['author_type']) : '',
      author_id: raw['author_id'] !== undefined ? String(raw['author_id']) : '',
      author_name: raw['author_name'] !== undefined ? String(raw['author_name']) : '',
      owner_user_id: raw['owner_user_id'] !== undefined ? String(raw['owner_user_id']) : null,
      visibility: raw['visibility'] !== undefined ? String(raw['visibility']) : '',
      content: raw['content'] !== undefined ? String(raw['content']) : '',
      source_type: raw['source_type'] !== undefined ? String(raw['source_type']) : '',
      allow_comment: raw['allow_comment'] === true,
      comment_count: Number(raw['comment_count'] ?? 0),
      created_at: raw['created_at'] !== undefined ? String(raw['created_at']) : null,
    };
  }

  private static parseComment(raw: Record<string, Object>): DynamicComment {
    return {
      id: raw['id'] !== undefined ? String(raw['id']) : '',
      dynamic_id: raw['dynamic_id'] !== undefined ? String(raw['dynamic_id']) : '',
      author_type: raw['author_type'] !== undefined ? String(raw['author_type']) : '',
      author_id: raw['author_id'] !== undefined ? String(raw['author_id']) : '',
      author_name: raw['author_name'] !== undefined ? String(raw['author_name']) : '',
      content: raw['content'] !== undefined ? String(raw['content']) : '',
      created_at: raw['created_at'] !== undefined ? String(raw['created_at']) : null,
    };
  }

  /** GET /dynamics 列表 */
  static async getDynamics(limit: number = 20, cursor: string | null = null): Promise<DynamicListResponse> {
    const client = await DynamicsService.withClient();
    if (client === null) {
      return { items: [], has_more: false, next_cursor: null };
    }
    try {
      const query = `username=${encodeURIComponent(AuthService.state.username)}&limit=${limit}` + (cursor !== null ? `&cursor=${encodeURIComponent(cursor)}` : '');
      const r = await client.requestJson(`/dynamics?${query}`, http.RequestMethod.GET, null,
        DynamicsService.authHeader());
      if (!r.ok) {
        return { items: [], has_more: false, next_cursor: null };
      }
      const rawList = r.data['items'];
      const items: DynamicPost[] = [];
      if (rawList instanceof Array) {
        for (let i = 0; i < rawList.length; i++) {
          items.push(DynamicsService.parsePost(rawList[i] as Record<string, Object>));
        }
      }
      return {
        items: items,
        has_more: r.data['has_more'] === true,
        next_cursor: r.data['next_cursor'] !== undefined ? String(r.data['next_cursor']) : null,
      };
    } catch (e) {
      hilog.error(0x0000, TAG, 'getDynamics error: %{public}s', (e as Error).message);
      return { items: [], has_more: false, next_cursor: null };
    }
  }

  /** POST /dynamics 发布 */
  static async createDynamic(content: string): Promise<DynamicPost | null> {
    const client = await DynamicsService.withClient();
    if (client === null) {
      return null;
    }
    try {
      const body: Record<string, Object> = {};
      body['username'] = AuthService.state.username;
      body['token'] = AuthService.state.messageToken;
      body['content'] = content;
      const r = await client.requestJson('/dynamics', http.RequestMethod.POST, JSON.stringify(body));
      if (!r.ok) {
        return null;
      }
      const item = r.data['item'] as Record<string, Object> | null;
      return item !== null ? DynamicsService.parsePost(item) : null;
    } catch (e) {
      hilog.error(0x0000, TAG, 'createDynamic error: %{public}s', (e as Error).message);
      return null;
    }
  }

  /** GET /dynamics/{id}/comments */
  static async getComments(dynamicId: string): Promise<DynamicComment[]> {
    const client = await DynamicsService.withClient();
    if (client === null) {
      return [];
    }
    try {
      const query = `username=${encodeURIComponent(AuthService.state.username)}&limit=50`;
      const r = await client.requestJson(`/dynamics/${encodeURIComponent(dynamicId)}/comments?${query}`,
        http.RequestMethod.GET, null, DynamicsService.authHeader());
      if (!r.ok) {
        return [];
      }
      const rawList = r.data['items'];
      const items: DynamicComment[] = [];
      if (rawList instanceof Array) {
        for (let i = 0; i < rawList.length; i++) {
          items.push(DynamicsService.parseComment(rawList[i] as Record<string, Object>));
        }
      }
      return items;
    } catch (e) {
      hilog.error(0x0000, TAG, 'getComments error: %{public}s', (e as Error).message);
      return [];
    }
  }

  /** POST /dynamics/{id}/comments 发布评论 */
  static async createComment(dynamicId: string, content: string): Promise<DynamicComment | null> {
    const client = await DynamicsService.withClient();
    if (client === null) {
      return null;
    }
    try {
      const body: Record<string, Object> = {};
      body['username'] = AuthService.state.username;
      body['token'] = AuthService.state.messageToken;
      body['content'] = content;
      body['parent_comment_id'] = '';
      const r = await client.requestJson(`/dynamics/${encodeURIComponent(dynamicId)}/comments`,
        http.RequestMethod.POST, JSON.stringify(body));
      if (!r.ok) {
        return null;
      }
      const item = r.data['item'] as Record<string, Object> | null;
      return item !== null ? DynamicsService.parseComment(item) : null;
    } catch (e) {
      hilog.error(0x0000, TAG, 'createComment error: %{public}s', (e as Error).message);
      return null;
    }
  }
}
