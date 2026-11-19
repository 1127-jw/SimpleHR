<template>
  <div class="page-panel space-y-4">
    <div class="flex justify-between items-center">
      <h1 class="page-title">候选人管理</h1>
    </div>
    <div>
      <div class="flex justify-end mb-4">
        <router-link to="/candidates/add">
          <el-button type="primary">添加候选人</el-button>
        </router-link>
      </div>

      <div class="flex">
        <el-table :data="candidates" style="width: 100%" border>
          <el-table-column prop="name" label="姓名"></el-table-column>
          <el-table-column prop="position.title" label="职位"></el-table-column>
          <el-table-column label="性别">
            <template #default="scope">
              <el-tag v-if="scope.row.gender == '男'" type="success">男</el-tag>
              <el-tag v-else-if="scope.row.gender == '女'" type="danger">女</el-tag>
              <el-tag v-else type="warning">未知</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="email" label="邮箱"></el-table-column>
          <el-table-column prop="phone_number" label="手机"></el-table-column>
          <el-table-column prop="status" label="状态">
            <template #default="scope">
              <el-tag :type="getStatusTagType(scope.row.status)">{{ scope.row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="created_at" label="创建时间"></el-table-column>
          <el-table-column label="错误信息" width="200">
            <template #default="scope">
              <el-tooltip
                v-if="scope.row.error_message"
                :content="scope.row.error_message"
                placement="top"
                :show-after="200"
              >
                <el-tag type="danger" size="small" effect="plain">
                  {{
                    scope.row.error_message.length > 20
                      ? scope.row.error_message.slice(0, 20) + '...'
                      : scope.row.error_message
                  }}
                </el-tag>
              </el-tooltip>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column label="操作">
            <template #default="scope">
              <el-button link type="primary" @click="showDetails(scope.row)">详情</el-button>
              <span class="ml-1"></span>
              <el-button link type="primary" @click="openStatusDialog(scope.row)"
                >修改状态</el-button
              >
              <span class="ml-1"></span>
              <!-- 人工闸门：只有停在「待人工确认」的候选人才需要（也才允许）发邀请 -->
              <el-button
                v-if="scope.row.status === CandidateStatusEnum.PENDING_MANUAL_DECISION"
                link
                type="primary"
                @click="openManualDecision(scope.row)"
                >发送邮件</el-button
              >
              <span
                v-if="scope.row.status === CandidateStatusEnum.PENDING_MANUAL_DECISION"
                class="ml-1"
              ></span>
              <el-button link type="primary" @click="openResume(scope.row)">简历</el-button>
              <span class="ml-1"></span>
              <el-button link type="primary" @click="showAIScore(scope.row)">AI评分</el-button>
              <span class="ml-1"></span>
              <el-button link type="danger" @click="handleDelete(scope.row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 分页：每页 5 条 -->
      <div class="mt-4 flex justify-end">
        <el-pagination
          background
          :current-page="pagination.currentPage"
          :page-size="pagination.pageSize"
          :total="pagination.total"
          layout="prev, pager, next"
          @current-change="handlePageChange"
        />
      </div>

      <el-dialog v-model="detailsDialogVisible" title="候选人详情" append-to-body>
        <el-descriptions v-if="selectedCandidate" :column="2" border>
          <el-descriptions-item label="姓名">{{ selectedCandidate.name }}</el-descriptions-item>
          <el-descriptions-item label="职位">{{
            selectedCandidate.position.title
          }}</el-descriptions-item>
          <el-descriptions-item label="性别">
            <el-tag v-if="selectedCandidate.gender == '男'" type="success">男</el-tag>
            <el-tag v-else-if="selectedCandidate.gender == '女'" type="danger">女</el-tag>
            <el-tag v-else type="warning">未知</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="邮箱">{{ selectedCandidate.email }}</el-descriptions-item>
          <el-descriptions-item label="手机">{{
            selectedCandidate.phone_number
          }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="getStatusTagType(selectedCandidate.status as string)">{{
              selectedCandidate.status
            }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item v-if="selectedCandidate.error_message" label="错误信息" :span="2">
            <el-text type="danger" size="small">{{ selectedCandidate.error_message }}</el-text>
          </el-descriptions-item>
          <el-descriptions-item v-if="selectedCandidate.creator" label="推荐人">{{
            selectedCandidate.creator.realname
          }}</el-descriptions-item>
          <!-- <el-descriptions-item label="创建时间">{{
          selectedCandidate.created_at
        }}</el-descriptions-item> -->
          <el-descriptions-item label="工作经历" :span="2">{{
            selectedCandidate.work_experience || '-'
          }}</el-descriptions-item>
          <el-descriptions-item label="项目经历" :span="2">{{
            selectedCandidate.project_experience || '-'
          }}</el-descriptions-item>
          <el-descriptions-item label="教育经历" :span="2">{{
            selectedCandidate.education_experience || '-'
          }}</el-descriptions-item>
          <el-descriptions-item label="技能" :span="2">{{
            selectedCandidate.skills || '-'
          }}</el-descriptions-item>
          <el-descriptions-item label="自我评价" :span="2">{{
            selectedCandidate.self_evaluation || '-'
          }}</el-descriptions-item>
          <el-descriptions-item label="其他信息" :span="2">{{
            selectedCandidate.other_information || '-'
          }}</el-descriptions-item>
        </el-descriptions>
        <template #footer>
          <span class="dialog-footer">
            <el-button @click="detailsDialogVisible = false">关闭</el-button>
          </span>
        </template>
      </el-dialog>

      <el-dialog v-model="aiScoreDialogVisible" title="AI评分详情" append-to-body>
        <div v-if="aiScore">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="综合得分">{{
              aiScore.overall_score
            }}</el-descriptions-item>
            <el-descriptions-item label="工作经验">{{
              aiScore.work_experience_score
            }}</el-descriptions-item>
            <el-descriptions-item label="技术能力">{{
              aiScore.technical_skills_score
            }}</el-descriptions-item>
            <el-descriptions-item label="软技能">{{
              aiScore.soft_skills_score
            }}</el-descriptions-item>
            <el-descriptions-item label="教育背景">{{
              aiScore.educational_background_score
            }}</el-descriptions-item>
            <el-descriptions-item label="项目经验">{{
              aiScore.project_experience_score
            }}</el-descriptions-item>
            <el-descriptions-item label="优势" :span="2">
              <ul>
                <li v-for="(strength, index) in aiScore.strengths" :key="index">{{ strength }}</li>
              </ul>
            </el-descriptions-item>
            <el-descriptions-item label="劣势" :span="2">
              <ul>
                <li v-for="(weakness, index) in aiScore.weaknesses" :key="index">{{ weakness }}</li>
              </ul>
            </el-descriptions-item>
            <el-descriptions-item label="总结" :span="2">{{
              aiScore.summary
            }}</el-descriptions-item>
          </el-descriptions>
        </div>
        <template #footer>
          <span class="dialog-footer">
            <el-button @click="aiScoreDialogVisible = false">关闭</el-button>
          </span>
        </template>
      </el-dialog>

      <el-dialog v-model="statusDialogVisible" title="修改候选人状态" width="520px" append-to-body>
        <el-form :model="statusForm" label-width="100px">
          <el-form-item label="当前状态">
            <el-tag
              v-if="statusCandidate"
              :type="getStatusTagType(statusCandidate.status as string)"
              >{{ statusCandidate.status }}</el-tag
            >
          </el-form-item>

          <el-form-item label="目标状态" required>
            <el-select v-model="statusForm.status" placeholder="请选择目标状态" style="width: 100%">
              <el-option v-for="s in selectableNextStatuses" :key="s" :label="s" :value="s" />
            </el-select>
          </el-form-item>

          <el-form-item v-if="statusForm.status === '待面试'" label="面试时间" required>
            <el-date-picker
              v-model="statusForm.interview_time"
              type="datetime"
              placeholder="请选择面试时间"
              style="width: 100%"
            />
          </el-form-item>

          <el-form-item v-if="statusForm.status === '面试未通过'" label="未通过原因" required>
            <el-input
              v-model="statusForm.rejection_reason"
              type="textarea"
              :rows="3"
              placeholder="请输入未通过原因"
            />
          </el-form-item>

          <el-alert
            v-if="statusForm.status === CandidateStatusEnum.MANUAL_PASSED"
            type="warning"
            :closable="false"
            show-icon
            class="mb-4"
            title="系统将重新发送面试邀请邮件，并开始协商面试时间"
          />
        </el-form>

        <template #footer>
          <span class="dialog-footer">
            <el-button @click="statusDialogVisible = false">取消</el-button>
            <el-button type="primary" :loading="statusSubmitting" @click="submitStatus"
              >确认</el-button
            >
          </span>
        </template>
      </el-dialog>

      <!-- 人工闸门：决定要不要发面试邀请邮件（唤醒暂停的招聘图） -->
      <el-dialog v-model="manualDialogVisible" title="发送面试邀请" width="440px">
        <div v-if="manualCandidate" class="space-y-3">
          <div class="text-sm">
            候选人 <span class="text-strong">{{ manualCandidate.name }}</span> 已通过 AI
            评分，流程正停在人工确认环节。
          </div>
          <div class="text-sm text-desc">
            <el-button
              type="primary"
              size="small"
              :loading="manualSubmitting"
              @click="handleManualDecision('approve')"
              >发送邮件并继续</el-button
            >
            <el-button
              type="danger"
              plain
              size="small"
              :loading="manualSubmitting"
              @click="handleManualDecision('reject')"
              >不发送，淘汰</el-button
            >
            <el-button
              size="small"
              :loading="manualSubmitting"
              @click="handleManualDecision('archive')"
              >归档，不再跟进</el-button
            >
          </div>
          <div class="text-xs text-muted">
            「发送邮件」会继续协商面试时间的流程；「不发送」会把候选人置为 AI 筛选失败；
            「归档」直接结束流程（状态保持当前值，之后仍可补标细分终态）。
          </div>
        </div>
      </el-dialog>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getCandidates,
  getCandidateAiScore,
  updateCandidateStatus,
  downloadCandidateResume,
  deleteCandidate,
  submitManualDecision,
} from '@/apis/candidate_api'
import type { Candidate, CandidateAIScore } from '@/apis/candidate_api'
import { CandidateStatusEnum, ALLOWED_TRANSITIONS } from '@/apis/candidate_api'
import { getPositionList, type Position } from '@/apis/position_api'

const candidates = ref<Candidate[]>([])
const positions = ref<Position[]>([])

// 候选人列表分页（每页 5 条）
const pagination = reactive({
  currentPage: 1,
  pageSize: 5,
  total: 0,
})

const handlePageChange = (page: number) => {
  pagination.currentPage = page
  fetchCandidates()
}

const detailsDialogVisible = ref(false)
const selectedCandidate = ref<Candidate | null>(null)

const showDetails = (candidate: Candidate) => {
  selectedCandidate.value = candidate
  detailsDialogVisible.value = true
}

// 下载简历：走鉴权接口拿 Blob，再用临时 URL 打开
const openResume = async (candidate: Candidate) => {
  try {
    const blob = await downloadCandidateResume(candidate.id)
    const url = URL.createObjectURL(blob)
    window.open(url, '_blank')
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  } catch (error: any) {
    // 区分错误类型：403 无权访问、404 文件缺失，其余归为下载失败
    const status = error?.response?.status
    if (status === 403) {
      ElMessage.error('无权访问该候选人的简历')
    } else if (status === 404) {
      ElMessage.error('简历文件不存在')
    } else {
      ElMessage.error('简历下载失败，请稍后重试')
    }
  }
}

// 修改状态
const statusDialogVisible = ref(false)
const statusSubmitting = ref(false)
const statusCandidate = ref<Candidate | null>(null)
const statusForm = ref<{
  status: string
  interview_time: Date | null
  rejection_reason: string
}>({
  status: '',
  interview_time: null,
  rejection_reason: '',
})

const selectableNextStatuses = computed(() => {
  if (!statusCandidate.value) return []
  const current = statusCandidate.value.status as unknown as string
  return ALLOWED_TRANSITIONS[current] ?? []
})

const openStatusDialog = (candidate: Candidate) => {
  statusCandidate.value = candidate
  statusForm.value = {
    status: '',
    interview_time: null,
    rejection_reason: '',
  }
  statusDialogVisible.value = true
}

const submitStatus = async () => {
  if (!statusCandidate.value) return
  if (!statusForm.value.status) {
    ElMessage.error('请选择目标状态')
    return
  }
  if (
    statusForm.value.status === CandidateStatusEnum.WAITING_FOR_INTERVIEW &&
    !statusForm.value.interview_time
  ) {
    ElMessage.error('请选择面试时间')
    return
  }
  if (
    statusForm.value.status === CandidateStatusEnum.INTERVIEW_REJECTED &&
    !statusForm.value.rejection_reason
  ) {
    ElMessage.error('请输入未通过原因')
    return
  }

  // 人工通过/重试需要二次确认
  if (statusForm.value.status === CandidateStatusEnum.MANUAL_PASSED) {
    try {
      await ElMessageBox.confirm(
        '系统将重新发送面试邀请邮件，并开始协商面试时间。是否继续？',
        '人工通过/重试',
        { confirmButtonText: '继续', cancelButtonText: '取消', type: 'warning' },
      )
    } catch {
      return
    }
  }

  try {
    statusSubmitting.value = true
    await updateCandidateStatus(statusCandidate.value.id, {
      status: statusForm.value.status as any,
      interview_time: statusForm.value.interview_time
        ? (statusForm.value.interview_time as any).toISOString()
        : null,
      rejection_reason: statusForm.value.rejection_reason || null,
    })
    ElMessage.success('状态修改成功')
    statusDialogVisible.value = false
    await fetchCandidates()
  } catch (e) {
    ElMessage.error('状态修改失败')
  } finally {
    statusSubmitting.value = false
  }
}

// 人工闸门：决定要不要发面试邀请邮件（唤醒暂停的招聘图）
const manualDialogVisible = ref(false)
const manualSubmitting = ref(false)
const manualCandidate = ref<Candidate | null>(null)

const openManualDecision = (candidate: Candidate) => {
  manualCandidate.value = candidate
  manualDialogVisible.value = true
}

const handleManualDecision = async (decision: 'approve' | 'reject' | 'archive') => {
  if (!manualCandidate.value) return
  try {
    manualSubmitting.value = true
    await submitManualDecision(manualCandidate.value.id, decision)
    const tips: Record<string, string> = {
      approve: '已发送面试邀请',
      reject: '已淘汰该候选人',
      archive: '已归档该候选人',
    }
    ElMessage.success(tips[decision])
    manualDialogVisible.value = false
    await fetchCandidates()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail ?? '操作失败，请稍后重试')
  } finally {
    manualSubmitting.value = false
  }
}

const fetchCandidates = async () => {
  try {
    const response = await getCandidates({
      page: pagination.currentPage,
      size: pagination.pageSize,
    })
    candidates.value = response.candidates || []
    pagination.total = response.total ?? 0
  } catch (error) {
    ElMessage.error('获取候选人列表失败')
  }
}

const fetchPositions = async () => {
  try {
    const response = await getPositionList({})
    positions.value = response.positions || []
  } catch (error) {
    ElMessage.error('获取职位列表失败')
  }
}

// Map candidate status to Element Plus tag type
const getStatusTagType = (status: string): string => {
  switch (status) {
    case '已投递':
      return 'info'
    case 'AI筛选失败':
      return 'danger'
    case 'AI筛选成功':
      return 'success'
    case '人工通过/重试':
      return 'warning'
    case '待面试':
      return 'warning'
    case '拒绝面试':
      return 'info'
    case '面试通过':
      return 'success'
    case '面试未通过':
      return 'danger'
    case '已入职':
      return 'success'
    case '已拒绝':
      return 'danger'
    case '协商超时':
      return 'danger'
    case '处理异常':
      return 'danger'
    case '待人工确认':
      return 'warning'
    case '面试时间协商中':
      return 'primary'
    default:
      return 'default'
  }
}

// AI Score dialog
const aiScoreDialogVisible = ref(false)
const aiScore = ref<CandidateAIScore | null>(null)

const showAIScore = async (candidate: Candidate) => {
  try {
    const data = await getCandidateAiScore(candidate.id)
    aiScore.value = data.ai_score
    aiScoreDialogVisible.value = true
  } catch (error) {
    ElMessage.error('暂无AI评分！')
  }
}

// 删除候选人
const handleDelete = async (candidate: Candidate) => {
  try {
    await ElMessageBox.confirm(
      `确定删除候选人「${candidate.name}」吗？此操作将同时删除其 AI 评分和面试记录，且不可恢复。`,
      '删除确认',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'error' },
    )
  } catch {
    return // 用户取消
  }

  try {
    await deleteCandidate(candidate.id)
    ElMessage.success('候选人已删除')
    // 删除后若当前页已空且不是第一页，回退一页
    if (candidates.value.length === 1 && pagination.currentPage > 1) {
      pagination.currentPage -= 1
    }
    await fetchCandidates()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? '删除失败')
  }
}

onMounted(() => {
  fetchCandidates()
  fetchPositions()
})
</script>
