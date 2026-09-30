<template>
  <section class="page" data-module="drilling_log">
    <header class="page-head">
      <div>
        <h2>班次编录与钻探日志</h2>
        <p class="page-desc">
          班次日志按孔深区间单向推进（待填写→已填写→已审核），跨班区间不重叠；
          审批结论一处写入，同步落到钻探台账、孔深曲线与班次待办。
        </p>
      </div>
      <div class="page-actions">
        <label class="operator-box">
          当前账号
          <select v-model="operator" @change="reloadAll">
            <option v-for="name in operators" :key="name" :value="name">{{ name }}</option>
          </select>
        </label>
        <button class="btn primary" type="button" @click="openBackfill">补录班次区间</button>
        <button class="btn" type="button" @click="openFinalDepth">登记终孔深度</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value" :class="{ warn: item.warn }">{{ item.value }}</strong>
      </article>
    </div>

    <nav class="tabs">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
        <em v-if="tab.badge !== undefined" class="tab-badge">{{ tab.badge }}</em>
      </button>
    </nav>

    <p v-if="notice" class="notice" :class="{ 'notice-error': noticeError }">{{ notice }}</p>

    <!-- 班次编录主表 -->
    <div v-show="activeTab === 'catalog'">
      <form class="filter-bar" @submit.prevent="reloadCatalog">
        <label class="filter-item">
          <span>钻孔编号</span>
          <input v-model="filters.borehole" placeholder="如 ZK-101" />
        </label>
        <label class="filter-item">
          <span>日志编号</span>
          <input v-model="filters.keyword" placeholder="按日志编号检索" />
        </label>
        <label class="filter-item">
          <span>状态</span>
          <select v-model="filters.status">
            <option value="">全部</option>
            <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
          </select>
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in catalogColumns" :key="column">{{ column }}</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in catalog" :key="String(row.id)" :class="{ locked: row.status === '已审核' }">
            <td v-for="column in catalogColumns" :key="column">
              <span v-if="column === '状态'">
                <span class="status-tag" :data-status="row.status">{{ row.status }}</span>
                <span v-if="row.终孔超深" class="over-tag">越终孔</span>
              </span>
              <span v-else-if="column === '深度区间'">{{ fmt(row.深度起) }}–{{ fmt(row.深度止) }}m</span>
              <span v-else>{{ row[column] ?? '—' }}</span>
            </td>
            <td class="row-actions">
              <button
                v-if="row.status === '待填写'"
                class="link"
                type="button"
                @click="openFill(row)"
              >填写日志</button>
              <button
                v-if="row.status === '待填写'"
                class="link"
                type="button"
                @click="openReview(row, true)"
              >跳步提交审核</button>
              <button
                v-if="row.status === '已填写'"
                class="link"
                type="button"
                @click="openReview(row, false)"
              >提交审核</button>
              <span v-if="row.status === '已审核'" class="muted-link">已锁定</span>
            </td>
          </tr>
          <tr v-if="!catalog.length">
            <td :colspan="catalogColumns.length + 1" class="empty-state">暂无班次记录，可先补录一个孔深区间</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span>共 {{ catalogTotal }} 条班次日志</span>
        <span>规则：区间 [深度起, 深度止) 不可重叠，状态只能前进，已审核不可改</span>
      </footer>
    </div>

    <!-- 钻探台账 -->
    <div v-show="activeTab === 'ledger'">
      <table class="data-table">
        <thead>
          <tr><th v-for="column in ledgerColumns" :key="column">{{ column }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in ledger" :key="String(row.id)">
            <td v-for="column in ledgerColumns" :key="column">
              <span v-if="column === '孔深区间'">{{ fmt(row.深度起) }}–{{ fmt(row.深度止) }}m</span>
              <span v-else-if="column === '生效深度止'">
                {{ fmt(row.生效深度止) }}m<span v-if="row.终孔超深" class="over-tag">已按终孔封顶</span>
              </span>
              <span v-else>{{ row[column] ?? '—' }}</span>
            </td>
          </tr>
          <tr v-if="!ledger.length">
            <td :colspan="ledgerColumns.length" class="empty-state">暂无台账记录，班次审核通过后自动入台账</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 孔深曲线 -->
    <div v-show="activeTab === 'curve'">
      <table class="data-table curve-table">
        <thead>
          <tr><th>钻孔</th><th>序号</th><th>班次</th><th>上报深度止</th><th>生效孔深（曲线）</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in curve" :key="String(row.id)">
            <td>{{ row.钻孔编号 }}</td>
            <td>{{ row.序号 }}</td>
            <td>第{{ row.班次序号 }}班 · {{ row.日志编号 }}</td>
            <td>{{ fmt(row.深度止) }}m</td>
            <td>{{ fmt(row.生效孔深) }}m</td>
            <td><span v-if="row.终孔超深" class="over-tag">超终孔，已按现场确认封顶</span><span v-else class="muted">—</span></td>
          </tr>
          <tr v-if="!curve.length">
            <td colspan="6" class="empty-state">暂无曲线点，审核通过后按孔深区间单向累加</td>
          </tr>
        </tbody>
      </table>
      <div v-for="group in curveGroups" :key="group.borehole" class="curve-chart">
        <h4>{{ group.borehole }} 孔深曲线（按班次推进）</h4>
        <svg :viewBox="`0 0 ${chartWidth} ${chartHeight}`" role="img" :aria-label="`${group.borehole}孔深曲线`">
          <line :x1="pad" :y1="chartHeight - pad" :x2="chartWidth - pad / 2" :y2="chartHeight - pad" class="axis" />
          <line :x1="pad" :y1="pad / 2" :x2="pad" :y2="chartHeight - pad" class="axis" />
          <polyline
            :points="group.points"
            fill="none"
            stroke="var(--brand)"
            stroke-width="2"
          />
          <circle
            v-for="p in group.pointList"
            :key="p.id"
            :cx="p.x"
            :cy="p.y"
            r="3.5"
            :class="{ over: p.over }"
          >
            <title>{{ p.label }}</title>
          </circle>
          <text v-for="p in group.pointList" :key="`t${p.id}`" :x="p.x" :y="p.y - 8" class="chart-label">
            {{ fmt(p.depth) }}
          </text>
        </svg>
      </div>
    </div>

    <!-- 班次待办 -->
    <div v-show="activeTab === 'todo'">
      <table class="data-table">
        <thead>
          <tr><th v-for="column in todoColumns" :key="column">{{ column }}</th><th>处理</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in todos" :key="String(row.id)">
            <td v-for="column in todoColumns" :key="column">
              <span v-if="column === '深度区间'">{{ fmt(row.深度起) }}–{{ fmt(row.深度止) }}m</span>
              <span v-else-if="column === '状态'" class="status-tag" :data-status="row.状态">{{ row.状态 }}</span>
              <span v-else-if="column === '提示'">
                <span v-if="row.终孔超深" class="over-tag">区间越过终孔，先核对</span>
                <span v-else class="muted">—</span>
              </span>
              <span v-else>{{ row[column] ?? '—' }}</span>
            </td>
            <td class="row-actions">
              <button
                class="link"
                type="button"
                @click="row.状态 === '待填写' ? openFill(row) : openReview(row, false)"
              >{{ row.待办事项 }}</button>
            </td>
          </tr>
          <tr v-if="!todos.length">
            <td :colspan="todoColumns.length + 1" class="empty-state">没有未完成班次，待办里不再残留已完成记录</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 操作序号 -->
    <div v-show="activeTab === 'journal'">
      <p class="muted small">
        每个钻孔维护独立操作序号；两个账号同区间补录时只允许序号匹配的一方成功。
        掉线后按下表最新序号续做，携带相同操作编号的请求会按原结果幂等返回。
        <button class="link" type="button" @click="replayPending">重发本地未确认操作（{{ pendingOps.length }}）</button>
      </p>
      <table class="data-table">
        <thead>
          <tr><th>钻孔</th><th>序号</th><th>操作</th><th>班次</th><th>操作人</th><th>说明</th><th>操作编号</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in journal" :key="String(row.id)">
            <td>{{ row.钻孔编号 }}</td>
            <td><strong>{{ row.seq }}</strong></td>
            <td>{{ row.操作 }}</td>
            <td>{{ row.内容?.['班次'] ?? (row.日志id ?? '—') }}</td>
            <td>{{ row.操作人 }}</td>
            <td>{{ row.说明 || (row.内容?.['跳步原因'] ?? '—') }}</td>
            <td class="muted small">{{ row.operation_id || '—' }}</td>
            <td class="muted small">{{ row.时间 }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 补录班次弹窗 -->
    <div v-if="dialog === 'backfill'" class="modal-mask" @click.self="dialog = ''">
      <form class="modal" @submit.prevent="submitBackfill">
        <h3>补录班次区间</h3>
        <p class="muted small">补录进入「待填写」，只能接在已有区间后面；区间重叠或越过现场终孔会被拒绝。</p>
        <label v-for="f in backfillFields" :key="f.key" class="form-item">
          <span>{{ f.label }}<i v-if="f.required">*</i></span>
          <input v-if="f.type !== 'select'" v-model="backfillForm[f.key]" :type="f.type || 'text'" :placeholder="f.placeholder" />
          <select v-else v-model="backfillForm[f.key]">
            <option v-for="opt in f.options" :key="opt" :value="opt">{{ opt }}</option>
          </select>
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="dialog = ''">取消</button>
          <button class="btn primary" type="submit">提交补录</button>
        </div>
      </form>
    </div>

    <!-- 填写日志弹窗 -->
    <div v-if="dialog === 'fill'" class="modal-mask" @click.self="dialog = ''">
      <form class="modal" @submit.prevent="submitFill">
        <h3>填写班报内容：{{ activeRow?.日志编号 }}</h3>
        <p class="muted small">
          {{ activeRow?.钻孔编号 }} · {{ activeRow?.班次 }} ·
          区间 {{ fmt(activeRow?.深度起) }}–{{ fmt(activeRow?.深度止) }}m
        </p>
        <label class="form-item">
          <span>岩层描述</span>
          <textarea v-model="fillForm.岩层描述" rows="3" placeholder="本班岩性、破碎程度、返水情况"></textarea>
        </label>
        <label class="form-item">
          <span>水位深度（m）</span>
          <input v-model="fillForm.水位深度" type="number" step="0.1" min="0" />
        </label>
        <label class="form-item">
          <span>钻探人员</span>
          <input v-model="fillForm.钻探人员" placeholder="本班负责人" />
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="dialog = ''">取消</button>
          <button class="btn primary" type="submit">确认填写</button>
        </div>
      </form>
    </div>

    <!-- 审核弹窗（含跳步原因） -->
    <div v-if="dialog === 'review'" class="modal-mask" @click.self="dialog = ''">
      <form class="modal" @submit.prevent="submitReview">
        <h3>{{ reviewJumping ? '跳步提交审核' : '提交审核' }}：{{ activeRow?.日志编号 }}</h3>
        <p class="muted small">
          {{ activeRow?.钻孔编号 }} · 区间 {{ fmt(activeRow?.深度起) }}–{{ fmt(activeRow?.深度止) }}m
        </p>
        <label class="form-item">
          <span>审批结论<i>*</i></span>
          <textarea v-model="reviewForm.审批结论" rows="2" placeholder="同意归档 / 需核对项"></textarea>
        </label>
        <label v-if="reviewJumping" class="form-item">
          <span>跳步原因<i>*</i></span>
          <textarea v-model="reviewForm.原因" rows="2" placeholder="从待填写直接审核必须说明依据，如：依据现场班报底稿补核"></textarea>
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="dialog = ''">取消</button>
          <button class="btn primary" type="submit">审核通过并同步</button>
        </div>
      </form>
    </div>

    <!-- 终孔深度弹窗 -->
    <div v-if="dialog === 'final'" class="modal-mask" @click.self="dialog = ''">
      <form class="modal" @submit.prevent="submitFinalDepth">
        <h3>登记终孔深度</h3>
        <p class="muted small">现场终孔确认值优先：一旦登记现场值，补充上报不能再覆盖；台账与曲线生效深度立即重算。</p>
        <label class="form-item">
          <span>钻孔编号<i>*</i></span>
          <input v-model="finalForm.钻孔编号" placeholder="如 ZK-104" />
        </label>
        <label class="form-item">
          <span>现场终孔确认深度（m）</span>
          <input v-model="finalForm.现场终孔确认深度" type="number" step="0.1" min="0" placeholder="现场签字确认值，优先采用" />
        </label>
        <label class="form-item">
          <span>补充上报深度（m）</span>
          <input v-model="finalForm.补充上报深度" type="number" step="0.1" min="0" placeholder="暂无现场确认时临时采用" />
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="dialog = ''">取消</button>
          <button class="btn primary" type="submit">登记并重算</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/drilling_log'
const statuses = ['待填写', '已填写', '已审核']
const operators = ['张工（白班）', '赵班（夜班）', '李审核', '周补录']

const operator = ref(operators[0])
const activeTab = ref('catalog')
const dialog = ref('')
const notice = ref('')
const noticeError = ref(false)
const catalog = ref<Row[]>([])
const catalogTotal = ref(0)
const ledger = ref<Row[]>([])
const curve = ref<Row[]>([])
const todos = ref<Row[]>([])
const journal = ref<Row[]>([])
const latestSeq = ref<Record<string, number>>({})
const summary = ref<Record<string, number | boolean>>({})
const activeRow = ref<Row | null>(null)
const reviewJumping = ref(false)
const filters = reactive({ borehole: '', keyword: '', status: '' })

// 掉线暂存：请求没拿到服务端确认时先落本地，恢复后按 operation_id 重发（幂等）。
type PendingOp = { url: string; body: Record<string, unknown>; operationId: string; borehole: string }
const pendingOps = ref<PendingOp[]>([])
const PENDING_KEY = 'drilling_log_pending_ops'

function loadPending() {
  try {
    pendingOps.value = JSON.parse(localStorage.getItem(PENDING_KEY) ?? '[]')
  } catch {
    pendingOps.value = []
  }
}
function savePending() {
  localStorage.setItem(PENDING_KEY, JSON.stringify(pendingOps.value))
}

function newOperationId() {
  return `op-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

function fmt(value: unknown) {
  if (value === null || value === undefined || value === '') return '—'
  const n = Number(value)
  return Number.isFinite(n) ? String(parseFloat(n.toFixed(2))) : String(value)
}

function flash(message: string, isError = false) {
  notice.value = message
  noticeError.value = isError
  if (!isError) {
    window.setTimeout(() => {
      if (notice.value === message) notice.value = ''
    }, 5000)
  }
}

const catalogColumns = ['日志编号', '钻孔编号', '班次', '班次序号', '深度区间', '岩层描述', '上报基准', '钻探人员', '状态', '审批结论']
const ledgerColumns = ['钻孔编号', '日志编号', '班次', '班次序号', '孔深区间', '生效深度止', '上报基准', '岩层描述', '审批结论', '审核人', '审核时间']
const todoColumns = ['钻孔编号', '日志编号', '班次', '班次序号', '深度区间', '上报基准', '状态', '待办事项', '提示']

const tabs = computed(() => [
  { key: 'catalog', label: '班次编录' },
  { key: 'ledger', label: '钻探台账' },
  { key: 'curve', label: '孔深曲线' },
  { key: 'todo', label: '班次待办', badge: todos.value.length },
  { key: 'journal', label: '操作序号' },
])

const stats = computed(() => [
  { label: '待填写', value: Number(summary.value['待填写'] ?? 0), warn: false },
  { label: '已填写待审核', value: Number(summary.value['已填写待审核'] ?? 0), warn: false },
  { label: '已审核', value: Number(summary.value['已审核'] ?? 0), warn: false },
  { label: '未完成待办', value: Number(summary.value['待办总数'] ?? 0), warn: false },
  {
    label: '三处同步',
    value: summary.value['同步一致'] ? '一致' : '不一致',
    warn: !summary.value['同步一致'],
  },
])

// --------------------------------------------------------------- 弹窗表单

const blankBackfill = () => ({
  钻孔编号: filters.borehole || '',
  深度起: '',
  深度止: '',
  班次: '白班 08:00-20:00',
  钻探人员: '',
  岩层描述: '',
  水位深度: '',
  补充说明: '',
})
const backfillForm = reactive<Record<string, string>>(blankBackfill())
const backfillFields = [
  { key: '钻孔编号', label: '钻孔编号', required: true, placeholder: '如 ZK-101' },
  { key: '深度起', label: '深度起（m）', required: true, type: 'number', placeholder: '区间起点，含' },
  { key: '深度止', label: '深度止（m）', required: true, type: 'number', placeholder: '区间终点，不含；须大于深度起' },
  { key: '班次', label: '班次', required: true, type: 'select', options: ['白班 08:00-20:00', '夜班 20:00-08:00'] },
  { key: '钻探人员', label: '钻探人员', placeholder: '本班负责人' },
  { key: '岩层描述', label: '岩层描述（可后补）', placeholder: '补录时允许留空，填写日志时补齐' },
  { key: '水位深度', label: '水位深度（m）', type: 'number' },
  { key: '补充说明', label: '补充说明', placeholder: '填写后该班次按「补充上报」基准留存' },
]

const fillForm = reactive({ 岩层描述: '', 水位深度: '', 钻探人员: '' })
const reviewForm = reactive({ 审批结论: '', 原因: '' })
const blankFinal = () => ({ 钻孔编号: filters.borehole || '', 现场终孔确认深度: '', 补充上报深度: '' })
const finalForm = reactive(blankFinal())

function openBackfill() {
  Object.assign(backfillForm, blankBackfill())
  dialog.value = 'backfill'
}
function openFill(row: Row) {
  activeRow.value = row
  fillForm.岩层描述 = String(row.岩层描述 ?? '')
  fillForm.水位深度 = row.水位深度 === null || row.水位深度 === undefined ? '' : String(row.水位深度)
  fillForm.钻探人员 = String(row.钻探人员 ?? '')
  dialog.value = 'fill'
}
function openReview(row: Row, jumping: boolean) {
  activeRow.value = row
  reviewJumping.value = jumping
  reviewForm.审批结论 = ''
  reviewForm.原因 = ''
  dialog.value = 'review'
}
function openFinalDepth() {
  Object.assign(finalForm, blankFinal())
  dialog.value = 'final'
}

// --------------------------------------------------------------- 数据读取

async function getJson(path: string): Promise<any> {
  const response = await request(path)
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? `接口返回 ${response.status}`)
  }
  return response.json()
}

async function reloadCatalog() {
  const query = new URLSearchParams()
  if (filters.borehole) query.set('borehole', filters.borehole)
  if (filters.keyword) query.set('keyword', filters.keyword)
  if (filters.status) query.set('status', filters.status)
  const payload = await getJson(`${ENDPOINT}?${query.toString()}`)
  catalog.value = payload.items ?? []
  catalogTotal.value = payload.total ?? 0
}

function resetFilters() {
  filters.borehole = ''
  filters.keyword = ''
  filters.status = ''
  void reloadCatalog()
}

async function reloadAll() {
  notice.value = ''
  try {
    const [listPayload, ledgerPayload, curvePayload, todoPayload, journalPayload, summaryPayload] = await Promise.all([
      getJson(`${ENDPOINT}?page=1&size=200`),
      getJson(`${ENDPOINT}/ledger`),
      getJson(`${ENDPOINT}/curve`),
      getJson(`${ENDPOINT}/todos`),
      getJson(`${ENDPOINT}/journal`),
      getJson(`${ENDPOINT}/summary`),
    ])
    catalog.value = listPayload.items ?? []
    catalogTotal.value = listPayload.total ?? 0
    ledger.value = ledgerPayload.items ?? []
    curve.value = curvePayload.items ?? []
    todos.value = todoPayload.items ?? []
    journal.value = journalPayload.items ?? []
    latestSeq.value = journalPayload.latest_seq ?? {}
    summary.value = summaryPayload
  } catch (error) {
    flash(error instanceof Error ? error.message : '班次数据读取失败', true)
  }
}

/** 带操作序号与幂等键的统一写入：序号过期或网络中断都保留现场，支持续做。 */
async function writeWithSeq(
  url: string,
  values: Record<string, unknown>,
  borehole: string,
  extra: Record<string, unknown> = {},
) {
  const operationId = newOperationId()
  const body = {
    values,
    operator: operator.value,
    operation_id: operationId,
    expected_seq: latestSeq.value[borehole] ?? 0,
    ...extra,
  }
  try {
    const response = await request(url, { method: 'POST', body: JSON.stringify(body) })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) {
      throw new Error(payload.detail ?? `接口返回 ${response.status}`)
    }
    if (payload.seq !== undefined) latestSeq.value[borehole] = payload.seq
    flash(payload.replayed ? payload.message : `${payload.message}（${borehole} 序号 ${payload.seq}）`)
    return payload
  } catch (error) {
    pendingOps.value.push({ url, body, operationId, borehole })
    savePending()
    const base = error instanceof Error ? error.message : '请求未送达'
    flash(`${base}；操作已暂存（${pendingOps.value.length} 条），联网后可在「操作序号」页按序号重发`, true)
    return null
  } finally {
    await reloadAll()
  }
}

async function replayPending() {
  if (!pendingOps.value.length) {
    flash('没有待重发的操作')
    return
  }
  const queue = [...pendingOps.value]
  for (const op of queue) {
    op.body = { ...op.body, expected_seq: latestSeq.value[op.borehole] ?? 0 }
    try {
      const response = await request(op.url, { method: 'POST', body: JSON.stringify(op.body) })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(payload.detail ?? `接口返回 ${response.status}`)
      if (payload.seq !== undefined) latestSeq.value[op.borehole] = payload.seq
      pendingOps.value = pendingOps.value.filter((item) => item.operationId !== op.operationId)
      flash(payload.replayed ? `序号 ${payload.seq} 的操作按原结果返回` : `已按序号 ${payload.seq} 续做成功`)
    } catch (error) {
      flash(error instanceof Error ? error.message : '重发失败，请稍后再试', true)
      break
    }
  }
  savePending()
  await reloadAll()
}

async function submitBackfill() {
  const values: Record<string, unknown> = { ...backfillForm }
  if (!values.钻孔编号 || values.深度起 === '' || values.深度止 === '') {
    flash('钻孔编号、深度起、深度止为必填', true)
    return
  }
  const payload = await writeWithSeq(`${ENDPOINT}/backfill`, values, String(values.钻孔编号))
  if (payload) {
    dialog.value = ''
    filters.borehole = String(values.钻孔编号)
  }
}

async function submitFill() {
  if (!activeRow.value) return
  const borehole = String(activeRow.value.钻孔编号)
  const payload = await writeWithSeq(
    `${ENDPOINT}/${activeRow.value.id}/actions`,
    { action: '填写日志', ...fillForm },
    borehole,
  )
  if (payload) dialog.value = ''
}

async function submitReview() {
  if (!activeRow.value) return
  if (!reviewForm.审批结论.trim()) {
    flash('审批结论必填', true)
    return
  }
  if (reviewJumping.value && !reviewForm.原因.trim()) {
    flash('跳步提交必须说明原因', true)
    return
  }
  const borehole = String(activeRow.value.钻孔编号)
  const payload = await writeWithSeq(
    `${ENDPOINT}/${activeRow.value.id}/actions`,
    { action: '提交审核', ...reviewForm },
    borehole,
  )
  if (payload) dialog.value = ''
}

async function submitFinalDepth() {
  if (!finalForm.钻孔编号) {
    flash('钻孔编号必填', true)
    return
  }
  if (finalForm.现场终孔确认深度 === '' && finalForm.补充上报深度 === '') {
    flash('现场终孔确认深度、补充上报深度至少填一个', true)
    return
  }
  const values: Record<string, unknown> = { 钻孔编号: finalForm.钻孔编号 }
  if (finalForm.现场终孔确认深度 !== '') values.现场终孔确认深度 = Number(finalForm.现场终孔确认深度)
  if (finalForm.补充上报深度 !== '') values.补充上报深度 = Number(finalForm.补充上报深度)
  const payload = await writeWithSeq(`${ENDPOINT}/final_depth`, values, finalForm.钻孔编号)
  if (payload) {
    dialog.value = ''
    filters.borehole = finalForm.钻孔编号
  }
}

// --------------------------------------------------------------- 曲线坐标

const chartWidth = 460
const chartHeight = 180
const pad = 34

const curveGroups = computed(() => {
  const groups = new Map<string, Row[]>()
  for (const row of curve.value) {
    const key = String(row.钻孔编号)
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(row)
  }
  return [...groups.entries()].map(([borehole, rows]) => {
    const sorted = [...rows].sort((a, b) => Number(a.序号) - Number(b.序号))
    const maxDepth = Math.max(1, ...sorted.map((r) => Number(r.生效孔深)))
    const innerW = chartWidth - pad * 1.6
    const innerH = chartHeight - pad * 1.4
    const stepX = sorted.length > 1 ? innerW / (sorted.length - 1) : 0
    const pointList = sorted.map((row, index) => {
      const depth = Number(row.生效孔深)
      const x = sorted.length > 1 ? pad + stepX * index : pad + innerW / 2
      const y = chartHeight - pad - (depth / maxDepth) * innerH
      return {
        id: row.id,
        x,
        y,
        depth,
        over: Boolean(row.终孔超深),
        label: `${borehole} 第${row.班次序号}班 生效孔深 ${depth}m`,
      }
    })
    return {
      borehole,
      points: pointList.map((p) => `${p.x},${p.y}`).join(' '),
      pointList,
    }
  })
})

loadPending()
onMounted(reloadAll)
</script>

<style scoped>
.page-actions { display: flex; gap: 8px; align-items: flex-end; }
.operator-box { font-size: 12px; color: var(--muted); display: flex; flex-direction: column; gap: 4px; }
.operator-box select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.warn { color: #b42318; }

.tabs { display: flex; gap: 4px; margin: 8px 0 12px; border-bottom: 1px solid var(--border); }
.tab { border: none; background: none; padding: 8px 14px; cursor: pointer; font-size: 13px; color: var(--muted); border-bottom: 2px solid transparent; }
.tab.active { color: var(--brand); border-bottom-color: var(--brand); font-weight: 600; }
.tab-badge { font-style: normal; background: var(--brand); color: #fff; border-radius: 10px; font-size: 11px; padding: 0 7px; margin-left: 4px; }

.notice { background: #eef6ff; border: 1px solid #b9dcff; color: #175199; border-radius: 6px; padding: 8px 12px; font-size: 13px; margin: 0 0 10px; }
.notice-error { background: #fef3f2; border-color: #fecdca; color: #b42318; }

.locked { background: #fafafa; }
.muted { color: var(--muted); }
.muted-link { color: var(--muted); font-size: 12px; }
.small { font-size: 12px; }
.status-tag { border-radius: 4px; padding: 1px 8px; font-size: 12px; background: #eef2f6; }
.status-tag[data-status='已审核'] { background: #e7f6ec; color: #1a7f37; }
.status-tag[data-status='已填写'] { background: #fff4e0; color: #9a6700; }
.over-tag { margin-left: 6px; background: #fef3f2; color: #b42318; border-radius: 4px; padding: 1px 6px; font-size: 12px; }

.modal-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.35); display: flex; align-items: center; justify-content: center; z-index: 20; }
.modal { background: #fff; border-radius: 10px; padding: 20px 22px; width: 460px; max-width: calc(100vw - 32px); box-shadow: 0 12px 32px rgba(15, 23, 42, 0.2); }
.modal h3 { margin: 0 0 6px; font-size: 16px; }
.form-item { display: block; margin: 10px 0; font-size: 13px; }
.form-item span { display: block; color: var(--muted); margin-bottom: 4px; }
.form-item i { color: #b42318; font-style: normal; margin-left: 2px; }
.form-item input, .form-item textarea, .form-item select { width: 100%; box-sizing: border-box; border: 1px solid var(--border); border-radius: 6px; padding: 7px 9px; font-size: 13px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }

.curve-table { margin-bottom: 14px; }
.curve-chart { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; margin-bottom: 12px; }
.curve-chart h4 { margin: 4px 0; font-size: 13px; }
.axis { stroke: var(--border); stroke-width: 1; }
.chart-label { font-size: 10px; fill: var(--muted); }
svg circle { fill: var(--brand); }
svg circle.over { fill: #b42318; }
</style>
