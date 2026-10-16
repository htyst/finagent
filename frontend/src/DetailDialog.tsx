import { useEffect, useRef } from 'react';
import Icon from './Icon';

export default function DetailDialog({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.showModal();
    return () => { if (previous?.isConnected) previous.focus(); };
  }, []);
  return <dialog ref={dialog} className="detail-dialog" aria-labelledby="detail-title" onCancel={e => { e.preventDefault(); onClose(); }}>
    <header><h2 id="detail-title">{title}</h2><button className="icon-button secondary" aria-label="关闭详情" onClick={onClose}><Icon name="close" /></button></header>
    <div className="detail-body">{children}</div>
  </dialog>;
}
