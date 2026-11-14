<template>
  <div class="page-panel space-y-4">
    <div class="flex justify-between items-center">
      <h1 class="page-title">职位管理</h1>
      <el-button type="primary" @click="handleAdd"
        ><el-icon><Plus /></el-icon> 添加职位</el-button
      >
    </div>

    <!-- 职位列表 -->
    <el-table :data="positions" stripe border>
      <el-table-column prop="title" label="标题">
        <template #default="{ row }">
          <el-popover placement="right" :width="500" trigger="hover" :show-after="300">
            <template #reference>
              <span class="cursor-pointer text-blue-600 hover:text-blue-800">
                {{ row.title }}
              </span>
            </template>
            <div class="position-popover">
              <h3 class="text-lg font-bold mb-3">{{ row.title }}</h3>
              <div class="space-y-2 text-sm">
                <div v-if="row.department">
                  <span class="font-semibold text-desc">部门：</span>
                  <span>{{ row.department.name }}</span>
                </div>
                <div v-if="row.description">
                  <span class="font-semibold text-desc">职位描述：</span>
                  <p class="mt-1 whitespace-pre-wrap">{{ row.description }}</p>
                </div>
                <div v-if="row.requirements">
                  <span class="font-semibold text-desc">职位要求：</span>
                  <p class="mt-1 whitespace-pre-wrap">{{ row.requirements }}</p>
                </div>
                <div>
                  <span class="font-semibold text-desc">薪资范围：</span>
                  <span>
                    {{ row.min_salary ?? '面议' }} - {{ row.max_salary ?? '面议' }} 元/月
                  </span>
                </div>
                <div>
                  <span class="font-semibold text-desc">招聘人数：</span>
                  <span>{{ row.recruitment_count }} 人</span>
                </div>
                <div>
                  <span class="font-semibold text-desc">学历要求：</span>
                  <span>{{ row.education }}</span>
                </div>
                <div>
                  <span class="font-semibold text-desc">工作年限：</span>
                  <span>{{ row.work_year }} 年</span>
                </div>
                <div>
                  <span class="font-semibold text-desc">状态：</span>
                  <el-tag :type="row.is_open ? 'success' : 'danger'" size="small">
                    {{ row.is_open ? '开放' : '关闭' }}
                  </el-tag>
                </div>
                <div v-if="row.deadline">
                  <span class="font-semibold text-desc">截止日期：</span>
                  <span>{{ row.deadline }}</span>
                </div>
                <div>
                  <span class="font-semibold text-desc">创建时间：</span>
                  <span>{{ row.created_at }}</span>
                </div>
              </div>
            </div>
          </el-popover>
        </template>
      </el-table-column>
      <el-table-column prop="department.name" label="部门" />
      <el-table-column label="薪资范围">
        <template #default="{ row }"> {{ row.min_salary }} - {{ row.max_salary }} </template>
      </el-table-column>
      <el-table-column prop="recruitment_count" label="招聘人数" />
      <el-table-column prop="education" label="学历要求" />
      <el-table-column label="工作年限">
        <template #default="{ row }"> {{ row.work_year }} 年 </template>
      </el-table-column>
      <el-table-column label="发布者">
        <template #default="{ row }">
          {{ row.creator?.name ?? row.creator?.username ?? '-' }}
        </template>
      </el-table-column>
      <el-table-column label="是否开放">
        <template #default="{ row }">
          <el-tag :type="row.is_open ? 'success' : 'danger'">
            {{ row.is_open ? '是' : '否' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" />
      <el-table-column label="操作" width="180">
        <template #default="{ row }">
          <el-button type="primary" size="small" @click="handleEdit(row)">编辑</el-button>
          <el-popconfirm
            title="确定删除这个职位吗?"
            @confirm="handleDelete(row.id)"
            confirm-button-text="确定"
            cancel-button-text="取消"
          >
            <template #reference>
              <el-button type="danger" size="small">删除</el-button>
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页 -->
    <div class="mt-4 flex justify-end">
      <el-pagination
        background
        layout="prev, pager, next"
        :total="pagination.total"
        :page-size="pagination.pageSize"
        v-model:current-page="pagination.currentPage"
        @current-change="handlePageChange"
      />
    </div>

    <!-- 添加/编辑职位对话框 -->
    <el-dialog v-model="dialog.visible" :title="dialog.title" width="1000">
      <el-form :model="positionForm" label-width="auto">
        <el-form-item label="职位标题">
          <el-input v-model="positionForm.title" placeholder="请输入标题" />
        </el-form-item>
        <el-form-item label="职位描述">
          <el-input
            v-model="positionForm.description"
            type="textarea"
            placeholder="请输入描述"
            :rows="8"
          />
        </el-form-item>
        <el-form-item label="职位要求">
          <el-input
            v-model="positionForm.requirements"
            type="textarea"
            placeholder="请输入要求"
            :rows="8"
          />
        </el-form-item>
        <el-form-item label="薪资范围">
          <el-input-number v-model="positionForm.min_salary" :min="0" />
          <span class="mx-2">-</span>
          <el-input-number v-model="positionForm.max_salary" :min="0" />
          <span class="ml-2">元/月</span>
        </el-form-item>
        <el-form-item label="招聘人数">
          <el-input-number v-model="positionForm.recruitment_count" :min="1" />
        </el-form-item>
        <el-form-item label="学历要求">
          <el-select v-model="positionForm.education" placeholder="请选择">
            <el-option
              v-for="item in Object.values(EducationEnum)"
              :key="item"
              :label="item"
              :value="item"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="工作年限">
          <el-input-number v-model="positionForm.work_year" :min="0" />
        </el-form-item>
        <el-form-item label="是否开放">
          <el-switch v-model="positionForm.is_open" />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="dialog.visible = false">取消</el-button>
          <el-button type="primary" @click="handleSubmit">确认</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import {
  createPosition,
  getPositionList,
  deletePosition,
  updatePosition,
  type Position,
  type PositionCreateData,
  type PositionUpdateData,
  EducationEnum,
} from '@/apis/position_api'

const dialog = reactive({
  visible: false,
  title: '',
})
const isEditMode = ref(false)
const editingPositionId = ref<string | null>(null)

const positions = ref<Position[]>([])
const positionForm = reactive<PositionCreateData>({
  title: '',
  description: '',
  requirements: '',
  min_salary: 0,
  max_salary: 0,
  deadline: null,
  recruitment_count: 1,
  education: EducationEnum.BenKe,
  work_year: 0,
  is_open: true,
})

const pagination = reactive({
  currentPage: 1,
  pageSize: 10,
  total: 0,
})

const fetchPositions = async () => {
  try {
    const data = await getPositionList({
      page: pagination.currentPage,
      size: pagination.pageSize,
    })
    // @ts-ignore
    positions.value = data.positions
    console.log('Fetched positions:', positions.value)
  } catch (error) {
    ElMessage.error('获取职位列表失败')
    console.error(error)
  }
}

const resetForm = () => {
  Object.assign(positionForm, {
    title: '',
    description: '',
    requirements: '',
    min_salary: 0,
    max_salary: 0,
    deadline: null,
    recruitment_count: 1,
    education: EducationEnum.BenKe,
    work_year: 0,
    is_open: true,
  })
}

const handleAdd = () => {
  resetForm()
  isEditMode.value = false
  editingPositionId.value = null
  dialog.title = '添加职位'
  dialog.visible = true
}

const handleEdit = (row: Position) => {
  // 填充表单
  Object.assign(positionForm, {
    title: row.title,
    description: row.description,
    requirements: row.requirements,
    min_salary: row.min_salary,
    max_salary: row.max_salary,
    deadline: row.deadline,
    recruitment_count: row.recruitment_count,
    education: row.education,
    work_year: row.work_year,
    is_open: row.is_open,
  })
  isEditMode.value = true
  editingPositionId.value = row.id
  dialog.title = '编辑职位'
  dialog.visible = true
}

const handleSubmit = async () => {
  try {
    if (isEditMode.value && editingPositionId.value) {
      // 编辑模式：只提交修改过的字段
      const updateData: PositionUpdateData = {
        title: positionForm.title,
        description: positionForm.description,
        requirements: positionForm.requirements,
        min_salary: positionForm.min_salary,
        max_salary: positionForm.max_salary,
        deadline: positionForm.deadline,
        recruitment_count: positionForm.recruitment_count,
        education: positionForm.education,
        work_year: positionForm.work_year,
        is_open: positionForm.is_open,
      }
      await updatePosition(editingPositionId.value, updateData)
      ElMessage.success('职位更新成功')
    } else {
      // 添加模式
      await createPosition(positionForm as PositionCreateData)
      ElMessage.success('职位创建成功')
    }
    dialog.visible = false
    await fetchPositions()
  } catch (error) {
    ElMessage.error(isEditMode.value ? '职位更新失败' : '职位创建失败')
    console.error(error)
  }
}

const handleDelete = async (id: string) => {
  try {
    await deletePosition(id)
    ElMessage.success('职位删除成功')
    await fetchPositions()
  } catch (error) {
    ElMessage.error('职位删除失败')
    console.error(error)
  }
}

const handlePageChange = (page: number) => {
  pagination.currentPage = page
  fetchPositions()
}

onMounted(() => {
  fetchPositions()
})
</script>
