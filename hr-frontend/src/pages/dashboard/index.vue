<template>
  <div class="page-panel space-y-4">
    <div class="flex items-center justify-between">
      <div>
        <h1 class="page-title">数据总览</h1>
        <p class="text-sm text-desc mt-1">近 7 天趋势 + 职位/候选人指标</p>
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
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import * as echarts from 'echarts'
import { getLatest7DaysCandidates } from '@/apis/dashboard_api'

const loading = ref(false)
const error = ref<string | null>(null)

const chartEl = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null

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

const refresh = async () => {
  loading.value = true
  error.value = null

  try {
    const data = await getLatest7DaysCandidates()

    chartDays.value = data.map((item) => item.day)
    chartCounts.value = data.map((item) => item.count)

    await nextTick()
    renderChart()
  } catch (err) {
    console.error(err)
    error.value = '获取近 7 天候选人趋势失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}

const handleResize = () => {
  chart?.resize()
}

onMounted(async () => {
  await refresh()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
  chart = null
})
</script>
