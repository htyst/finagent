import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api';

type TaskState = { id: string; status: string; proposal: { id: string; status: string } | null };
const active = (task: TaskState | null) => !!task && (['created', 'gathering_info', 'running_readonly', 'awaiting_confirmation'].includes(task.status) || task.proposal?.status === 'pending_confirmation');
const signature = (task: TaskState | null) => JSON.stringify([task?.id, task?.status, task?.proposal?.id, task?.proposal?.status]);

// Reads are invalidated before a mutation; polling never repeats a POST.
export function useTaskSync<T extends TaskState>(path: string, onChanged: () => Promise<void>, revision = 0) {
  const [task, updateTask] = useState<T | null>(null);
  const [busy, updateBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [syncError, setSyncError] = useState('');
  const [updatedAt, setUpdatedAt] = useState('');
  const state = useRef({ mounted: false, sequence: 0, writing: false, task: null as T | null });
  const changed = useRef(onChanged);
  changed.current = onChanged;
  const setTask = useCallback((value: T | null) => {
    if (!state.current.mounted) return;
    state.current.sequence++;
    state.current.task = value;
    updateTask(value); setSyncError(''); setLoading(false);
    setUpdatedAt(new Date().toLocaleTimeString('zh-CN'));
  }, []);
  const refresh = useCallback(async (id?: string) => {
    const sequence = ++state.current.sequence;
    try {
      const value = await api<T | null>(id ? `/api/tasks/${id}` : path);
      if (!state.current.mounted || sequence !== state.current.sequence) return value;
      const different = state.current.task && signature(value) !== signature(state.current.task);
      state.current.task = value;
      updateTask(value); setSyncError(''); setLoading(false);
      setUpdatedAt(new Date().toLocaleTimeString('zh-CN'));
      if (different) void changed.current().catch(() => { /* Account refresh has its own notice. */ });
      return value;
    } catch (error) {
      if (state.current.mounted && sequence === state.current.sequence) {
        setLoading(false);
        setSyncError(error instanceof Error ? error.message : '状态同步失败');
      }
      throw error;
    }
  }, [path]);
  const retry = useCallback(() => { if (!state.current.writing) void refresh().catch(() => {}); }, [refresh]);
  useEffect(() => {
    state.current.mounted = true;
    return () => { state.current.mounted = false; state.current.sequence++; };
  }, []);
  useEffect(() => { retry(); }, [retry, revision]);
  useEffect(() => {
    const resume = () => { if (!document.hidden) retry(); };
    window.addEventListener('focus', resume);
    window.addEventListener('online', resume);
    document.addEventListener('visibilitychange', resume);
    return () => {
      window.removeEventListener('focus', resume);
      window.removeEventListener('online', resume);
      document.removeEventListener('visibilitychange', resume);
    };
  }, [retry]);
  useEffect(() => {
    if (busy || loading || syncError || !active(task)) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      if (stopped) return;
      if (!document.hidden && !state.current.writing) {
        try { await refresh(); } catch { return; }
      }
      if (!stopped && active(state.current.task)) timer = setTimeout(() => void tick(), 2000);
    };
    timer = setTimeout(() => void tick(), 2000);
    return () => { stopped = true; clearTimeout(timer); };
  }, [busy, loading, syncError, task?.id, task?.status, task?.proposal?.status, refresh]);
  function setBusy(value: boolean) {
    if (value && state.current.writing) return false;
    state.current.sequence++;
    state.current.writing = value;
    if (state.current.mounted) updateBusy(value);
    return true;
  }
  return { task, setTask, busy, setBusy, loading, syncError, updatedAt, refresh, retry };
}
