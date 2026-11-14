<template>
  <div class="page-panel space-y-6">
    <h1 class="page-title">个人设置</h1>

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
          <el-tag :type="userStore.user?.status === 'ACTIVE' ? 'success' : 'danger'" size="small">
            {{ statusText(userStore.user?.status) }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 修改密码 -->
    <el-card shadow="never">
      <template #header>
        <span class="font-semibold">修改密码</span>
      </template>
      <el-form
        ref="passwordFormRef"
        :model="passwordForm"
        :rules="passwordRules"
        label-width="120px"
        style="max-width: 500px"
      >
        <el-form-item label="旧密码" prop="old_password">
          <el-input
            v-model="passwordForm.old_password"
            type="password"
            placeholder="请输入旧密码"
            show-password
          />
        </el-form-item>
        <el-form-item label="新密码" prop="new_password">
          <el-input
            v-model="passwordForm.new_password"
            type="password"
            placeholder="请输入新密码（6-15位）"
            show-password
          />
        </el-form-item>
        <el-form-item label="确认新密码" prop="confirm_password">
          <el-input
            v-model="passwordForm.confirm_password"
            type="password"
            placeholder="请再次输入新密码"
            show-password
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleChangePassword" :loading="passwordLoading">
            确认修改
          </el-button>
          <el-button @click="resetPasswordForm">重置</el-button>
        </el-form-item>
      </el-form>
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
            <div class="text-lg font-medium text-strong">{{ dingtalkUser.nick }}</div>
            <div class="text-sm text-desc">{{ dingtalkUser.mobile }}</div>
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
        <el-icon :size="48" class="text-muted mb-3"><Link /></el-icon>
        <p class="text-desc mb-4">绑定钉钉后，可直接使用钉钉扫码登录，并同步面试日程到钉钉日历</p>
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
      <ul class="list-disc list-inside space-y-2 text-sm text-desc">
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
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Link, Refresh } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import {
  getDingtalkAuthorizeUrl,
  getDingtalkStatus,
  changePassword,
  type DingtalkUser,
} from '@/apis/user_apis'

const userStore = useUserStore()

const dingtalkUser = ref<DingtalkUser | null>(null)
const loading = ref(false)
const binding = ref(false)
const passwordLoading = ref(false)
const passwordFormRef = ref<FormInstance>()

const passwordForm = ref({
  old_password: '',
  new_password: '',
  confirm_password: '',
})

const validatePass = (rule: any, value: any, callback: any) => {
  if (value === '') {
    callback(new Error('请输入密码'))
  } else if (value.length < 6 || value.length > 15) {
    callback(new Error('密码长度应为 6 到 15 个字符'))
  } else {
    callback()
  }
}

const validateConfirmPass = (rule: any, value: any, callback: any) => {
  if (value === '') {
    callback(new Error('请再次输入密码'))
  } else if (value !== passwordForm.value.new_password) {
    callback(new Error('两次输入的密码不一致'))
  } else {
    callback()
  }
}

const passwordRules: FormRules = {
  old_password: [{ validator: validatePass, trigger: 'blur' }],
  new_password: [{ validator: validatePass, trigger: 'blur' }],
  confirm_password: [{ validator: validateConfirmPass, trigger: 'blur' }],
}

const handleChangePassword = async () => {
  if (!passwordFormRef.value) return
  await passwordFormRef.value.validate(async (valid) => {
    if (!valid) return
    passwordLoading.value = true
    try {
      await changePassword(passwordForm.value)
      ElMessage.success('密码修改成功')
      resetPasswordForm()
    } catch (error: any) {
      ElMessage.error(error.response?.data?.detail ?? '密码修改失败')
    } finally {
      passwordLoading.value = false
    }
  })
}

const resetPasswordForm = () => {
  passwordFormRef.value?.resetFields()
  passwordForm.value = {
    old_password: '',
    new_password: '',
    confirm_password: '',
  }
}

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
