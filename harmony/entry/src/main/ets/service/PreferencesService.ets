import { HttpClient } from './HttpClient';
import { SecurityStore } from './SecurityStore';
import { AuthService } from './AuthService';
import { http } from '@kit.NetworkKit';
import { hilog } from '@kit.PerformanceAnalysisKit';

const TAG = 'Preferences';

/** 用户偏好（对齐 app UserPreferences；#sym:personality_text 字段因 ArkTS 限制改用 personality_text，
 *  序列化/反序列化时映射回 #sym:personality_text 键） */
export interface UserPreferences {
  relationship: string;
  speaking_style: string;
  personality_text: string;
  personality_traits: string[];
  custom_context: string;
}

/** 服务端偏好响应（data.preferences 或 data 兜底） */
interface PrefResponse {
  preferences?: UserPreferences;
  relationship?: string;
  speaking_style?: string;
  custom_context?: string;
}

/**
 * 用户偏好服务（对齐 app utils/getPreferences.ts）：
 * - get:    POST /preference/get  {username, token}
 * - overwrite: POST /preference/overwrite {username, token, preferences}
 */
export class PreferencesService {
  private static emptyPrefs(): UserPreferences {
    return {
      relationship: '',
      speaking_style: '',
      personality_text: '',
      personality_traits: [],
      custom_context: '',
    };
  }

  static async get(): Promise<UserPreferences | null> {
    const username = AuthService.state.username;
    const token = AuthService.state.messageToken;
    if (username.length === 0 || token.length === 0) {
      return null;
    }
    try {
      const url = await SecurityStore.getServerUrl();
      const client = new HttpClient(url);
      const r = await client.requestJson('/preference/get', http.RequestMethod.POST,
        JSON.stringify({ username: username, token: token }));
      if (!r.ok) {
        return null;
      }
      const parsed = r.data as PrefResponse;
      if (parsed.preferences !== undefined) {
        return parsed.preferences;
      }
      // 兜底：data 本身即 preferences 字段
      const fallback = PreferencesService.fromFlat(r.data);
      return fallback;
    } catch (e) {
      hilog.error(0x0000, TAG, 'preferences error: %{public}s', (e as Error).message);
      return null;
    }
  }

  private static fromFlat(data: Record<string, Object>): UserPreferences {
    const prefs = PreferencesService.emptyPrefs();
    prefs.relationship = data['relationship'] !== undefined ? String(data['relationship']) : '';
    prefs.speaking_style = data['speaking_style'] !== undefined ? String(data['speaking_style']) : '';
    prefs.custom_context = data['custom_context'] !== undefined ? String(data['custom_context']) : '';
    const personalityText = data['#sym:personality_text'];
    prefs.personality_text = personalityText !== undefined ? String(personalityText) : '';
    const traits = data['personality_traits'];
    if (traits instanceof Array) {
      const list: string[] = [];
      for (let i = 0; i < traits.length; i++) {
        list.push(String(traits[i]));
      }
      prefs.personality_traits = list;
    }
    return prefs;
  }

  static async overwrite(preferences: UserPreferences): Promise<boolean> {
    const username = AuthService.state.username;
    const token = AuthService.state.messageToken;
    if (username.length === 0 || token.length === 0) {
      return false;
    }
    try {
      // 构建请求体：personality_text 映射回服务端使用的 #sym:personality_text 键
      const prefsBody: Record<string, Object> = {};
      prefsBody['relationship'] = preferences.relationship;
      prefsBody['speaking_style'] = preferences.speaking_style;
      prefsBody['#sym:personality_text'] = preferences.personality_text;
      prefsBody['personality_traits'] = preferences.personality_traits;
      prefsBody['custom_context'] = preferences.custom_context;

      const body: Record<string, Object> = {};
      body['username'] = username;
      body['token'] = token;
      body['preferences'] = prefsBody;

      const url = await SecurityStore.getServerUrl();
      const client = new HttpClient(url);
      const r = await client.requestJson('/preference/overwrite', http.RequestMethod.POST,
        JSON.stringify(body));
      return r.ok;
    } catch (e) {
      hilog.error(0x0000, TAG, 'overwrite error: %{public}s', (e as Error).message);
      return false;
    }
  }
}
