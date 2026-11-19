<script setup lang="ts">
/**
 * 主布局：暗色背景（环境光 + 网格）+ 粒子字样 + 毛玻璃侧边栏 + 内容区。
 * 仅登录后的业务页面套用本布局；登录/注册页在路由表中是独立顶层路由。
 */
import { computed, onBeforeMount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  House,
  Postcard,
  User,
  UserFilled,
  OfficeBuilding,
  Message,
  ChatDotRound,
  SwitchButton,
  Setting,
} from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import LogoMark from '@/components/LogoMark.vue'
import ParticleField from '@/components/ParticleField.vue'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

interface NavItem {
  to: string
  label: string
  icon: any
}

const navItems: NavItem[] = [
  { to: '/dashboard', label: '首页', icon: House },
  { to: '/positions', label: '职位管理', icon: Postcard },
  { to: '/candidates', label: '候选人管理', icon: User },
  { to: '/candidates/mail-submissions', label: '邮件投递确认', icon: Message },
  { to: '/candidates/mail-reviews', label: '意图复核', icon: ChatDotRound },
  { to: '/employees', label: '员工管理', icon: OfficeBuilding },
]

const adminNavItems: NavItem[] = [{ to: '/hr-management', label: 'HR管理', icon: UserFilled }]

const visibleNav = computed(() => {
  const items = [...navItems]
  if (userStore.user?.is_superuser) items.push(...adminNavItems)
  return items
})

const pageMeta: Record<string, { title: string; subtitle: string }> = {
  dashboard: { title: '首页', subtitle: '近 7 天趋势 + 职位/候选人指标' },
  positions: { title: '职位管理', subtitle: '管理与发布招聘职位' },
  candidates: { title: '候选人管理', subtitle: '跟踪候选人全流程状态' },
  'candidates-add': { title: '添加候选人', subtitle: '上传简历并自动解析关键信息' },
  'candidates-mail-submissions': {
    title: '邮件投递确认',
    subtitle: '确认邮件投递的简历并创建候选人',
  },
  employees: { title: '员工管理', subtitle: '邀请与管理在职员工' },
  'hr-management': { title: 'HR 管理', subtitle: '为 HR 指派负责部门' },
  settings: { title: '个人设置', subtitle: '账号信息、密码与钉钉绑定' },
}

const title = computed(() => pageMeta[route.name as string]?.title ?? '')
const subtitle = computed(() => pageMeta[route.name as string]?.subtitle ?? '')

const avatarText = computed(() => {
  const u = userStore.user
  return (u?.realname || u?.username || 'U').charAt(0).toUpperCase()
})

const roleLabel = computed(() => {
  const u = userStore.user
  if (u?.is_superuser) return '超级管理员'
  if (u?.is_hr) return 'HR'
  return '员工'
})

onBeforeMount(() => {
  if (!userStore.isLoggedIn) {
    router.push('/login')
  }
})

const handleLogout = () => {
  userStore.logout()
  router.push('/login')
}

const goToSettings = () => {
  router.push('/settings')
}
</script>

<template>
  <div class="app-view">
    <div class="app-bg" aria-hidden="true">
      <div class="aurora aurora-d"></div>
      <div class="grid-overlay"></div>
    </div>

    <!-- 粒子背景：网格之上的粒子场 + SimpleHR 文字粒子，鼠标可搅动 -->
    <ParticleField text="SimpleHR" variant="upper" />

    <div class="shell">
      <aside class="sidebar glass">
        <div class="brand sidebar-brand">
          <span class="logo-mark"><LogoMark :size="24" /></span>
          <span class="brand-name">智能招聘</span>
        </div>

        <nav class="side-nav">
          <router-link
            v-for="item in visibleNav"
            :key="item.to"
            class="nav-item"
            active-class="active"
            :to="item.to"
          >
            <el-icon><component :is="item.icon" /></el-icon>
            <span>{{ item.label }}</span>
          </router-link>
        </nav>

        <div class="sidebar-footer">
          <!-- 点击「头像 + 用户名」进入个人设置 -->
          <button class="me" title="个人设置" @click="goToSettings">
            <span class="avatar">{{ avatarText }}</span>
            <span class="me-meta">
              <span class="me-name">
                [{{ userStore.user?.department?.name ?? '未分配' }}]
                {{ userStore.user?.realname }}
              </span>
              <span class="me-role">{{ roleLabel }}</span>
            </span>
          </button>
          <button class="btn btn-ghost btn-block" @click="handleLogout">
            <el-icon><SwitchButton /></el-icon>
            退出登录
          </button>
        </div>
      </aside>

      <div class="workspace">
        <header class="app-header">
          <div>
            <h2 class="panel-title">{{ title }}</h2>
            <p class="panel-sub">{{ subtitle }}</p>
          </div>
          <span class="badge-preview" @click="goToSettings">
            <span class="dot"></span>
            <el-icon><Setting /></el-icon>
            设置
          </span>
        </header>

        <!-- 子路由视图 -->
        <main class="workspace-body">
          <router-view></router-view>
        </main>
      </div>
    </div>
  </div>
</template>
