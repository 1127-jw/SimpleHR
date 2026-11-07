import request from '@/apis/requests'

export interface Latest7DaysCandidatesResponse {
  day: string
  count: number
}

export function getLatest7DaysCandidates() {
  return request.get<Array<Latest7DaysCandidatesResponse>>('/candidate/candidate/7d')
}
