/**
 * 后端 API 封装：
 * - 统一走 /api 代理
 * - 自动从 localStorage 注入 Authorization: Bearer <token>
 * - 已登录后访问受保护接口遇 401 时清 token 并抛 AuthError，由 App 层回到登录页
 * - 登录接口自身的 401（凭证错误）不应触发上述拦截，用 skipAuthIntercept 跳过
 * - 非 2xx 时抛出带 detail 的错误
 */

const TOKEN_KEY = 'kqz_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

/** token 过期专用错误，App 层据此回到登录页 */
export class AuthError extends Error {
  constructor(message = '登录已过期，请重新登录') {
    super(message)
    this.name = 'AuthError'
  }
}

export interface ApiOptions extends RequestInit {
  /** 登录等接口自身的 401 是凭证错误而非 token 过期，跳过自动清 token 拦截 */
  skipAuthIntercept?: boolean
}

export async function api<T>(path: string, init?: ApiOptions): Promise<T> {
  const { skipAuthIntercept, ...rest } = init ?? {}
  const headers = new Headers(rest.headers)
  const token = getToken()
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', 'Bearer ' + token)
  }
  const res = await fetch(path, { ...rest, headers })
  // 仅对「已登录后访问受保护接口」的 401 做 token 过期处理；
  // 登录接口的 401 是凭证错误，透传给调用方按普通错误展示
  if (res.status === 401 && !skipAuthIntercept) {
    clearToken()
    throw new AuthError()
  }
  if (!res.ok) {
    let msg = '请求失败（HTTP ' + res.status + '）'
    try {
      const body = await res.json()
      if (body?.detail) {
        msg = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
      }
    } catch {
      /* 保留默认信息 */
    }
    throw new Error(msg)
  }
  return res.json()
}

// ===== 鉴权 =====

export type UserRole = 'user' | 'admin'

export interface UserInfo {
  id: number
  email: string
  role: UserRole
}

export interface LoginResult {
  token: string
  user: UserInfo
}

export const authApi = {
  // 登录接口自身的 401 是凭证错误，不能被全局 401 拦截当成 token 过期
  login: (email: string, password: string) =>
    api<LoginResult>('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
      skipAuthIntercept: true,
    }),
  // 自助注册：新用户一律普通用户，注册即登录
  register: (email: string, password: string) =>
    api<LoginResult>('/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
      skipAuthIntercept: true,
    }),
  me: () => api<UserInfo>('/api/auth/me'),
}

// ===== 用户管理（仅管理员） =====

export interface UserItem {
  id: number
  email: string
  role: UserRole
  created_at: string
}

export interface RoleChangeItem {
  id: number
  target_email: string
  old_role: string
  new_role: string
  operator_email: string
  created_at: string
}

export interface SecurityLogItem {
  id: number
  user_email: string
  action: string
  path: string
  detail: string
  created_at: string
}

export const usersApi = {
  list: () => api<UserItem[]>('/api/users'),
  changeRole: (userId: number, role: UserRole, adminPassword: string) =>
    api<{ ok: boolean; email: string; old_role: string; new_role: string }>(
      '/api/users/' + userId + '/role',
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role, admin_password: adminPassword }),
      }
    ),
  roleChanges: () => api<RoleChangeItem[]>('/api/users/role-changes'),
  securityLogs: () => api<SecurityLogItem[]>('/api/security-logs'),
}

// ===== 会话历史消息 =====

export interface MessageItem {
  id: number
  role: 'user' | 'assistant'
  content: string
  intent: ChatIntent | null
  answer: AnswerInfo | null
  retrieval: RetrievalInfo | null
  question: string | null
  /** 1 点赞 / -1 点踩 / null 未评价 */
  feedback?: number | null
  feedback_at?: string | null
  created_at: string
}

export const messagesApi = {
  /** 拉取最近的历史消息，单次最多 50 条 */
  list: (limit = 50) => api<MessageItem[]>('/api/messages?limit=' + limit),
  /** 清空当前用户的全部聊天记录 */
  clear: () => api<{ ok: boolean; deleted: number }>('/api/messages', { method: 'DELETE' }),
}

// ===== 知识库文档 =====

export interface DocumentItem {
  id: number
  filename: string
  title: string
  size: number
  chunk_count: number
  status: 'processing' | 'ready' | 'failed'
  error: string | null
  created_at: string
}

export interface ChunkItem {
  id: number
  document_id: number
  seq: number
  heading_path: string
  content: string
}

export const documentsApi = {
  list: () => api<DocumentItem[]>('/api/documents'),
  get: (id: number) => api<{ document: DocumentItem; chunks: ChunkItem[] }>('/api/documents/' + id),
  upload: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api<DocumentItem>('/api/documents/upload', { method: 'POST', body: form })
  },
  remove: (id: number) =>
    api<{ ok: boolean }>('/api/documents/' + id, { method: 'DELETE' }),
}

// ===== 会话问答 =====

export type IntentName = 'SUPPORT' | 'FEEDBACK' | 'CHAT'

export interface ChatIntent {
  name: IntentName
  confidence: number
  resolved_question: string
  reason: string
}

export interface AnswerInfo {
  useful: boolean
  content: string
}

export interface ChatReply {
  reply: string
  intent: ChatIntent
  answer?: AnswerInfo | null
  retrieval?: RetrievalInfo | null
  user_message: MessageItem
  bot_message: MessageItem
  log_id: number
}

export interface RetrievalInfo {
  query: string
  semantic_hits: SearchHit[]
  bm25_hits: SearchHit[]
  fused_hits: SearchHit[]
  rerank_used: boolean
  chunks: RetrievedChunk[]
}

export interface SearchHit {
  chunk_id: number
  score: number
}

export interface RetrievedChunk {
  chunk_id: number
  document_id: number
  document_title: string
  seq: number
  heading_path: string
  content: string
  score: number
  semantic_score: number | null
  bm25_score: number | null
  rrf_score: number | null
}

export const chatApi = {
  /** 历史消息由后端按用户拉取，前端只传当前消息 */
  send: (message: string) =>
    api<ChatReply>('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    }),
}

// ===== 数据飞轮：反馈与审核 =====

export const feedbackApi = {
  /** 对 bot 回复点赞（1）/ 点踩（-1）；点赞自动收集进审核池 */
  submit: (messageId: number, rating: 1 | -1) =>
    api<{ ok: boolean; rating: number; collected: boolean }>('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_id: messageId, rating }),
    }),
}

export type ReviewStatus = 'pending' | 'approved' | 'rejected' | 'revised'

export interface ReviewItem {
  id: number
  message_id: number
  user_email: string
  question: string
  answer: string
  log_id: number | null
  sanitized_types: string[]
  status: ReviewStatus
  reviewer_email: string | null
  review_note: string | null
  revised_question: string | null
  revised_answer: string | null
  feedback: number
  created_at: string
  reviewed_at: string | null
}

export interface QaLibraryItem {
  id: number
  review_id: number
  question: string
  answer: string
  source: string
  document_id: number | null
  created_at: string
}

export const reviewsApi = {
  list: (status: ReviewStatus | 'all' = 'pending') =>
    api<ReviewItem[]>('/api/reviews?status=' + status),
  decide: (
    reviewId: number,
    decision: ReviewStatus,
    extra?: { note?: string; revised_question?: string; revised_answer?: string }
  ) =>
    api<{
      ok: boolean
      decision: ReviewStatus
      document_id: number | null
      qa_library_id: number | null
      message: string
    }>('/api/reviews/' + reviewId, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, ...extra }),
    }),
  qaLibrary: () => api<QaLibraryItem[]>('/api/qa-library'),
}

// ===== 调试中心（仅管理员） =====

export interface DebugParam {
  key: string
  label: string
  type: 'int' | 'float' | 'bool'
  group: string
  default: number | boolean
  min: number | null
  max: number | null
  current: number | boolean
}

export interface DebugConfig {
  params: DebugParam[]
  services: Record<string, string>
}

export interface ConfigChangeItem {
  id: number
  key: string
  old_value: number | boolean
  new_value: number | boolean
  updated_by: string
  created_at: string
}

export interface ReplayResult {
  replayed_log_id: number
  original_user_id: number
  original_intent: string
  question: string
  reply: string | null
  useful: boolean | null
  mock_used: boolean
  total_ms: number
  status: string
  error: string | null
  trace: Array<{
    name: string
    service: string
    input: unknown
    output: Record<string, unknown> | null
    duration_ms: number
    status: string
    error?: string
  }>
}

export const debugApi = {
  getConfig: () => api<DebugConfig>('/api/debug/config'),
  updateConfig: (updates: Record<string, number | boolean>) =>
    api<{ ok: boolean; changed: number; message: string }>('/api/debug/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates }),
    }),
  rollback: (key: string) =>
    api<{ ok: boolean; changed: number; message: string }>(
      '/api/debug/config/' + key + '/rollback',
      { method: 'POST' }
    ),
  changes: () => api<ConfigChangeItem[]>('/api/debug/config/changes'),
  replay: (logId: number) =>
    api<ReplayResult>('/api/debug/replay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ log_id: logId }),
    }),
}

// ===== 全链路日志 =====

export interface LogListItem {
  id: number
  question: string
  intent_name: string
  total_ms: number
  status: string
  created_at: string
  /** 管理员看全部日志时附带；普通用户只看自己时不返回 */
  user_email?: string
}

export interface LogTraceNode {
  name: string
  service: string
  input: unknown
  output: unknown
  duration_ms: number
  status: string
  error?: string
}

export interface LogDetail extends LogListItem {
  user_id: number
  trace: LogTraceNode[]
}

export const logsApi = {
  /** 拉取问答日志列表（仅管理员，看全部用户） */
  list: (limit = 50) => api<LogListItem[]>('/api/logs?limit=' + limit),
  /** 拉取单条日志详情（含全链路 trace 节点） */
  get: (id: number) => api<LogDetail>('/api/logs/' + id),
}
