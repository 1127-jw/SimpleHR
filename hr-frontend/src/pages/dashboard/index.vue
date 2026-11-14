<template>
  <div class="page-panel space-y-4">
    <div class="flex items-center justify-between">
      <div>
        <h1 class="page-title">数据总览</h1>
        <p class="text-sm text-desc mt-1">近 7 天趋势 + 学历/状态/职位分布</p>
      </div>
      <button class="btn btn-secondary" :disabled="loading" @click="refresh">刷新</button>
    </div>

    <div v-if="error" class="error-banner">
      {{ error }}
    </div>

    <div class="panel-block">
      <div class="flex items-center justify-between">
        <div>
          <div class="text-base font-semibold text-strong">近 7 天新增候选人</div>
          <div class="mt-1 text-xs text-desc">候选人数量趋势</div>
        </div>
        <div v-if="loading" class="text-xs text-muted">加载中…</div>
      </div>
      <div ref="chartEl" class="mt-4 w-full" style="height: 320px"></div>
    </div>

    <!-- 三个分布饼图：与上方卡片同宽，各占三分之一、并列一行 -->
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

const loading = ref(false)
const error = ref<string | null>(null)

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
