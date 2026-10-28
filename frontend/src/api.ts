const base = import.meta.env.VITE_API_BASE || '/api'
export interface ChatResult { response: string; thread_id: string }
export function newThreadId(): string { return crypto.randomUUID() }
export async function chat(message: string, threadId: string): Promise<ChatResult> {
  const response = await fetch(base + '/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message, thread_id: threadId }) })
  const data = await response.json().catch(() => ({ detail: '响应格式错误' }))
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请确认后端服务已启动')
  return data as ChatResult
}
export interface User { id: string; account: string; nickname: string; role: string }
export interface Tool { name: string; title: string; category: string; path: string; method: string; risk: string; description: string; available: boolean; example: Record<string, unknown>; input_schema: Record<string, unknown>; response_schema: Record<string, unknown> }
export interface AuditEvent { id: number; created_at: string; kind: string; payload: unknown; hash: string; prev_hash: string }
export interface Approval { id: string; run_id: string; owner_id: string; tool_name: string; arguments: unknown; reason: string; payload_hash: string; status: string; requested_at: string; decided_by?: string; decision_note?: string }
export interface Run { id: string; owner_id: string; conversation_id: string | null; prompt: string; mode: string; status: string; created_at: string; updated_at: string; error?: string; result?: { report?: string; display_report?: string; evidence_count?: number; context_reused?: boolean; source_run_id?: string | null; fresh_query_required?: boolean; current_evidence_count?: number; pending?: unknown }; events?: AuditEvent[]; approvals?: Approval[] }
export interface Conversation { id: string; owner_id: string; title: string; created_at: string; updated_at: string }
export interface ConversationDetail extends Conversation { runs: Run[] }
export interface Workflow { id: string; name: string; description: string; version: number; owner_id?: string; parameters: Record<string, unknown>; steps: { tool: string; arguments: Record<string, unknown> }[] }
let csrf = '', sessionVersion = 0
export function setCsrf(value: string) { csrf = value; sessionVersion++ }
export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const version = sessionVersion
  const response = await fetch(base + path, { method, credentials: 'include', cache: 'no-store', headers: { 'Content-Type': 'application/json', ...(method === 'POST' ? { 'X-CSRF-Token': csrf } : {}) }, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) })
  const data = await response.json().catch(() => ({ detail: '响应格式错误' }))
  if (version !== sessionVersion) throw new Error('登录状态已变更，请重试')
  if (!response.ok) {
    if (response.status === 401 && !path.includes('/auth/login')) window.dispatchEvent(new Event('session-expired'))
    throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data))
  }
  return data as T
}
export async function upload<T>(path: string, form: FormData): Promise<T> {
  const version = sessionVersion
  const response = await fetch(base + path, { method: 'POST', credentials: 'include', cache: 'no-store', headers: { 'X-CSRF-Token': csrf }, body: form })
  const data = await response.json().catch(() => ({ detail: '上传响应格式错误' }))
  if (version !== sessionVersion) throw new Error('登录状态已变更，请重试')
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('session-expired'))
    throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data))
  }
  return data as T
}
export async function download(path: string, name: string) {
  const version = sessionVersion
  const response = await fetch(base + path, { credentials: 'include', cache: 'no-store' })
  if (version !== sessionVersion) throw new Error('登录状态已变更，请重试')
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('session-expired'))
    throw new Error('导出失败，请检查权限')
  }
  const blob = await response.blob()
  if (version !== sessionVersion) throw new Error('登录状态已变更，请重试')
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a'); a.href = url; a.download = name; a.click(); URL.revokeObjectURL(url)
}
export function requestId() { return crypto.randomUUID() }
