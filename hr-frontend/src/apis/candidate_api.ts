import httpRequest from './requests'
import { type Position } from './position_api'
import type { User } from '@/stores/user'

// Enums based on API documentation
export enum CandidateStatusEnum {
  APPLICATION = '已投递',
  AI_FILTER_FAILED = 'AI筛选失败',
  AI_FILTER_PASSED = 'AI筛选成功',
  MANUAL_PASSED = '人工通过/重试',
  WAITING_FOR_INTERVIEW = '待面试',
  REFUSED_INTERVIEW = '拒绝面试',
  INTERVIEW_PASSED = '面试通过',
  INTERVIEW_REJECTED = '面试未通过',
  HIRED = '已入职',
  REJECTED = '已拒绝',
  NEGOTIATION_EXPIRED = '协商超时',
  PROCESSING_ERROR = '处理异常',
  // AI 评分通过后、发送邀请邮件前的人工闸门
  PENDING_MANUAL_DECISION = '待人工确认',
  // 已发出邀请，等候选人回复或改约
  NEGOTIATING = '面试时间协商中',
}

// 允许的状态转移表（与后端保持一致）
export const ALLOWED_TRANSITIONS: Record<string, string[]> = {
  [CandidateStatusEnum.APPLICATION]: [
    CandidateStatusEnum.AI_FILTER_FAILED,
    // 评分达标直接进人工确认，AI筛选成功不再是需要手动再点一次的中间态
    CandidateStatusEnum.PENDING_MANUAL_DECISION,
  ],
  // 归档后允许 HR 补标细分终态
  [CandidateStatusEnum.AI_FILTER_FAILED]: [
    CandidateStatusEnum.MANUAL_PASSED,
    CandidateStatusEnum.REJECTED,
    CandidateStatusEnum.HIRED,
  ],
  [CandidateStatusEnum.AI_FILTER_PASSED]: [
    CandidateStatusEnum.WAITING_FOR_INTERVIEW,
  ],
  // 待人工确认：只能淘汰。要推进请走闸门（approve 会自己进入协商并发邀请），
  // 手动改成「面试时间协商中」会绕过闸门、不发邀请，造成状态与检查点脱节。
  [CandidateStatusEnum.PENDING_MANUAL_DECISION]: [
    CandidateStatusEnum.AI_FILTER_FAILED,
  ],
  // 面试时间协商中：拒绝 / 轮次耗尽 / 回复超时 / 确认时间
  [CandidateStatusEnum.NEGOTIATING]: [
    CandidateStatusEnum.REFUSED_INTERVIEW,
    CandidateStatusEnum.NEGOTIATION_EXPIRED,
    CandidateStatusEnum.WAITING_FOR_INTERVIEW,
  ],
  [CandidateStatusEnum.NEGOTIATION_EXPIRED]: [CandidateStatusEnum.MANUAL_PASSED],
  [CandidateStatusEnum.PROCESSING_ERROR]: [CandidateStatusEnum.MANUAL_PASSED],
  [CandidateStatusEnum.MANUAL_PASSED]: [CandidateStatusEnum.WAITING_FOR_INTERVIEW],
  [CandidateStatusEnum.WAITING_FOR_INTERVIEW]: [
    CandidateStatusEnum.INTERVIEW_PASSED,
    CandidateStatusEnum.INTERVIEW_REJECTED,
    CandidateStatusEnum.REFUSED_INTERVIEW,
  ],
  [CandidateStatusEnum.INTERVIEW_PASSED]: [
    CandidateStatusEnum.HIRED,
    CandidateStatusEnum.REJECTED,
  ],
  [CandidateStatusEnum.INTERVIEW_REJECTED]: [
    CandidateStatusEnum.HIRED,
    CandidateStatusEnum.REJECTED,
  ],
  [CandidateStatusEnum.HIRED]: [],
  [CandidateStatusEnum.REJECTED]: [],
  [CandidateStatusEnum.REFUSED_INTERVIEW]: [],
}

export enum EducationEnum {
  JUNIOR_COLLEGE = '大专',
  BACHELOR = '本科',
  MASTER = '硕士',
  DOCTOR = '博士',
  UNKNOWN = '未知',
}

export interface Resume {
  id: string
  file_path: string
  uploader: User
}

export interface ResumeUploadResponse {
  resume: Resume | null
}

export interface ResumeParseTaskResponse {
  task_id: string
}

export interface ResumeOCRResult {
  name?: string | null
  gender?: string | null
  birthday?: string | null
  email?: string | null
  phone_number?: string | null
  work_experience?: string | null
  project_experience?: string | null
  education_experience?: string | null
  self_evaluation?: string | null
  other_information?: string | null
  skills?: string | null
  work_year?: number | null
}

export interface ResumeParseTaskInfoResponse {
  task_id: string
  status: 'pending' | 'done' | 'failed'
  result?: ResumeOCRResult | null
  error?: string | null
}

export interface Candidate {
  id: string
  name: string
  email: string
  gender?: string | null
  birthday?: string | null
  phone_number?: string | null
  work_experience?: string | null
  project_experience?: string | null
  education_experience?: string | null
  self_evaluation?: string | null
  other_information?: string | null
  skills?: string | null
  status?: CandidateStatusEnum | string | null
  work_year?: number
  created_at: string
  error_message?: string | null
  creator: User
  position: Position
  resume: Resume
}

export interface CandidateListResponse {
  candidates: Candidate[]
  total: number
}

export interface CandidateCreateData {
  name: string
  email: string
  gender?: string | null
  birthday?: string | null
  phone_number?: string | null
  work_experience?: string | null
  project_experience?: string | null
  education_experience?: string | null
  self_evaluation?: string | null
  other_information?: string | null
  skills?: string | null
  work_year?: number | null
  position_id: string
  resume_id: string
  /** 跳过筛选与 AI 评分，直达面试邀请（不产生 AI 评分记录） */
  skip_scoring?: boolean
}

export interface CandidateStatusUpdateData {
  status: CandidateStatusEnum
  interview_time?: string | null
  rejection_reason?: string | null
}

export interface GetCandidatesParams {
  page?: number
  size?: number
  position_id?: string
  status?: CandidateStatusEnum
}

export interface CandidateAIScore {
  id: string
  work_experience_score: number
  technical_skills_score: number
  soft_skills_score: number
  educational_background_score: number
  project_experience_score: number
  overall_score: number
  summary: string
  strengths: string[]
  weaknesses: string[]
  candidate_id: string
}

export interface CandidateAIScoreResponse {
  ai_score: CandidateAIScore
}

export const uploadResume = (file: File): Promise<ResumeUploadResponse> => {
  const formData = new FormData()
  formData.append('file', file)
  return httpRequest.upload<ResumeUploadResponse>('/candidate/resume/upload', formData)
}

export const parseResume = (resumeId: string): Promise<ResumeParseTaskResponse> => {
  return httpRequest.post<ResumeParseTaskResponse>('/candidate/resume/parse', {
    resume_id: resumeId,
  })
}

export const getResumeParseTaskStatus = (taskId: string): Promise<ResumeParseTaskInfoResponse> => {
  return httpRequest.get<ResumeParseTaskInfoResponse>(`/candidate/resume/parse/${taskId}`)
}

export const createCandidate = (data: CandidateCreateData) => {
  return httpRequest.post('/candidate/create', data)
}

export const getCandidates = (params: GetCandidatesParams) => {
  return httpRequest.get<CandidateListResponse>('/candidate/list', params)
}

/**
 * 下载某候选人的简历（需登录，后端做权限校验）。
 * 简历属于敏感个人信息，不再通过公开的 /media 静态路径访问。
 */
export const downloadCandidateResume = (candidateId: string): Promise<Blob> =>
  httpRequest.get<Blob>(`/candidate/${candidateId}/resume`, undefined, { responseType: 'blob' })

/** 下载刚上传、尚未关联候选人的简历（仅上传者本人）。 */
export const downloadResumeFile = (resumeId: string): Promise<Blob> =>
  httpRequest.get<Blob>(`/candidate/resume/${resumeId}`, undefined, { responseType: 'blob' })

export const updateCandidateStatus = (candidateId: string, data: CandidateStatusUpdateData) => {
  return httpRequest.patch(`/candidate/${candidateId}/status`, data)
}

/**
 * 人工闸门：决定是否给候选人发送面试邀请邮件，唤醒暂停的招聘图。
 * approve = 发送并继续；reject = 不发送，淘汰；archive = 直接归档收尾。
 */
export const submitManualDecision = (
  candidateId: string,
  decision: 'approve' | 'reject' | 'archive',
) => {
  return httpRequest.post(`/candidate/${candidateId}/manual-decision`, { decision })
}

export const getCandidateAiScore = (candidateId: string) => {
  return httpRequest.get<CandidateAIScoreResponse>(`/candidate/ai-score/${candidateId}`)
}

export const deleteCandidate = (candidateId: string) => {
  return httpRequest.delete(`/candidate/${candidateId}`)
}
