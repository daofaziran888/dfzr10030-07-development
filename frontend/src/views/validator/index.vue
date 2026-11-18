<template>
  <section class="page" data-module="validator">
    <header class="page-head">
      <div>
        <h2>采集结果校验管理</h2>
        <p class="page-desc">维护校验规则，围绕规则编号、校验字段、校验类型、期望值做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记校验规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出采集结果校验清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集结果校验数据，可先登记校验规则</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记校验规则</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section class="panel" data-testid="audit-panel">
      <header class="panel-head">
        <div>
          <h3>解析批次核对</h3>
          <p class="page-desc">
            只读读取解析批次清单与标准化字段包，按来源文件汇总字段缺失和类型异常，
            回写核对结论并生成待复核清单。
          </p>
        </div>
        <button class="btn ghost" type="button" data-testid="refresh-audit" @click="loadAudit()">刷新批次</button>
      </header>

      <table class="data-table">
        <thead>
          <tr><th>批次编号</th><th>映射口径</th><th>状态</th><th>成功/失败</th><th>字段顺序</th><th>校验状态</th><th>复核版本</th><th>待复核</th><th>动作</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in auditBatches" :key="row.id" :data-testid="`audit-row-${row.id}`" :data-status="String(row.校验状态)">
            <td>{{ row.批次编号 }}</td>
            <td>{{ row.映射口径 }}</td>
            <td>{{ row.状态 }}</td>
            <td>{{ row.成功条数 }}/{{ row.失败条数 }}</td>
            <td>{{ (row.字段顺序 || []).join('、') }}</td>
            <td>{{ row.校验状态 }}</td>
            <td>v{{ row.复核版本 }}</td>
            <td>{{ row.待复核条数 }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`audit-review-${row.id}`" @click="reviewBatch(row)">
                {{ row.校验状态 === '已校验' ? '重新核对' : '执行核对' }}
              </button>
              <button class="link" type="button" :data-testid="`audit-reviews-${row.id}`" @click="openReviews(row)">待复核清单</button>
            </td>
          </tr>
          <tr v-if="!auditBatches.length">
            <td colspan="9" class="empty-state">暂无解析批次，请先在格式解析器页提交批量解析</td>
          </tr>
        </tbody>
      </table>

      <p v-if="auditMessage" class="hint-text" data-testid="audit-message">{{ auditMessage }}</p>

      <div v-if="auditResult" class="batch-detail" data-testid="audit-result">
        <h4>核对汇总</h4>
        <p class="hint-text">
          缺失字段 {{ auditResult.汇总?.缺失字段数 ?? 0 }} 处 · 类型异常 {{ auditResult.汇总?.类型异常数 ?? 0 }} 处 ·
          通过 {{ auditResult.汇总?.通过条数 ?? 0 }} 条 · 待复核 {{ auditResult.汇总?.待复核条数 ?? 0 }} 条 ·
          业务记录 {{ auditResult.业务记录数 ?? 0 }} 条
        </p>
        <table class="data-table">
          <thead>
            <tr><th>来源文件</th><th>输入编号</th><th>映射版本</th><th>缺失字段</th><th>类型异常</th><th>结论</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in auditResult.按来源文件" :key="item.输入编号" :data-testid="`audit-file-${item.输入编号}`">
              <td>{{ item.来源文件 }}</td>
              <td>{{ item.输入编号 }}</td>
              <td>{{ item.映射版本 }}</td>
              <td>{{ item.缺失字段.map((field: string) => `${field}（缺失）`).join('、') || '—' }}</td>
              <td>{{ item.类型异常.map((entry: any) => `${entry.字段}（类型异常）${entry.原因}`).join('；') || '—' }}</td>
              <td>{{ item.结论 }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="reviews.length" class="batch-detail" data-testid="review-list">
        <h4>待复核清单{{ reviewBatchNo ? `（${reviewBatchNo}）` : '' }}</h4>
        <table class="data-table">
          <thead>
            <tr><th>复核编号</th><th>批次编号</th><th>来源文件</th><th>字段</th><th>问题类型</th><th>说明</th><th>复核状态</th><th>复核版本</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in reviews" :key="row.id" :data-testid="`review-row-${row.id}`" :data-status="String(row.复核状态)">
              <td>{{ row.复核编号 }}</td>
              <td>{{ row.批次编号 }}</td>
              <td>{{ row.来源文件 }}</td>
              <td>{{ row.字段 }}</td>
              <td>{{ row.问题类型 }}</td>
              <td>{{ row.说明 }}</td>
              <td>{{ row.复核状态 }}</td>
              <td>v{{ row.复核版本 }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集结果校验记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/validator'
const columns = ["规则编号", "校验字段", "校验类型", "期望值", "容错范围", "采样比例", "阻断级别", "规则状态"]
const actions = ["提交测试", "启用规则", "停用规则"]
const statuses = ["草稿", "待测试", "已启用", "已停用"]
const metricNames = ["规则总数", "已启用规则", "待测试规则"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'validator.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('采集结果校验动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集结果校验操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('校验规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集结果校验列表读取失败'
  }
}

type AuditBatch = Record<string, any>
type ReviewRow = Record<string, any>

const auditBatches = ref<AuditBatch[]>([])
const auditMessage = ref('')
const auditResult = ref<Record<string, any> | null>(null)
const reviews = ref<ReviewRow[]>([])
const reviewBatchNo = ref('')

async function loadAudit(clearMessage = true) {
  if (clearMessage) auditMessage.value = ''
  try {
    const payload = await fetchJson<{ items: AuditBatch[] }>('/api/validator/batches')
    auditBatches.value = payload.items ?? []
  } catch (error) {
    auditMessage.value = error instanceof Error ? error.message : '解析批次读取失败'
  }
}

async function reviewBatch(row: AuditBatch) {
  auditMessage.value = ''
  reviews.value = []
  try {
    const expected = Number(row.复核版本) > 0 ? Number(row.复核版本) : undefined
    const response = await request(`/api/validator/batches/${row.id}/review`, {
      method: 'POST',
      body: JSON.stringify({ reviewer: '值班校验', expected_version: expected }),
    })
    const payload = await response.json()
    auditMessage.value = payload.message || (payload.ok ? '核对完成' : '核对未生效')
    if (payload.ok) {
      auditResult.value = payload.result
      reviews.value = payload.result?.待复核清单 ?? []
      reviewBatchNo.value = String(row.批次编号)
      await loadAudit(false)
    } else {
      auditResult.value = null
      await loadAudit(false)
    }
  } catch (error) {
    auditMessage.value = error instanceof Error ? error.message : '核对失败'
  }
}

async function openReviews(row: AuditBatch) {
  auditMessage.value = ''
  try {
    const payload = await fetchJson<{ items: ReviewRow[] }>(
      `/api/validator/reviews?batch_no=${encodeURIComponent(String(row.批次编号))}`,
    )
    reviews.value = payload.items ?? []
    reviewBatchNo.value = String(row.批次编号)
  } catch (error) {
    auditMessage.value = error instanceof Error ? error.message : '待复核清单读取失败'
  }
}

onMounted(async () => {
  await Promise.all([reload(), loadAudit()])
})
</script>
