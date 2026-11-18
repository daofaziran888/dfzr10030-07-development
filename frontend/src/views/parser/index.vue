<template>
  <section class="page" data-module="parser">
    <header class="page-head">
      <div>
        <h2>格式解析器管理</h2>
        <p class="page-desc">维护解析配置，围绕解析编号、输入格式、输出格式、字段映射做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记解析配置</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出格式解析器清单</button>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无格式解析器数据，可先登记解析配置</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记解析配置</h3>
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

    <section class="panel" data-testid="batch-panel">
      <header class="panel-head">
        <div>
          <h3>批量解析</h3>
          <p class="page-desc">一次勾选多条输入（CSV / JSON / XML），提交后逐条生成解析批次清单与标准化字段包。</p>
        </div>
        <span class="tag">批次内字段类型必须唯一</span>
      </header>

      <div class="batch-form">
        <label class="filter-item">
          <span>映射口径</span>
          <select v-model="policy" data-testid="policy">
            <option v-for="item in policies" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label class="filter-item grow">
          <span>批次备注</span>
          <input v-model="remark" data-testid="batch-remark" placeholder="可选，例如：11 月订单补采" />
        </label>
        <button class="btn" type="button" data-testid="select-all" @click="toggleAll">
          {{ allSelected ? '取消全选' : '全选输入' }}
        </button>
        <button class="btn primary" type="button" data-testid="submit-batch" :disabled="submitting" @click="submitBatch">
          {{ submitting ? '解析中…' : `提交解析（${selectedInputs.length} 条）` }}
        </button>
      </div>

      <table class="data-table">
        <thead>
          <tr><th>选择</th><th>输入编号</th><th>文件名</th><th>输入格式</th><th>命中映射</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in inputs" :key="row.id" :data-testid="`input-row-${row.id}`">
            <td>
              <input
                type="checkbox"
                :data-testid="`input-${row.id}`"
                :value="row.id"
                :checked="selectedInputs.includes(row.id)"
                @change="toggleInput(row.id)"
              />
            </td>
            <td>{{ row.输入编号 }}</td>
            <td>{{ row.文件名 }}</td>
            <td>{{ row.输入格式 }}</td>
            <td>{{ row.命中映射编号 || '—' }}</td>
            <td>{{ row.说明 || '—' }}</td>
          </tr>
          <tr v-if="!inputs.length">
            <td colspan="6" class="empty-state">暂无可解析的输入文件</td>
          </tr>
        </tbody>
      </table>

      <p v-if="batchMessage" class="hint-text" data-testid="batch-message">{{ batchMessage }}</p>
      <ul v-if="batchErrors.length" class="error-list" data-testid="batch-errors">
        <li v-for="(item, index) in batchErrors" :key="`${item.输入}-${index}`">
          <strong>{{ item.输入 }}</strong>：{{ item.失败原因 }}
        </li>
      </ul>

      <div class="panel-subhead">
        <h4>字段映射版本</h4>
        <button class="link" type="button" data-testid="toggle-mappings" @click="showMappings = !showMappings">
          {{ showMappings ? '收起映射版本' : '查看映射版本' }}
        </button>
      </div>
      <table v-if="showMappings" class="data-table" data-testid="mapping-table">
        <thead>
          <tr><th>映射编号</th><th>输入格式</th><th>版本</th><th>目标字段</th><th>类型转换</th><th>确认时间</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in mappings" :key="row.id" :data-testid="`mapping-row-${row.id}`">
            <td>{{ row.映射编号 }}</td>
            <td>{{ row.输入格式 }}</td>
            <td>{{ row.映射版本 }}</td>
            <td>{{ (row.字段映射 || []).map((item: any) => item.目标字段).join('、') }}</td>
            <td>{{ typeSummary(row.类型转换) }}</td>
            <td>{{ row.确认时间 }}</td>
            <td>{{ row.状态 }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel" data-testid="batch-list-panel">
      <header class="panel-head">
        <div>
          <h3>解析批次清单</h3>
          <p class="page-desc">每条输入都能追溯到所用映射版本、标准化字段结果与失败原因。</p>
        </div>
        <button class="btn ghost" type="button" data-testid="refresh-batches" @click="loadBatches">刷新批次</button>
      </header>

      <table class="data-table">
        <thead>
          <tr><th>批次编号</th><th>映射口径</th><th>状态</th><th>输入条数</th><th>成功/失败</th><th>字段顺序</th><th>校验状态</th><th>复核版本</th><th>动作</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in batches" :key="row.id" :data-testid="`batch-row-${row.id}`" :data-status="String(row.状态)">
            <td>{{ row.批次编号 }}</td>
            <td>{{ row.映射口径 }}</td>
            <td>{{ row.状态 }}</td>
            <td>{{ row.输入条数 }}</td>
            <td>{{ row.成功条数 }}/{{ row.失败条数 }}</td>
            <td>{{ (row.字段顺序 || []).join('、') }}</td>
            <td>{{ row.校验状态 }}</td>
            <td>v{{ row.复核版本 }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`batch-view-${row.id}`" @click="openBatch(row.id)">查看明细</button>
              <button class="link" type="button" :data-testid="`batch-reparse-${row.id}`" @click="reparseBatch(row.id)">按原映射重算</button>
            </td>
          </tr>
          <tr v-if="!batches.length">
            <td colspan="9" class="empty-state">暂无解析批次，先勾选输入并提交解析</td>
          </tr>
        </tbody>
      </table>

      <div v-if="activeBatch" class="batch-detail" data-testid="batch-detail">
        <header class="panel-head">
          <div>
            <h4>{{ activeBatch.批次编号 }} 明细</h4>
            <p class="page-desc">
              口径 {{ activeBatch.映射口径 }} · 原摘要 {{ activeBatch.原摘要 }} · 当前摘要 {{ activeBatch.当前摘要 }} ·
              重算 {{ activeBatch.重算次数 }} 次
            </p>
          </div>
          <button class="btn ghost" type="button" data-testid="close-batch-detail" @click="activeBatch = null">关闭明细</button>
        </header>

        <h4>映射版本清单</h4>
        <table class="data-table">
          <thead>
            <tr><th>输入编号</th><th>来源文件</th><th>输入格式</th><th>映射编号</th><th>映射版本</th><th>映射确认时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in activeBatch.映射版本清单" :key="item.输入编号" :data-testid="`map-line-${item.输入编号}`">
              <td>{{ item.输入编号 }}</td>
              <td>{{ item.文件名 }}</td>
              <td>{{ item.输入格式 }}</td>
              <td>{{ item.映射编号 }}</td>
              <td>{{ item.映射版本 }}</td>
              <td>{{ item.映射确认时间 }}</td>
            </tr>
          </tbody>
        </table>

        <h4>标准化字段包</h4>
        <p class="hint-text">
          字段顺序：{{ activeBatch.标准化字段包?.字段顺序?.join(' → ') || '—' }}；
          字段类型：{{ typeSummary(activeBatch.标准化字段包?.字段类型表) }}
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th>输入编号</th><th>来源文件</th><th>映射版本</th><th>状态</th>
              <th>字段结果</th><th>失败原因</th><th>校验结论</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in activeBatch.items"
              :key="item.输入编号"
              :data-testid="`batch-item-${item.输入编号}`"
              :data-status="item.状态"
            >
              <td>{{ item.输入编号 }}</td>
              <td>{{ item.文件名 }}</td>
              <td>{{ item.映射编号 }}@{{ item.映射版本 }}</td>
              <td>{{ item.状态 }}</td>
              <td>{{ fieldText(item.标准化字段) }}</td>
              <td>{{ item.失败原因 || '—' }}</td>
              <td>{{ item.校验结论 || '未校验' }}</td>
            </tr>
          </tbody>
        </table>

        <h4>校验回查</h4>
        <p class="hint-text" data-testid="batch-check-state">
          校验状态 {{ activeBatch.校验状态 }} · 复核状态 {{ activeBatch.复核状态 }} ·
          复核版本 v{{ activeBatch.复核版本 }} ·
          有效复核 {{ activeBatch.有效复核 ? `v${activeBatch.有效复核.复核版本}` : '无' }} ·
          业务记录 {{ activeBatch.业务记录数 }} 条
        </p>
        <button class="btn ghost" type="button" data-testid="load-conclusion" @click="loadConclusion(activeBatch.id)">
          回查校验结论
        </button>
        <p v-if="conclusion" class="hint-text" data-testid="conclusion-text">
          有效 {{ conclusion.有效 ? '是' : '否' }} · 校验状态 {{ conclusion.校验状态 }} · 待复核
          {{ conclusion.待复核清单?.length || 0 }} 条
        </p>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条格式解析器记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/parser'
const columns = ["解析编号", "输入格式", "输出格式", "字段映射", "类型转换", "空值处理", "异常策略", "解析状态"]
const actions = ["提交配置", "启用解析", "废弃解析"]
const statuses = ["草稿", "已配置", "生效中", "已废弃"]
const metricNames = ["解析器总数", "生效中解析器", "已废弃解析器"]

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
    link.download = 'parser.csv'
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
      throw new Error('格式解析器动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '格式解析器操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('解析配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '格式解析器列表读取失败'
  }
}

type MappingRow = Record<string, any>
type BatchRow = Record<string, any>
type BatchError = { 输入: string; 失败原因: string }

const policies = ref<string[]>(['最新确认', '按输入命中'])
const policy = ref('最新确认')
const remark = ref('')
const inputs = ref<MappingRow[]>([])
const mappings = ref<MappingRow[]>([])
const selectedInputs = ref<number[]>([])
const submitting = ref(false)
const batchMessage = ref('')
const batchErrors = ref<BatchError[]>([])
const batches = ref<BatchRow[]>([])
const activeBatch = ref<BatchRow | null>(null)
const conclusion = ref<Record<string, any> | null>(null)
const showMappings = ref(false)

const allSelected = computed(
  () => inputs.value.length > 0 && selectedInputs.value.length === inputs.value.length,
)

function typeSummary(table: Record<string, string> | undefined | null) {
  if (!table) return '—'
  return Object.entries(table).map(([field, kind]) => `${field}:${kind}`).join('、')
}

function fieldText(values: Record<string, unknown> | undefined | null) {
  if (!values) return '—'
  return Object.entries(values)
    .map(([field, value]) => `${field}=${value === null || value === '' ? '（空）' : value}`)
    .join('，')
}

function toggleInput(id: number) {
  selectedInputs.value = selectedInputs.value.includes(id)
    ? selectedInputs.value.filter((item) => item !== id)
    : [...selectedInputs.value, id]
}

function toggleAll() {
  selectedInputs.value = allSelected.value ? [] : inputs.value.map((row) => Number(row.id))
}

async function loadInputs() {
  try {
    const payload = await fetchJson<{ items: MappingRow[] }>('/api/parser/inputs')
    inputs.value = payload.items ?? []
  } catch (error) {
    batchErrors.value = []
    batchMessage.value = error instanceof Error ? error.message : '输入文件读取失败'
  }
}

async function loadMappings() {
  try {
    const payload = await fetchJson<{ items: MappingRow[]; policies: string[] }>('/api/parser/mappings')
    mappings.value = payload.items ?? []
    if (payload.policies?.length) policies.value = payload.policies
  } catch {
    mappings.value = []
  }
}

async function loadBatches() {
  try {
    const payload = await fetchJson<{ items: BatchRow[] }>('/api/parser/batches')
    batches.value = payload.items ?? []
    if (activeBatch.value) {
      const stillThere = batches.value.find((row) => row.id === activeBatch.value?.id)
      if (stillThere) await openBatch(Number(stillThere.id))
      else activeBatch.value = null
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '解析批次读取失败'
  }
}

async function submitBatch() {
  batchMessage.value = ''
  batchErrors.value = []
  errorMessage.value = ''
  if (!selectedInputs.value.length) {
    batchMessage.value = '至少勾选一条输入后再提交解析'
    return
  }
  submitting.value = true
  try {
    const response = await request('/api/parser/batches', {
      method: 'POST',
      body: JSON.stringify({
        inputs: selectedInputs.value,
        policy: policy.value,
        remark: remark.value,
      }),
    })
    const payload = await response.json()
    batchMessage.value = payload.message || (payload.ok ? '批次已生成' : '批次未生成')
    batchErrors.value = payload.errors ?? []
    if (payload.ok && payload.batch) {
      activeBatch.value = payload.batch
      selectedInputs.value = []
      remark.value = ''
      await loadBatches()
    }
  } catch (error) {
    batchMessage.value = error instanceof Error ? error.message : '批量解析失败'
  } finally {
    submitting.value = false
  }
}

async function openBatch(id: number) {
  conclusion.value = null
  try {
    activeBatch.value = await fetchJson<BatchRow>(`/api/parser/batches/${id}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次明细读取失败'
  }
}

async function reparseBatch(id: number) {
  errorMessage.value = ''
  try {
    const response = await request(`/api/parser/batches/${id}/reparse`, { method: 'POST' })
    const payload = await response.json()
    batchMessage.value = payload.message || '重算完成'
    if (payload.ok && payload.batch) {
      activeBatch.value = payload.batch
    }
    await loadBatches()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重算失败'
  }
}

async function loadConclusion(id: number) {
  conclusion.value = null
  try {
    conclusion.value = await fetchJson<Record<string, any>>(`/api/validator/batches/${id}/conclusion`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '校验结论读取失败'
  }
}

onMounted(async () => {
  await Promise.all([reload(), loadInputs(), loadMappings(), loadBatches()])
})
</script>
