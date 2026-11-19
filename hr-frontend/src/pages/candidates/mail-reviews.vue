<template>
  <div class="page-panel space-y-4">
    <div class="flex justify-between items-center">
      <h1 class="page-title">意图复核</h1>
      <el-button @click="loadList">刷新</el-button>
    </div>

    <p class="text-sm text-desc">
      AI 判不准意图的邮件会落在这里，由 HR 确认意图并执行对应动作。
    </p>

    <el-tabs v-model="status" @tab-change="loadList">
      <el-tab-pane
        v-for="tab in tabs"
        :key="tab.value"
        :label="tab.label"
        :name="tab.value"
      />
    </el-tabs>

    <el-table :data="items" style="width: 100%" border v-loading="loading">
      <el-table-column prop="from_address" label="发件邮箱" min-width="170" />
      <el-table-column label="发件人" width="100">
        <template #default="scope">{{ scope.row.from_name || '-' }}</template>
      </el-table-column>
      <el-table-column prop="subject" label="主题" min-width="200" show-overflow-tooltip />
      <el-table-column label="AI 猜测" width="150">
        <template #default="scope">
          <el-tag size="small" type="info">{{ intentLabel(scope.row.intent_guess) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="置信度" width="90">
        <template #default="scope">{{ pct(scope.row.confidence) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="scope">
          <el-tag :type="statusTag(scope.row.status)" size="small">
            {{ statusLabel(scope.row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="时间" width="180" />
      <el-table-column label="操作" width="200">
        <template #default="scope">
          <el-button link type="primary" @click="openDetail(scope.row)">详情</el-button>
          <el-button
            v-if="scope.row.status === 'PENDING'"
            link
            type="success"
            @click="openResolve(scope.row)"
          >
            处理
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      class="justify-end"
      layout="total, prev, pager, next"
      :total="total"
      :page-size="size"
      :current-page="page"
      @current-change="onPage"
    />

    <!-- 详情 -->
    <el-dialog v-model="detailVisible" title="复核详情" width="680px" append-to-body>
      <el-descriptions v-if="detail" :column="1" border>
        <el-descriptions-item label="发件人">
          {{ detail.from_name ? `${detail.from_name} <${detail.from_address}>` : detail.from_address }}
        </el-descriptions-item>
        <el-descriptions-item label="主题">{{ detail.subject }}</el-descriptions-item>
        <el-descriptions-item label="AI 猜测">
          {{ intentLabel(detail.intent_guess) }}
          <span v-if="detail.sub_intent"> / {{ detail.sub_intent }}</span>
          （{{ pct(detail.confidence) }}）
        </el-descriptions-item>
        <el-descriptions-item label="判据">{{ detail.reason || '（无）' }}</el-descriptions-item>
        <el-descriptions-item label="证据">{{ detail.evidence || '（无）' }}</el-descriptions-item>
        <el-descriptions-item label="正文">{{ detail.body_excerpt || '（无）' }}</el-descriptions-item>
        <el-descriptions-item label="附件">
          <div v-for="(p, i) in detail.attachment_paths || []" :key="i">{{ fileName(p) }}</div>
          <div v-if="!detail.attachment_paths?.length">（无附件）</div>
        </el-descriptions-item>
        <el-descriptions-item v-if="detail.status !== 'PENDING'" label="处理结果">
          {{ detail.resolution || '-' }}
        </el-descriptions-item>
      </el-descriptions>
    </el-dialog>

    <!-- 处理 -->
    <el-dialog v-model="resolveVisible" title="处理复核" width="520px" append-to-body>
      <div class="space-y-3">
        <div class="text-sm">
          确认这封邮件的意图，系统会<b>执行对应动作</b>（恢复协商 / 建投递记录）。
        </div>
        <el-select v-model="resolveIntent" placeholder="选择意图" class="w-full">
          <el-option
            v-for="it in executableIntents"
            :key="it.value"
            :label="it.label"
            :value="it.value"
          />
          <el-option
            v-for="it in recordOnlyIntents"
            :key="it.value"
            :label="it.label + '（仅记录）'"
            :value="it.value"
          />
        </el-select>
        <div class="flex justify-end gap-2">
          <el-button :disabled="submitting" @click="resolveVisible = false">取消</el-button>
          <el-button
            type="danger"
            plain
            :disabled="submitting"
            @click="submitResolve('ignore')"
          >
            忽略
          </el-button>
          <el-button
            type="primary"
            :loading="submitting"
            :disabled="!resolveIntent"
            @click="submitResolve('execute')"
          >
            确认并执行
          </el-button>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script lang="ts" setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  EXECUTABLE_INTENTS,
  getMailReviewDetail,
  getMailReviews,
  MAIL_REVIEW_STATUS_LABEL,
  MAIL_REVIEW_STATUS_TAG,
  RECORD_ONLY_INTENTS,
  resolveMailReview,
  type MailReview,
  type MailReviewDetail,
} from '@/apis/mail_review_api'

const tabs = [
  { label: '待复核', value: 'PENDING' },
  { label: '已处理', value: 'RESOLVED' },
  { label: '已忽略', value: 'IGNORED' },
  { label: '全部', value: '' },
]

const executableIntents = EXECUTABLE_INTENTS
const recordOnlyIntents = RECORD_ONLY_INTENTS

const status = ref('PENDING')
const items = ref<MailReview[]>([])
const total = ref(0)
const page = ref(1)
const size = 20
const loading = ref(false)

const detailVisible = ref(false)
const detail = ref<MailReviewDetail | null>(null)

const resolveVisible = ref(false)
const resolveIntent = ref('')
const submitting = ref(false)

const statusLabel = (s: string) => MAIL_REVIEW_STATUS_LABEL[s] ?? s
const statusTag = (s: string) => MAIL_REVIEW_STATUS_TAG[s] ?? 'info'
const intentLabel = (v: string) => {
  const all = [...EXECUTABLE_INTENTS, ...RECORD_ONLY_INTENTS]
  return all.find((i) => i.value === v)?.label ?? v
}
const pct = (n: number) => `${Math.round((n || 0) * 100)}%`
const fileName = (p: string) => p.split(/[\\/]/).pop() ?? p

const loadList = async (): Promise<void> => {
  loading.value = true
  try {
    const res = await getMailReviews({
      status: status.value || undefined,
      page: page.value,
      size,
    })
    items.value = res.items || []
    total.value = res.total ?? 0
  } catch {
    ElMessage.error('获取复核列表失败')
  } finally {
    loading.value = false
  }
}

const onPage = (p: number): void => {
  page.value = p
  loadList()
}

const openDetail = async (row: MailReview): Promise<void> => {
  try {
    detail.value = await getMailReviewDetail(row.id)
    detailVisible.value = true
  } catch {
    ElMessage.error('获取详情失败')
  }
}

const openResolve = (row: MailReview): void => {
  detail.value = row as MailReviewDetail
  resolveIntent.value = row.intent_guess || ''
  resolveVisible.value = true
}

const submitResolve = async (action: 'execute' | 'ignore'): Promise<void> => {
  if (!detail.value) return
  if (action === 'execute' && !resolveIntent.value) {
    ElMessage.warning('请先选择意图')
    return
  }
  submitting.value = true
  try {
    await resolveMailReview(detail.value.id, {
      action,
      intent: action === 'execute' ? resolveIntent.value : undefined,
    })
    ElMessage.success(action === 'execute' ? '已按选定意图执行' : '已忽略')
    resolveVisible.value = false
    await loadList()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail ?? '处理失败')
  } finally {
    submitting.value = false
  }
}

onMounted(loadList)
</script>
