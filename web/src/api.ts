import { useEffect, useState } from 'react';
export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: {'Content-Type':'application/json',...init?.headers} });
  let body: any; try { body = await response.json(); } catch { body = null; }
  if (!response.ok) {
    const detail = typeof body?.detail === 'string' ? body.detail : body?.error;
    throw new Error(detail || `服務暫時未能完成要求（${response.status}）。請稍後重試。`);
  }
  return body as T;
}
export function useData<T>(path: string, revision = 0) {
  const [data,setData] = useState<T | null>(null); const [loading,setLoading] = useState(true); const [error,setError] = useState(''); const [retry,setRetry] = useState(0);
  useEffect(() => { let active = true; setLoading(true); setError(''); request<T>(path).then(result=>{if(active)setData(result);}).catch(e=>{if(active)setError(e.message);}).finally(()=>{if(active)setLoading(false);}); return()=>{active=false;}; },[path,revision,retry]);
  return {data,loading,error,reload:()=>setRetry(x=>x+1)};
}
