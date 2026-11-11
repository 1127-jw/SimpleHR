<template>
  <div class="space-y-6">
    <h1 class="text-2xl font-bold text-gray-900">个人设置</h1>

    <!-- 用户信息 -->
    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">基本信息</span>
        </div>
      </template>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="用户名">
          {{ userStore.user?.username }}
        </el-descriptions-item>
        <el-descriptions-item label="真实姓名">
          {{ userStore.user?.realname }}
        </el-descriptions-item>
        <el-descriptions-item label="邮箱">
          {{ userStore.user?.email }}
        </el-descriptions-item>
        <el-descriptions-item label="部门">
          {{ userStore.user?.department?.name ?? '未分配' }}
        </el-descriptions-item>
        <el-descriptions-item label="角色">
          <el-tag v-if="userStore.user?.is_superuser" type="danger" size="small">超级管理员</el-tag>
          <el-tag v-else-if="userStore.user?.is_hr" type="success" size="small">HR</el-tag>
          <el-tag v-else type="info" size="small">员工</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="账号状态">
          <el-tag
            :type="userStore.user?.status === 'ACTIVE' ? 'success' : 'danger'"
            size="small"
          >
            {{ statusText(userStore.user?.status) }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 钉钉绑定 -->
    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">钉钉账号绑定</span>
          <el-tag v-if="dingtalkUser" type="success" size="small">已绑定</el-tag>
          <el-tag v-else type="info" size="small">未绑定</el-tag>
        </div>
      </template>

      <div v-if="dingtalkUser" class="flex items-center justify-between">
        <div class="flex items-center gap-4">
          <el-avatar :size="56" class="bg-blue-500">
            {{ dingtalkUser.nick?.charAt(0) ?? '钉' }}
          </el-avatar>
          <div>
            <div class="text-lg font-medium text-gray-900">{{ dingtalkUser.nick }}</div>
            <div class="text-sm text-gray-500">{{ dingtalkUser.mobile }}</div>
          </div>
        </div>
        <div class="flex gap-2">
          <el-button type="primary" plain @click="refreshDingtalkStatus" :loading="loading">
            <el-icon class="mr-1"><Refresh /></el-icon>
            刷新
          </el-button>
          <el-button type="danger" plain @click="handleBind">
            <el-icon class="mr-1"><Link /></el-icon>
            重新绑定
          </el-button>
        </div>
      </div>

      <div v-else class="text-center py-6">
        <el-icon :size="48" class="text-gray-300 mb-3"><Link /></el-icon>
        <p class="text-gray-500 mb-4">
          绑定钉钉后，可直接使用钉钉扫码登录，并同步面试日程到钉钉日历
        </p>
        <el-button type="primary" size="large" @click="handleBind" :loading="binding">
          <el-icon class="mr-1"><Link /></el-icon>
          绑定钉钉账号
        </el-button>
      </div>
    </el-card>

    <!-- 说明 -->
    <el-card shadow="never">
      <template #header>
        <span class="font-semibold">绑定说明</span>
      </template>
      <ul class="list-disc list-inside space-y-2 text-sm text-gray-600">
        <li>绑定钉钉后，面试官的空闲时间将自动与钉钉日历同步</li>
        <li>面试安排确认后，会自动在钉钉日历中创建日程</li>
        <li>可随时重新绑定，新绑定会覆盖旧的钉钉账号</li>
        <li>钉钉授权有效期为 2 小时，系统会自动刷新，无需手动操作</li>
      </ul>
    </el-card>
  </div>
</template>

<script lang="ts" setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Link, Refresh } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import { getDingtalkAuthorizeUrl, getDingtalkStatus, type DingtalkUser } from '@/apis/user_apis'

const userStore = useUserStore()

const dingtalkUser = ref<DingtalkUser | null>(null)
const loading = ref(false)
const binding = ref(false)

const statusText = (status?: string) => {
  const map: Record<string, string> = {
    ACTIVE: '正常',
    BLOCKED: '已冻结',
    RESIGNED: '已离职',
  }
  return map[status ?? ''] ?? status ?? '未知'
}

const refreshDingtalkStatus = async () => {
  loading.value = true
  try {
    const res = await getDingtalkStatus()
    dingtalkUser.value = res.dingding_user
  } catch {
    // 忽略错误，避免打扰用户
  } finally {
    loading.value = false
  }
}

const handleBind = async () => {
  try {
    await ElMessageBox.confirm(
      dingtalkUser.value
        ? '重新绑定将覆盖当前已绑定的钉钉账号，是否继续？'
        : '即将跳转到钉钉授权页面，是否继续？',
      '提示',
      { confirmButtonText: '继续', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return // 用户取消
  }

  binding.value = true
  try {
    const res = await getDingtalkAuthorizeUrl()
    // 跳转到钉钉授权页（授权完成后会重定向回后端 callback）
    window.location.href = res.authorize_url
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? '获取授权链接失败')
    binding.value = false
  }
}

onMounted(() => {
  refreshDingtalkStatus()
})
</script>
