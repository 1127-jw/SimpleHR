<template>
  <div class="page-panel space-y-4">
    <div class="flex justify-between items-center">
      <h1 class="page-title">邮件投递确认</h1>
      <el-button @click="loadList">刷新</el-button>
    </div>

    <el-tabs v-model="status" @tab-change="loadList">
      <el-tab-pane
        v-for="tab in tabs"
        :key="tab.value"
        :label="tab.label"
        :name="tab.value"
      />
    </el-tabs>

    <el-table :data="items" style="width: 100%" border v-loading="loading">
      <el-table-column prop="from_address" label="投递邮箱" min-width="180" />
      <el-table-column label="投递人" width="100">
        <template #default="scope">{{ scope.row.from_name || '-' }}</template>
      </el-table-column>
      <el-table-column prop="subject" label="主题" min-width="220" show-overflow-tooltip />
      <el-table-column label="附件" width="80">
        <template #default="scope">{{ scope.row.attachment_paths?.length ?? 0 }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="scope">
          <el-tag :type="statusTag(scope.row.status)" size="small">
            {{ statusLabel(scope.row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="投递时间" width="180" />
      <el-table-column label="操作" width="220">
        <template #default="scope">
          <el-button link type="primary" @click="openDetail(scope.row)">详情</el-button>
          <template v-if="scope.row.status === 'PENDING'">
            <el-button link type="success" @click="openConfirm(scope.row)">确认</el-button>
            <el-button link type="danger" @click="openReject(scope.row)">拒绝</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>

    <!-- 详情 -->
    <el-dialog v-model="detailVisible" title="投递详情" width="640px" append-to-body>
      <el-descriptions :column="1" border v-if="current">
        <el-descriptions-item label="投递邮箱">{{ current.from_address }}</el-descriptions-item>
        <el-descriptions-item label="主题">{{ current.subject }}</el-descriptions-item>
        <el-descriptions-item label="正文摘要">
          {{ current.body_excerpt || '（无）' }}
        </el-descriptions-item>
        <el-descriptions-item label="附件">
          <div v-for="(p, i) in current.attachment_paths || []" :key="i">{{ fileName(p) }}</div>
          <div v-if="!current.attachment_paths?.length">（无附件）</div>
        </el-descriptions-item>
        <el-descriptions-item label="解析信息">
          <pre class="parsed-info">{{ formatParsed(current.parsed_info) }}</pre>
        </el-descriptions-item>
      </el-descriptions>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 确认 -->
    <el-dialog v-model="confirmVisible" title="确认投递" width="520px" append-to-body>
      <p class="mb-3">请选择候选人应聘的职位，确认后将创建候选人并启动招聘流程。</p>
      <el-select v-model="selectedPositionId" placeholder="选择职位" style="width: 100%">
        <el-option
          v-for="p in positions"
          :key="p.id"
          :label="p.title"
          :value="p.id"
        />
      </el-select>
      <template #footer>
        <el-button @click="confirmVisible = false">取消</el-button>
        <el-button
          type="primary"
          :disabled="!selectedPositionId"
          :loading="submitting"
          @click="handleConfirm"
          >确认</el-button
        >
      </template>
    </el-dialog>

    <!-- 拒绝 -->
    <el-dialog v-model="rejectVisible" title="拒绝投递" width="480px" append-to-body>
      <el-input v-model="rejectNote" type="textarea" :rows="3" placeholder="拒绝原因（可选）" />
      <template #footer>
        <el-button @click="rejectVisible = false">取消</el-button>
        <el-button type="danger" :loading="submitting" @click="handleReject">拒绝</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  confirmMailSubmission,
  getMailSubmissions,
  rejectMailSubmission,
  MAIL_SUBMISSION_STATUS_LABEL,
  MAIL_SUBMISSION_STATUS_TAG,
  type MailSubmission,
} from '@/apis/mail_submission_api'
import { getPositionList } from '@/apis/position_api'

const tabs = [
  { label: '待确认', value: 'PENDING' },
  { label: '已确认', value: 'CONFIRMED' },
  { label: '已拒绝', value: 'REJECTED' },
  { label: '已过期', value: 'EXPIRED' },
]

const status = ref('PENDING')
const items = ref<MailSubmission[]>([])
const loading = ref(false)
const submitting = ref(false)

const detailVisible = ref(false)
const confirmVisible = ref(false)
const rejectVisible = ref(false)
const current = ref<MailSubmission | null>(null)
const currentAttachment = ref<MailSubmission | null>(null)

const positions = ref<Array<{ id: string; title: string }>>([])
const selectedPositionId = ref('')
const rejectNote = ref('')

const statusLabel = (s: string) => MAIL_SUBMISSION_STATUS_LABEL[s] ?? s
const statusTag = (s: string) => MAIL_SUBMISSION_STATUS_TAG[s] ?? 'info'
const fileName = (p: string) => p.split(/[\\/]/).pop() ?? p
const formatParsed = (info: Record<string, unknown>) =>
  info && Object.keys(info).length ? JSON.stringify(info, null, 2) : '（未解析）'

async function loadList() {
  loading.value = true
  try {
    const resp = await getMailSubmissions(status.value)
    items.value = resp.items
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail ?? '加载失败')
  } finally {
    loading.value = false
  }
}

async function loadPositions() {
  try {
    const resp = await getPositionList({ page: 1, size: 100 })
    positions.value = resp.positions ?? []
  } catch {
    positions.value = []
  }
}

function openDetail(row: MailSubmission) {
  current.value = row
  detailVisible.value = true
}

function openConfirm(row: MailSubmission) {
  currentAttachment.value = row
  selectedPositionId.value = row.suggested_position_id ?? ''
  confirmVisible.value = true
}

function openReject(row: MailSubmission) {
  currentAttachment.value = row
  rejectNote.value = ''
  rejectVisible.value = true
}

async function handleConfirm() {
  if (!currentAttachment.value || !selectedPositionId.value) return
  submitting.value = true
  try {
    await confirmMailSubmission(currentAttachment.value.id, selectedPositionId.value)
    ElMessage.success('已确认，候选人创建并进入招聘流程')
    confirmVisible.value = false
    await loadList()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail ?? '确认失败')
  } finally {
    submitting.value = false
  }
}

async function handleReject() {
  if (!currentAttachment.value) return
  submitting.value = true
  try {
    await rejectMailSubmission(currentAttachment.value.id, rejectNote.value)
    ElMessage.success('已拒绝')
    rejectVisible.value = false
    await loadList()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail ?? '操作失败')
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  loadList()
  loadPositions()
})
</script>

<style scoped>
.parsed-info {
  margin: 0;
  max-height: 240px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
