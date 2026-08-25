import { useCallback, useEffect, useState } from 'react';
import { server_config } from '@/config';
import { fetchClientModelTypes } from '@/utils/llm_client';
import type { ClientModelType } from '@/utils/llm_client';
import { getLlmModulesConfig, setLlmModulesConfig } from '@/utils/llm_key_storage';
import { buildLlmModulesConfig, emptyModuleForm, validateLlmForms } from '@/utils/llm_requirements';
import type { ModuleFormState } from '@/utils/llm_requirements';

export interface LlmSettingsPageProps {
  onBack: () => void;
}

function formFromConfig(config: Awaited<ReturnType<typeof getLlmModulesConfig>>, requirement: ClientModelType): ModuleFormState {
  const stored = config[requirement.id] ?? config[requirement.name];
  if (!stored) return emptyModuleForm();
  return {
    enabled: stored.enabled,
    provider: stored.provider,
    baseUrl: stored.baseUrl,
    model: stored.model,
    apiKey: stored.apiKey,
    paramsText: stored.paramsText,
    supportsJson: stored.modelCapabilities.can_use_json,
    supportsThinking: stored.modelCapabilities.can_enable_thinking,
  };
}

export function LlmSettingsPage({ onBack }: LlmSettingsPageProps) {
  const [types, setTypes] = useState<ClientModelType[]>([]);
  const [forms, setForms] = useState<Record<string, ModuleFormState>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState<{ tone: 'error' | 'success'; text: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [response, stored] = await Promise.all([fetchClientModelTypes(server_config.BASE_URL), getLlmModulesConfig()]);
      const nextForms: Record<string, ModuleFormState> = {};
      response.types.forEach((requirement) => { nextForms[requirement.id] = formFromConfig(stored, requirement); });
      setTypes(response.types);
      setForms(nextForms);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '获取模型需求失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // 异步加载模型类型与已存配置，setState 均在 await 之后，属标准 mount 初始化。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const updateForm = (id: string, patch: Partial<ModuleFormState>) => {
    setForms((current) => ({ ...current, [id]: { ...(current[id] ?? emptyModuleForm()), ...patch } }));
  };

  const save = async () => {
    const validationError = validateLlmForms(types, forms);
    if (validationError) {
      setNotice({ tone: 'error', text: validationError });
      return;
    }
    setSaving(true);
    setNotice(null);
    try {
      await setLlmModulesConfig(buildLlmModulesConfig(types, forms));
      setNotice({ tone: 'success', text: '模型配置已保存。实际调用时会由服务端按能力要求选择。' });
    } catch (caught) {
      setNotice({ tone: 'error', text: caught instanceof Error ? caught.message : '保存模型配置失败' });
    } finally {
      setSaving(false);
    }
  };

  return (
    <main className="subpage-shell"><header className="subpage-header"><button className="secondary-button" type="button" onClick={onBack}>返回聊天</button><div><p className="eyebrow">YOUR MODEL ROUTER</p><h1>LLM 模型设置</h1><p>服务端只下发调用需求，服务商与密钥由你自己掌握。</p></div><button className="secondary-button" type="button" onClick={() => void load()}>刷新需求</button></header><div className="subpage-scroll"><div className="subpage-content subpage-content--wide"><div className="info-banner"><strong>隐私提醒</strong><span>Web 端配置会按已有契约保存在浏览器 localStorage 中。请不要在公共电脑上保存真实密钥。</span></div>{loading ? <div className="page-loader"><span /><p>正在读取服务端模型需求…</p></div> : error ? <div className="empty-card empty-card--error"><h2>无法读取模型需求</h2><p>{error}</p><button className="secondary-button" type="button" onClick={() => void load()}>重试</button></div> : types.length === 0 ? <div className="empty-card"><h2>服务端暂未下发模型类型</h2><p>可以稍后刷新，或先检查服务器地址。</p></div> : <><section className="model-grid">{types.map((requirement) => { const form = forms[requirement.id] ?? emptyModuleForm(); return <article className={`model-card ${form.enabled ? 'is-enabled' : ''}`} key={requirement.id}><header className="model-card__header"><div><div className="model-card__kind">{requirement.model_kind.toUpperCase()} {requirement.requires_json && <span>需要 JSON</span>} {requirement.requires_thinking && <span>需要 thinking</span>}</div><h2>{requirement.name}</h2></div><label className="switch-field"><input type="checkbox" checked={form.enabled} onChange={(event) => updateForm(requirement.id, { enabled: event.target.checked })} /><span>{form.enabled ? '已启用' : '未启用'}</span></label></header>{requirement.description && <p className="model-card__description">{requirement.description}</p>}{form.enabled && <div className="model-card__fields"><label className="field"><span>服务商</span><input value={form.provider} onChange={(event) => updateForm(requirement.id, { provider: event.target.value })} placeholder="例如：OpenAI Compatible" /></label><label className="field"><span>Base URL</span><input value={form.baseUrl} onChange={(event) => updateForm(requirement.id, { baseUrl: event.target.value })} inputMode="url" placeholder="https://example.com/v1" /></label><label className="field"><span>模型名称</span><input value={form.model} onChange={(event) => updateForm(requirement.id, { model: event.target.value })} placeholder="例如：gpt-4o-mini" /></label><label className="field"><span>API Key</span><input type="password" value={form.apiKey} onChange={(event) => updateForm(requirement.id, { apiKey: event.target.value })} autoComplete="off" placeholder="$YOUR_KEY 或真实密钥" /></label><div className="capability-grid"><label className="check-field"><input type="checkbox" checked={form.supportsJson} onChange={(event) => updateForm(requirement.id, { supportsJson: event.target.checked })} /><span>支持 JSON 输出</span></label><label className="check-field"><input type="checkbox" checked={form.supportsThinking} onChange={(event) => updateForm(requirement.id, { supportsThinking: event.target.checked })} /><span>支持 thinking</span></label></div><label className="field"><span>高级参数（可选 JSON 对象）</span><textarea value={form.paramsText} onChange={(event) => updateForm(requirement.id, { paramsText: event.target.value })} rows={3} placeholder={'{"temperature": 0.7}'} /></label></div>}</article>; })}</section>{notice && <p className={`form-notice form-notice--${notice.tone}`} role="status">{notice.text}</p>}<div className="settings-form__actions"><button className="secondary-button" type="button" onClick={onBack}>返回聊天</button><button className="primary-button" type="button" disabled={saving} onClick={() => void save()}>{saving ? '保存中…' : '保存全部配置'}</button></div></>}</div></div></main>
  );
}
