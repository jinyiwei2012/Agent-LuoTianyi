import { useCallback, useEffect, useRef, useState } from 'react';
import { AffectionInfo, getAffectionInfo } from '@/utils/getAffection';

export function useAffection(username: string, messageToken: string) {
  const [affection, setAffection] = useState<AffectionInfo | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchAffection = useCallback(async () => {
    if (!username || !messageToken) {
      return;
    }
    const info = await getAffectionInfo(username, messageToken);
    if (info) {
      setAffection(info);
    }
  }, [username, messageToken]);

  useEffect(() => {
    // 异步拉取好感度并定时轮询：setState 发生在 await 之后（异步回调内），
    // 非同步级联渲染，属于标准 fetch-on-mount + polling 模式，故关闭该规则。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    fetchAffection();
    intervalRef.current = setInterval(fetchAffection, 60000);
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, [fetchAffection]);

  return { affection, refreshAffection: fetchAffection };
}
