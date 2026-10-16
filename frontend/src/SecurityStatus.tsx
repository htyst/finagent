import { useEffect, useState } from 'react';
import { api } from './api';

export default function SecurityStatus() {
  const [state, setState] = useState<{ locked_until: string | null; failures: number } | null>(null);
  useEffect(() => {
    let active = true;
    const read = () => api<{ locked_until: string | null; failures: number }>('/api/security/status').then(value => { if (active) setState(value); }).catch(() => {});
    void read(); const timer = window.setInterval(() => void read(), 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  const locked = state?.locked_until && new Date(state.locked_until).getTime() > Date.now();
  if (!locked) return null;
  return <div className="alert" role="alert">连续安全验证失败，写操作及后台扣款已锁定至 {new Date(state!.locked_until!).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}（北京时间）。可以继续必要查询、撤销未执行授权或请求人工帮助。</div>;
}
