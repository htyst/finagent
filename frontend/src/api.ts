type Envelope<T> = { status: string; data: T; error_code: string | null; message: string };
let expectedUser = '';
let userGeneration = 0;
export type SecurityOperation = { level: 'yellow' | 'red'; required: string; action: string; amount_minor: number;
  including_this_minor: number | null; resource_kind: string; resource_id: string; terms: Record<string, unknown>;
  action_id?:string; authorization_path?:string };
let securityGate: ((operation: SecurityOperation) => Promise<void>) | null = null;
export function setSecurityGate(gate: typeof securityGate) { securityGate = gate; }
export function setExpectedUser(user: string) { expectedUser = user; userGeneration++; }
export class ApiError extends Error {
  constructor(message: string, public code: string, public status: number) { super(message); }
}
export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const user = expectedUser;
  const generation = userGeneration;
  if (options?.method === 'POST') {
    const taskToken=path.match(/^\/api\/tasks\/([^/]+)\/authorizations\/(proposal|wealth|card|subscription|birthday|schedule|collection)\/([^/]+)\/confirmation-token$/);
    if(taskToken){
      if(!securityGate)throw new ApiError('安全确认面板未就绪，请稍后重试','SECURITY_UI_UNAVAILABLE',0);
      const operation=await api<SecurityOperation>(path.replace(/\/confirmation-token$/,''));
      const expected=typeof options.body==='string'?JSON.parse(options.body).action_id:null;
      if(!expected||operation.action_id!==expected)throw new ApiError('操作单或任务版本已变化，请核对当前单','STALE_RESOURCE',409);
      await securityGate(operation);
      if(generation!==userGeneration)throw new ApiError('用户已变化，请重新确认','SESSION_CHANGED',409);
    }
    const routes: [RegExp, string][] = [
      [/^\/api\/proposals\/([^/]+)\/confirmation-token$/, 'proposal'],
      [/^\/api\/wealth\/orders\/([^/]+)\/confirmation-token$/, 'wealth'],
      [/^\/api\/card-management\/requests\/([^/]+)\/confirmation-token$/, 'card'],
      [/^\/api\/birthdays\/requests\/([^/]+)\/confirmation-token$/, 'birthday'],
      [/^\/api\/subscriptions\/requests\/([^/]+)\/confirmation-token$/, 'subscription'],
      [/^\/api\/schedules\/([^/]+)\/token$/, 'schedule'],
      [/^\/api\/collections\/([^/]+)\/confirmation-token$/, 'collection'],
    ];
    for (const [pattern, kind] of routes) {
      const match = path.match(pattern);
      if (match) {
        if (!securityGate) throw new ApiError('安全确认面板未就绪，请稍后重试', 'SECURITY_UI_UNAVAILABLE', 0);
        const operation = await api<SecurityOperation>(`/api/security/operations/${kind}/${match[1]}`);
        // Every yellow/red operation displays its server-owned details before token issuance.
        await securityGate(operation);
        if (generation !== userGeneration) throw new ApiError('用户已变化，请重新确认', 'SESSION_CHANGED', 409);
        break;
      }
    }
  }
  const headers = new Headers(options?.headers);
  if (user) headers.set('X-FinAgent-Expected-User', user);
  let res: Response;
  try {
    res = await fetch(path, { credentials: 'same-origin', ...options, headers,
      signal: options?.signal ?? AbortSignal.timeout(options?.method === 'POST' ? 150000 : 15000) });
  } catch {
    throw new ApiError('连接中断或等待超时，请重新查询状态。', 'NETWORK_ERROR', 0);
  }
  let payload: Envelope<T>;
  try { payload = await res.json() as Envelope<T>; }
  catch { throw new ApiError('服务暂时不可用，请稍后重新查询状态。', 'INVALID_RESPONSE', res.status); }
  if (!res.ok) {
    if (user && generation === userGeneration && user === expectedUser && ['SESSION_EXPIRED', 'SESSION_CHANGED'].includes(payload.error_code ?? '')) {
      window.dispatchEvent(new CustomEvent('finagent:session-lost', { detail: payload.message }));
    }
    throw new ApiError(payload.message || `请求失败 (${res.status})`, payload.error_code ?? 'REQUEST_ERROR', res.status);
  }
  if (generation !== userGeneration) throw new ApiError('用户已变化，请重新查询当前会话', 'SESSION_CHANGED', 409);
  return payload.data;
}
