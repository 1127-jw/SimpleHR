<script setup lang="ts">
/**
 * 登录 / 注册布局：
 * 暗色背景（光晕 + 网格 + 噪点）+ 品牌粒子字样 + 毛玻璃认证卡片。
 * 与 SimpleYun 登录页保持同一套视觉语言。
 */
import LogoMark from './LogoMark.vue'
import ParticleField from './ParticleField.vue'

interface Props {
  title: string
  subtitle?: string
}

defineProps<Props>()
</script>

<template>
  <div class="auth-view">
    <div class="bg" aria-hidden="true">
      <div class="aurora aurora-a"></div>
      <div class="aurora aurora-b"></div>
      <div class="aurora aurora-c"></div>
      <div class="grid-overlay"></div>
      <div class="noise"></div>
    </div>

    <!-- 粒子背景：页面中央的 SimpleHR 由粒子拼成，鼠标划过会散开再聚合 -->
    <ParticleField text="SimpleHR" variant="center" />

    <nav class="topbar">
      <div class="brand">
        <span class="logo-mark"><LogoMark :size="26" /></span>
        <span class="brand-name">智能招聘</span>
      </div>
      <span class="badge-preview"><span class="dot"></span>SIMPLE HR</span>
    </nav>

    <main class="auth-hero">
      <div class="auth-copy">
        <p class="eyebrow">SIMPLE · 智能招聘平台</p>
        <h1 class="auth-title">让招聘，<br />更简单一点</h1>
        <p class="auth-sub">
          AI 简历解析 · 流程状态追踪 · 数据看板。<br />
          从投递到入职，一个平台管理全部候选人。
        </p>
        <div class="auth-points">
          <div class="point"><span class="pt-ic">⚡</span> 秒级简历解析</div>
          <div class="point"><span class="pt-ic">↻</span> 状态流转可追踪</div>
          <div class="point"><span class="pt-ic">◈</span> 数据驱动决策</div>
        </div>
      </div>

      <div class="auth-card glass">
        <h2 class="auth-card-title">{{ title }}</h2>
        <p v-if="subtitle" class="auth-card-sub">{{ subtitle }}</p>
        <div class="auth-card-body">
          <slot></slot>
        </div>
      </div>
    </main>

    <footer class="auth-footer">
      <span>智能招聘系统</span>
    </footer>
  </div>
</template>
