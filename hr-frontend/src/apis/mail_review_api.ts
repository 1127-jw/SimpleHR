import httpRequest from './requests'

/** 待人工复核的邮件意图 */
export enum MailReviewStatus {
  PENDING = 'PENDING',
  RESOLVED = 'RESOLVED',
  IGNORED = 'IGNORED',
}

export const MAIL_REVIEW_STATUS_LABEL: Record<string, string> = {
  PENDING: '待复核',
  RESOLVED: '已处理',
  IGNORED: '已忽略',
}

export const MAIL_REVIEW_STATUS_TAG: Record<string, string> = {
  PENDING: 'warning',
  RESOLVED: 'success',
  IGNORED: 'info',
}

/** 可人工选定并执行的意图（与后端 _EXECUTABLE_INTENTS 对应） */
export const EXECUTABLE_INTENTS = [
  { value: 'INTERVIEW_REPLY', label: '面试回复（恢复协商流程）' },
  { value: 'RESUME_SUBMISSION', label: '简历投递（建投递记录）' },
  { value: 'PROGRESS_INQUIRY', label: '进度询问' },
]

/** 其余意图：确认后只记录，不触发动作 */
export const RECORD_ONLY_INTENTS = [
  { value: 'AUTO_REPLY', label: '自动回复' },
  { value: 'OUT_OF_SCOPE', label: '无关邮件' },
  { value: 'INTERVIEWER_MAIL', label: '面试官邮件' },
  { value: 'AMBIGUOUS', label: '仍然存疑' },
]

export interface MailReview {
  id: string
  from_address: string
  from_name: string
  subject: string
  intent_guess: string
  sub_intent: string
  confidence: number
  reason: string
  status: string
  resolver_id?: string | null
  /** 认领时间：仅「执行中」有值，用于并发抢占 */
  claimed_at?: string | null
  /** 处理完成时间 */
  resolved_at?: string | null
  resolution: string
  created_at: string
}

export interface MailReviewDetail extends MailReview {
  body_excerpt: string
  evidence: string
  attachment_paths: string[]
}

export interface MailReviewListResponse {
  items: MailReview[]
  total: number
}

export interface MailReviewListParams {
  status?: string
  page?: number
  size?: number
}

export const getMailReviews = (params: MailReviewListParams = {}) =>
  httpRequest.get<MailReviewListResponse>('/mail-reviews', params as object)

export const getMailReviewDetail = (reviewId: string) =>
  httpRequest.get<MailReviewDetail>(`/mail-reviews/${reviewId}`)

export interface ResolveMailReviewData {
  /** execute=按 intent 执行动作；ignore=标记忽略 */
  action: 'execute' | 'ignore'
  intent?: string
}

export const resolveMailReview = (reviewId: string, data: ResolveMailReviewData) =>
  httpRequest.post(`/mail-reviews/${reviewId}/resolve`, data)
