import { createContext, useContext, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';

// In-memory UI drafts only. Provider is keyed by the demo user in App; nothing
// is persisted to browser storage, and authorization is never stored here.
const DraftContext = createContext<Map<string, unknown> | null>(null);
export function WorkspaceDrafts({ children }: { children: React.ReactNode }) {
  const drafts = useRef(new Map<string, unknown>());
  return <DraftContext.Provider value={drafts.current}>{children}</DraftContext.Provider>;
}
export function useWorkspaceDraft<T>(key: string, initial: T | (() => T)): [T, Dispatch<SetStateAction<T>>] {
  const cache = useContext(DraftContext);
  const [value, setValue] = useState<T>(() => cache?.has(key) ? cache.get(key) as T : typeof initial === 'function' ? (initial as () => T)() : initial);
  function update(next: SetStateAction<T>) {
    setValue(old => { const result = typeof next === 'function' ? (next as (old: T) => T)(old) : next; cache?.set(key, result); return result; });
  }
  return [value, update];
}
