import { useRef } from 'react';
import Icon from './Icon';

export default function MessageComposer({ value, onChange, onSend, disabled, home = false }: {
  value: string; onChange: (value: string) => void; onSend: () => void; disabled: boolean; home?: boolean;
}) {
  const composing = useRef(false);
  return <form className={`message-composer${home ? ' home-composer' : ''}`} onSubmit={e => { e.preventDefault(); if (!disabled && value.trim()) onSend(); }}>
    <label className="sr-only" htmlFor="agent-message">说说你想了解什么</label>
    <textarea id="agent-message" rows={home ? 2 : 1} maxLength={1000} value={value}
      onChange={e => onChange(e.target.value)} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }}
      onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && !composing.current && e.keyCode !== 229) { e.preventDefault(); if (!disabled && value.trim()) onSend(); } }}
      placeholder={home ? '例如：查一下这个月的支出，看看有没有异常扣款' : '继续输入，或修改这笔办理…'} />
    <button type="submit" aria-label="发送" disabled={disabled || !value.trim()}><Icon name="send" size={23} /></button>
  </form>;
}
