import request from '@/apis/requests'

/** 一条日程（数据来自钉钉日历） */
export interface CalendarEvent {
  /** 钉钉事件 ID */
  event_id: string
  /** 日程标题 */
  title: string
  /** 开始时间（ISO-8601，北京时间） */
  start: string
  /** 结束时间（ISO-8601，北京时间） */
  end: string
  /** 地点，可能为空 */
  location: string
  /** 是否全天日程 */
  is_all_day: boolean
}

export interface CalendarEventsResponse {
  items: CalendarEvent[]
  total: number
}

/** 拉取当前登录用户未来 days 天的日程（默认 7 天） */
export function getCalendarEvents(days = 7) {
  return request.get<CalendarEventsResponse>('/calendar/events', { days })
}
