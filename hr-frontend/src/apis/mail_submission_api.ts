import httpRequest from './requests'

/** 待确认投递状态 */
export enum MailSubmissionStatus {
  PENDING = 'PENDING',
  CONFIRMED = 'CONFIRMED',
  REJECTED = 'REJECTED',
  EXPIRED = 'EXPIRED',
}

export const MAIL_SUBMISSION_STATUS_LABEL: Record<string, string> = {
  PENDING: '待确认',
  CONFIRMED: '已确认',
  REJECTED: '已拒绝',
  EXPIRED: '已过期',
}

export const MAIL_SUBMISSION_STATUS_TAG: Record<string, string> = {
  PENDING: 'warning',
  CONFIRMED: 'success',
  REJECTED: 'info',
  EXPIRED: 'danger',
}

export interface MailSubmission {
  id: string
  from_address: string
  from_name: string
  subject: string
  body_excerpt?: string
  attachment_paths: string[]
  parsed_info: Record<string, unknown>
  suggested_position_id?: string | null
  status: string
  review_note?: string
  created_at: string
}

export interface MailSubmissionListResponse {
  items: MailSubmission[]
  total: number
}

export const getMailSubmissions = (
  status: string = MailSubmissionStatus.PENDING,
): Promise<MailSubmissionListResponse> => {
  return httpRequest.get<MailSubmissionListResponse>('/mail-submissions', { status })
}

export const getMailSubmission = (id: string): Promise<MailSubmission> => {
  return httpRequest.get<MailSubmission>(`/mail-submissions/${id}`)
}

/** 确认投递：创建候选人并启动招聘流程 */
export const confirmMailSubmission = (id: string, positionId: string) => {
  return httpRequest.post(`/mail-submissions/${id}/confirm`, { position_id: positionId })
}

export const rejectMailSubmission = (id: string, note: string = '') => {
  return httpRequest.post(`/mail-submissions/${id}/reject`, { note })
}
