<script setup lang="ts">
// 品牌 Logo（渐变圆角方块 + 招聘/添加成员图标）。
// 用模块级计数器生成唯一 gradient id，避免同页多次渲染时 SVG id 冲突。
defineProps({ size: { type: Number, default: 26 } })
let seed = 0
const gid = `lg-${++seed}`
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 28 28" fill="none" aria-hidden="true">
    <defs>
      <linearGradient :id="gid" x1="0" y1="0" x2="28" y2="28" gradientUnits="userSpaceOnUse">
        <stop offset="0" stop-color="#6799fe" />
        <stop offset="1" stop-color="#4d6bfe" />
      </linearGradient>
    </defs>
    <rect x="1" y="1" width="26" height="26" rx="8" :fill="`url(#${gid})`" />
    <!-- 人像 + 加号：招聘 / 引入候选人 -->
    <circle cx="12" cy="11.5" r="3.4" fill="#fff" />
    <path
      d="M5.5 21.5c.8-3.6 3.4-5.4 6.5-5.4s5.7 1.8 6.5 5.4"
      stroke="#fff"
      stroke-width="2.2"
      stroke-linecap="round"
    />
    <path d="M20.5 7v5M18 9.5h5" stroke="#fff" stroke-width="2" stroke-linecap="round" />
  </svg>
</template>
