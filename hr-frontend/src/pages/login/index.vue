<template>
  <AuthLayout title="智能招聘系统" subtitle="登录您的账户">
    <el-form
      ref="ruleFormRef"
      :model="ruleForm"
      :rules="rules"
      label-position="top"
      class="space-y-6"
      @submit.prevent="submitForm(ruleFormRef)"
    >
      <el-form-item label="邮箱" prop="email">
        <el-input v-model="ruleForm.email" size="large" placeholder="请输入邮箱地址" />
      </el-form-item>

      <el-form-item label="密码" prop="password">
        <el-input
          v-model="ruleForm.password"
          type="password"
          size="large"
          placeholder="请输入密码"
          show-password
        />
      </el-form-item>

      <div>
        <el-button type="primary" native-type="submit" class="w-full" size="large">登录</el-button>
      </div>
    </el-form>

    <p class="mt-10 text-center text-sm text-gray-500">
      还没有账户?
      {{ ' ' }}
      <router-link
        to="/register"
        class="font-semibold leading-6 text-indigo-600 hover:text-indigo-500"
        >立即注册</router-link
      >
    </p>
  </AuthLayout>
</template>

<script lang="ts" setup>
import { reactive, ref } from 'vue'
import type { FormInstance, FormRules } from 'element-plus'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import AuthLayout from '@/components/AuthLayout.vue'
import { login, type LoginData } from '@/apis/user_apis'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const ruleFormRef = ref<FormInstance>()

const ruleForm = reactive({
  email: '',
  password: '',
})

const rules = reactive<FormRules<typeof ruleForm>>({
  email: [
    { required: true, message: '请输入邮箱地址', trigger: 'blur' },
    { type: 'email', message: '请输入正确的邮箱地址', trigger: ['blur', 'change'] },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 20, message: '密码长度应为 6 到 20 个字符', trigger: 'blur' },
  ],
})

async function submitForm(formEl: FormInstance | undefined) {
  if (!formEl) return
  await formEl.validate(async (valid) => {
    if (!valid) return
    try {
      const res = await login(ruleForm as LoginData)
      const user = res.user
      const useraccessToken = res.access_token
      const userRefreshToken = res.refresh_token
      const userStore = useUserStore()
      userStore.login(user, useraccessToken, userRefreshToken)
      ElMessage.success('登录成功')
      router.push('/')
    } catch (error) {
      ElMessage.error('登录失败，请检查您的邮箱和密码')
    }
  })
}
</script>
