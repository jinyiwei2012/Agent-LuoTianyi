import { useEffect, useState } from 'react';
import { getPreferences, overwritePreferences } from '@/utils/getPreferences';
import type { UserPreferences } from '@/utils/getPreferences';

export interface PreferencesPageProps {
  username: string;
  messageToken: string;
  onBack: () => void;
}

const RELATIONSHIPS = ['朋友', '知己', '偶像', '搭档', '家人'];
const SPEAKING_STYLES = ['活泼可爱', '温柔可人', '文静恬淡'];

function personalityText(preferences: UserPreferences): string {
  if (typeof preferences['#sym:personality_text'] === 'string') {
    return preferences['#sym:personality_text'];
  }
  return preferences.personality_traits?.join('、') ?? '';
}

export function PreferencesPage({ username, messageToken, onBack }: PreferencesPageProps) {
  const [relationship, setRelationship] = useState('');
  const [speakingStyle, setSpeakingStyle] = useState('');
  const [personality, setPersonality] = useState('');
  const [customContext, setCustomContext] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<{ tone: 'error' | 'success'; text: string } | null>(null);

  useEffect(() => {
    let active = true;
    const load = async () => {
      const preferences = await getPreferences(username, messageToken);
      if (active && preferences) {
        setRelationship(preferences.relationship ?? '');
        setSpeakingStyle(preferences.speaking_style ?? '');
        setPersonality(personalityText(preferences));
        setCustomContext(preferences.custom_context ?? '');
      }
      if (active) setLoading(false);
    };
    void load();
    return () => { active = false; };
  }, [messageToken, username]);

  const save = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving(true);
    setNotice(null);
    const preferences: UserPreferences = {
      relationship: relationship.trim(),
      speaking_style: speakingStyle.trim(),
      '#sym:personality_text': personality.trim(),
      custom_context: customContext.trim(),
    };
    const success = await overwritePreferences(username, messageToken, preferences);
    setSaving(false);
    setNotice({ tone: success ? 'success' : 'error', text: success ? '偏好设置已保存，天依下次会记住这些相处方式。' : '保存失败，请检查网络后重试。' });
  };

  return (
    <main className="subpage-shell"><header className="subpage-header"><button className="secondary-button" type="button" onClick={onBack}>返回聊天</button><div><p className="eyebrow">YOUR CONNECTION</p><h1>相处方式</h1><p>让天依更懂你的语气，也更懂你们之间的距离。</p></div><span className="subpage-header__spacer" /></header><div className="subpage-scroll"><div className="subpage-content subpage-content--narrow">{loading ? <div className="page-loader"><span /><p>正在读取你们的相处方式…</p></div> : <form className="settings-form" onSubmit={save}><section className="settings-section"><div className="settings-section__heading"><h2>关系与语气</h2><p>这些选择会作为聊天时的长期偏好。</p></div><label className="field"><span>你希望天依是你的</span><input value={relationship} onChange={(event) => setRelationship(event.target.value)} placeholder="例如：朋友" /> </label><div className="choice-group" aria-label="关系快捷选项">{RELATIONSHIPS.map((option) => <button className={relationship === option ? 'choice-chip is-active' : 'choice-chip'} key={option} type="button" onClick={() => setRelationship(option)}>{option}</button>)}</div><label className="field"><span>你希望她的表达风格偏向</span><input value={speakingStyle} onChange={(event) => setSpeakingStyle(event.target.value)} placeholder="例如：活泼可爱" /></label><div className="choice-group" aria-label="表达风格快捷选项">{SPEAKING_STYLES.map((option) => <button className={speakingStyle === option ? 'choice-chip is-active' : 'choice-chip'} key={option} type="button" onClick={() => setSpeakingStyle(option)}>{option}</button>)}</div></section><section className="settings-section"><div className="settings-section__heading"><h2>天依的性格</h2><p>用几个词描述你喜欢的陪伴方式。</p></div><label className="field"><span>性格特点</span><textarea value={personality} onChange={(event) => setPersonality(event.target.value)} placeholder="例如：温柔、耐心、善解人意" rows={3} /></label></section><section className="settings-section"><div className="settings-section__heading"><h2>想让她记住的事</h2><p>可以写下你的习惯、近况或希望她注意的上下文。</p></div><label className="field"><span>自定义上下文</span><textarea value={customContext} onChange={(event) => setCustomContext(event.target.value)} placeholder="例如：我最近在准备考试，希望聊天时多给我一点鼓励。" rows={6} /></label></section>{notice && <p className={`form-notice form-notice--${notice.tone}`} role="status">{notice.text}</p>}<div className="settings-form__actions"><button className="secondary-button" type="button" onClick={onBack}>返回聊天</button><button className="primary-button" type="submit" disabled={saving}>{saving ? '保存中…' : '保存设置'}</button></div></form>}</div></div></main>
  );
}
