import request from '@/apis/requests'

export interface Latest7DaysCandidatesResponse {
  day: string
  count: number
}

export function getLatest7DaysCandidates() {
  return request.get<Array<Latest7DaysCandidatesResponse>>('/candidate/candidate/7d')
}

/** 饼图单项 */
export interface DistributionItem {
  name: string
  value: number
}

export interface CandidateDistributionResponse {
  /** 学历分布 */
  education: DistributionItem[]
  /** 状态分布 */
  status: DistributionItem[]
  /** 职位分布 */
  position: DistributionItem[]
}

export function getCandidateDistribution() {
  return request.get<CandidateDistributionResponse>('/candidate/candidate/distribution')
}
