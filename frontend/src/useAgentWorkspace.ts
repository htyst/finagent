import { useState } from 'react';
import { api } from './api';
import { useTaskSync } from './useTaskSync';
import type { Condition, Validation } from './ConditionDetails';
import type { BirthdayDetails, TransferDetails } from './TaskDetailsForm';
import type { TransactionPage, TransactionFilters } from './TransactionEvidence';
export type Proposal = { id: string; task_id: string; kind: string; status: string; expires_at_utc: string;
  summary: { action: string; source?: { alias: string; mask: string }; target?: { alias: string; mask: string; phone_mask?: string | null; bank_name?: string; account_number?: string };
    card?: { alias: string; mask: string }; amount_minor?: number; impact: string; condition?: Condition }; validation?: Validation | null;
  receipt?: { order_id?: string; committed_at_utc?: string; message: string; amount_minor?: number; source_balance_minor?: number; recipient_credit_minor?: number; recipient_credit_id?: string; card_status?: string } | null };
export type AgentTask = { birthday_details?: BirthdayDetails | null; transfer_details?: TransferDetails | null; wealth_order?: { id: string } | null; card_request?: { id: string } | null; subscription_request?: { id: string } | null; birthday_request?: { id: string } | null; id: string; mode: string; intent: string | null; status: string; proposal: Proposal | null;
  messages: Array<{ role: string; content: string }>;
  requested_outcomes?: Array<string | {text: string; kind?: string}>;
  control?: { state?: string; reason?: string; suggestion?: string };
  plan_version: number; query_range?: string[];
  candidates: Array<{ id: string; merchant: string | null; amount_minor: number; posted_at_utc: string; card_last4: string | null }>;
  steps: Array<{ ordinal: number; tool_name: string; status: string; arguments?: TransactionFilters; result: Partial<TransactionPage> & {
    reason?: string; message?: string; available_balance_minor?: number; alias?: string; mask?: string;
    receipt?: { status?: string }; already_in_state?: boolean; status?: string; validation?: Validation; condition?: Condition; evidence?: Validation['evidence']; met?: boolean; complete?: boolean } }> };

export function useAgentWorkspace(onChanged: () => Promise<void>, revision: number) {
  const { task, setTask, busy, setBusy, loading, syncError, updatedAt, refresh, retry } = useTaskSync<AgentTask>('/api/agent/tasks/current', onChanged, revision);
  const [input, setInput] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const unavailable = busy || loading || !!syncError;
  async function write<T>(path: string, payload?: unknown): Promise<T> {
    const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf');
    return api<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json',
      'X-FinAgent-CSRF': csrf_token }, ...(payload === undefined ? {} : { body: JSON.stringify(payload) }) });
  }
  async function sendMessage(message: string) {
    if (!message || unavailable) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const active = task && !['completed', 'partially_completed', 'cancelled', 'expired', 'failed', 'scheduled', 'collecting'].includes(task.status);
      const result = active
        ? await write<AgentTask>(`/api/tasks/${task.id}/messages`, { message })
        : await write<AgentTask>('/api/tasks', { message });
      setTask(result); setInput('');
      await onChanged();
    } catch (err) {
      try { await refresh(); } catch { /* Keep the error and offer read-only recovery. */ }
      setError(err instanceof Error ? err.message : '对话请求失败');
    }
    finally { setBusy(false); }
  }
  async function submitDetails(kind: 'birthday' | 'transfer', values: Record<string, string>) {
    if (!task || unavailable) return;
    setBusy(true); setError(''); setNotice('');
    try {
      setTask(await write<AgentTask>(`/api/tasks/${task.id}/${kind}-details`, values));
      await onChanged();
    } catch (err) {
      try { await refresh(task.id); } catch { /* Keep submitted inputs visible until recovery. */ }
      setError(err instanceof Error ? err.message : '资料提交失败');
    } finally { setBusy(false); }
  }
  async function confirm(proposal: Proposal) {
    setBusy(true); setError(''); setNotice('');
    try {
      const { confirmation_token } = await write<{ confirmation_token: string }>(`/api/proposals/${proposal.id}/confirmation-token`);
      await write(`/api/proposals/${proposal.id}/confirm`, { confirmation_token });
      if (task) await refresh(task.id);
      await onChanged();
    } catch (err) {
      // A lost response after commit is resolved from the durable task state.
      try {
        if (task && (await refresh(task.id))?.proposal?.status === 'executed') { setNotice('已核对服务端，操作已成功，回执已恢复。'); await onChanged(); return; }
      } catch { /* Preserve the original request error. */ }
      setError(err instanceof Error ? err.message : '确认失败');
    } finally { setBusy(false); }
  }
  async function taskAction(action: 'cancel' | 'reject-operation' | 'resume' | 'pause' | 'handoff' | 'continue') {
    if (!task) return;
    setBusy(true); setError(''); setNotice('');
    try { setTask(await write<AgentTask>(`/api/tasks/${task.id}/${['pause', 'handoff', 'continue'].includes(action) ? 'control' : action}`,
      ['pause', 'handoff', 'continue'].includes(action) ? { action: action === 'continue' ? 'resume' : action, reason: '用户页面请求' } : undefined)); await onChanged(); }
    catch (err) { try { await refresh(task.id); } catch { /* Read-only retry is offered below. */ } setError(err instanceof Error ? err.message : '操作失败'); }
    finally { setBusy(false); }
  }
  return { task, input, setInput, error, notice, busy, loading, syncError, updatedAt, unavailable, retry, sendMessage, submitDetails, confirm, taskAction };
}
export type AgentWorkspace = ReturnType<typeof useAgentWorkspace>;
