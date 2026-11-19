<template>
  <div class="page-panel space-y-4">
    <div class="flex items-center justify-between">
      <div>
        <h1 class="page-title">数据总览</h1>
        <p class="text-sm text-desc mt-1">近 7 天趋势 + 我的日程 + 学历/状态/职位分布</p>
      </div>
      <button class="btn btn-secondary" :disabled="loading" @click="refresh">刷新</button>
    </div>

    <div v-if="error" class="error-banner">
      {{ error }}
    </div>

    <!-- 上排：近 7 天趋势占 2/3，右侧日程列表占 1/3 -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <div class="panel-block lg:col-span-2">
        <div class="flex items-center justify-between">
          <div>
            <div class="text-base font-semibold text-strong">近 7 天新增候选人</div>
            <div class="mt-1 text-xs text-desc">候选人数量趋势</div>
          </div>
          <div v-if="loading" class="text-xs text-muted">加载中…</div>
        </div>
        <div ref="chartEl" class="mt-4 w-full" style="height: 300px"></div>
      </div>

      <div class="panel-block flex flex-col">
        <div class="flex items-center justify-between">
          <div>
            <div class="text-base font-semibold text-strong">我的日程</div>
            <div class="mt-1 text-xs text-desc">未来 {{ CALENDAR_DAYS }} 天</div>
          </div>
          <div class="text-xs text-muted">{{ events.length }} 场</div>
        </div>

        <div v-if="calendarError" class="error-banner mt-3">{{ calendarError }}</div>

        <div class="mt-3 flex-1 overflow-y-auto" style="max-height: 300px">
          <div v-if="calendarLoading" class="py-8 text-center text-xs text-muted">加载中…</div>
          <div v-else-if="!events.length" class="py-8 text-center text-xs text-muted">
            未来 {{ CALENDAR_DAYS }} 天暂无日程
          </div>
          <div
            v-for="ev in events"
            :key="ev.event_id || `${ev.start}-${ev.title}`"
            class="mb-2 rounded-lg border border-white/5 bg-white/[0.03] px-3 py-2"
          >
            <div class="flex items-center justify-between gap-2">
              <span class="text-xs text-brand">{{ formatDay(ev.start) }}</span>
              <span class="text-xs text-muted">
                {{ ev.is_all_day ? '全天' : `${formatTime(ev.start)}-${formatTime(ev.end)}` }}
              </span>
            </div>
            <div class="mt-1 text-sm text-strong truncate">{{ ev.title }}</div>
            <div v-if="ev.location" class="mt-0.5 text-xs text-desc truncate">{{ ev.location }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 三个分布饼图：各占三分之一、并列一行 -->
    <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
      <div class="panel-block">
        <div class="mb-2 text-sm font-semibold text-strong">学历分布</div>
        <div ref="educationChartEl" class="w-full" style="height: 220px"></div>
      </div>
      <div class="panel-block">
        <div class="mb-2 text-sm font-semibold text-strong">状态分布</div>
        <div ref="statusChartEl" class="w-full" style="height: 220px"></div>
      </div>
      <div class="panel-block">
        <div class="mb-2 text-sm font-semibold text-strong">职位分布</div>
        <div ref="positionChartEl" class="w-full" style="height: 220px"></div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import * as echarts from 'echarts'
import { getLatest7DaysCandidates, getCandidateDistribution } from '@/apis/dashboard_api'
import type { CandidateDistributionResponse, DistributionItem } from '@/apis/dashboard_api'
import { getCalendarEvents } from '@/apis/calendar_api'
import type { CalendarEvent } from '@/apis/calendar_api'

// 日程列表固定展示未来 7 天，与后端默认值一致
const CALENDAR_DAYS = 7

const loading = ref(false)
const error = ref<string | null>(null)

const calendarLoading = ref(false)
const calendarError = ref<string | null>(null)
const events = ref<CalendarEvent[]>([])

const chartEl = ref<HTMLDivElement | null>(null)
const educationChartEl = ref<HTMLDivElement | null>(null)
const statusChartEl = ref<HTMLDivElement | null>(null)
const positionChartEl = ref<HTMLDivElement | null>(null)

let chart: echarts.ECharts | null = null
let educationChart: echarts.ECharts | null = null
let statusChart: echarts.ECharts | null = null
let positionChart: echarts.ECharts | null = null

const chartDays = ref<string[]>([])
const chartCounts = ref<number[]>([])

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

/**
 * 把 ISO 时间换算到北京时间（UTC+8）再取字段。
 *
 * 后端统一发 ``+08:00``（见 ``app/core/time.py``），但浏览器 ``getHours()``
 * 按本地时区解释。不显式换算的话，非 UTC+8 的浏览器上时间会整体偏移。
 */
const toBeijingParts = (iso: string) => {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  // 先得到 UTC 时刻，再整体加 8 小时，最后用 UTC getter 读出北京时间的各字段
  const bj = new Date(d.getTime() + 8 * 3600_000)
  return {
    month: bj.getUTCMonth() + 1,
    day: bj.getUTCDate(),
    weekday: bj.getUTCDay(),
    hours: bj.getUTCHours(),
    minutes: bj.getUTCMinutes(),
  }
}

const pad2 = (n: number) => String(n).padStart(2, '0')

/** "2026-11-20T10:30:00+08:00" → "11-20 周五" */
const formatDay = (iso: string) => {
  const p = toBeijingParts(iso)
  return p ? `${pad2(p.month)}-${pad2(p.day)} ${WEEKDAYS[p.weekday]}` : iso
}

/** "2026-11-20T10:30:00+08:00" → "10:30" */
const formatTime = (iso: string) => {
  const p = toBeijingParts(iso)
  return p ? `${pad2(p.hours)}:${pad2(p.minutes)}` : '--:--'
}

/** 日程独立加载：失败只影响右栏，不拖累图表 */
const refreshCalendar = async () => {
  calendarLoading.value = true
  calendarError.value = null
  try {
    const res = await getCalendarEvents(CALENDAR_DAYS)
    events.value = res.items
  } catch (err) {
    console.error(err)
    calendarError.value = '获取日程失败：可能未绑定钉钉账号'
    events.value = []
  } finally {
    calendarLoading.value = false
  }
}

const renderChart = () => {
  if (!chartEl.value) return

  if (!chart) {
    chart = echarts.init(chartEl.value)
  }

  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(15, 23, 42, 0.88)',
      borderWidth: 0,
      textStyle: {
        color: '#F8FAFC',
      },
      axisPointer: {
        type: 'line',
        lineStyle: {
          color: '#94A3B8',
          width: 1,
        },
      },
    },
    grid: {
      left: 16,
      right: 16,
      top: 22,
      bottom: 18,
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: chartDays.value,
      axisLine: {
        show: false,
      },
      axisTick: {
        show: false,
      },
      axisLabel: {
        color: '#8b93a7',
      },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLine: {
        show: false,
      },
      axisTick: {
        show: false,
      },
      splitLine: {
        lineStyle: {
          color: 'rgba(255, 255, 255, 0.07)',
          type: 'dashed',
        },
      },
      axisLabel: {
        color: '#8b93a7',
      },
    },
    series: [
      {
        name: '候选人人数',
        type: 'line',
        smooth: true,
        data: chartCounts.value,
        showSymbol: false,
        lineStyle: {
          width: 3,
          color: '#6799fe',
          shadowColor: 'rgba(103, 153, 254, 0.35)',
          shadowBlur: 8,
        },
        itemStyle: {
          color: '#6799fe',
        },
        emphasis: {
          focus: 'series',
          itemStyle: {
            borderColor: '#d3dffe',
            borderWidth: 2,
          },
        },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(103, 153, 254, 0.32)' },
            { offset: 1, color: 'rgba(103, 153, 254, 0.02)' },
          ]),
        },
      },
    ],
  })
}

// 饼图配色
const PIE_COLORS = [
  '#6799fe',
  '#4d6bfe',
  '#6be2a2',
  '#ffc46b',
  '#ff8080',
  '#73a3d2',
  '#a78bfa',
  '#f472b6',
]

const pieOption = (data: DistributionItem[]): echarts.EChartsOption => {
  const option: echarts.EChartsOption = {
    backgroundColor: 'transparent',
    color: PIE_COLORS,
    tooltip: {
      trigger: 'item',
      backgroundColor: 'rgba(15, 23, 42, 0.92)',
      borderWidth: 0,
      textStyle: { color: '#F8FAFC' },
      formatter: '{b}: {c} ({d}%)',
    },
    legend: {
      bottom: 0,
      type: 'scroll',
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { color: '#8b93a7', fontSize: 11 },
    },
    series: [
      {
        type: 'pie',
        radius: ['42%', '66%'],
        center: ['50%', '42%'],
        avoidLabelOverlap: true,
        itemStyle: {
          borderColor: 'rgba(16, 18, 24, 1)',
          borderWidth: 2,
          borderRadius: 4,
        },
        label: { show: false },
        emphasis: {
          scale: true,
          scaleSize: 4,
        },
        data,
      },
    ],
  }
  if (!data.length) {
    option.title = {
      text: '暂无数据',
      left: 'center',
      top: 'center',
      textStyle: { color: '#8b93a7', fontSize: 13, fontWeight: 'normal' },
    }
  }
  return option
}

const renderPies = (dist: CandidateDistributionResponse) => {
  if (educationChartEl.value) {
    educationChart = educationChart || echarts.init(educationChartEl.value)
    educationChart.setOption(pieOption(dist.education), true)
  }
  if (statusChartEl.value) {
    statusChart = statusChart || echarts.init(statusChartEl.value)
    statusChart.setOption(pieOption(dist.status), true)
  }
  if (positionChartEl.value) {
    positionChart = positionChart || echarts.init(positionChartEl.value)
    positionChart.setOption(pieOption(dist.position), true)
  }
}

const refresh = async () => {
  loading.value = true
  error.value = null

  try {
    const [trend, dist] = await Promise.all([
      getLatest7DaysCandidates(),
      getCandidateDistribution(),
    ])

    chartDays.value = trend.map((item) => item.day)
    chartCounts.value = trend.map((item) => item.count)

    await nextTick()
    renderChart()
    renderPies(dist)

    // 日程走独立请求：失败只提示右栏，不影响图表渲染
    await refreshCalendar()
  } catch (err) {
    console.error(err)
    error.value = '获取看板数据失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}

const handleResize = () => {
  chart?.resize()
  educationChart?.resize()
  statusChart?.resize()
  positionChart?.resize()
}

onMounted(async () => {
  await refresh()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
  educationChart?.dispose()
  statusChart?.dispose()
  positionChart?.dispose()
  chart = null
  educationChart = null
  statusChart = null
  positionChart = null
})
</script>
