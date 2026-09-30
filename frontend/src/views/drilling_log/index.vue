<template>
  <section class="page" data-module="drilling_log">
    <header class="page-head">
      <div>
        <h2>班次编录 · 钻探日志</h2>
        <p class="page-desc">
          按孔深区间单向推进：待填写 → 已填写 → 已审核；跨班区间不重叠，跳步须说明原因；
          审批结论同源落到钻探台账、孔深曲线与班次待办。
        </p>
      </div>
      <div class="page-actions">
        <label class="operator-switch">
          当前账号
          <select v-model="operator">
            <option v-for="acc in ACCOUNTS" :key="acc" :value="acc">{{ acc }}</option>
          </select>
        </label>
        <button class="btn primary" type="button" @click="openCreate">班前排程登记</button>
        <button class="btn" type="button" @click="confirmFinal">现场终孔确认</button>
        <button class="btn ghost" type="button" @click="resumeSync">按序号续传</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}<em v-if="tab.badge" class="tab-badge">{{ tab.badge }}</em>
      </button>
    </nav>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>钻孔编号</span>
        <select v-model="filters.borehole">
          <option value="">全部钻孔</option>
          <option v-for="bh in boreholes" :key="bh" :value="bh">{{ bh }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>日志编号</span>
        <input v-model="filters.keyword" placeholder="按日志编号检索" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <!-- 班次编录 -->
    <table v-if="activeTab === 'shifts'" class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">
            <span :class="{ 'muted-cell': (row[column] ?? '') === '' }">{{ row[column] || '待补录' }}</span>
          </td>
          <td class="row-actions">
            <button
              v-if="row.status === '待填写'"
              class="link"
              type="button"
              @click="openFill(row)"
            >补录编录</button>
            <button
              v-if="row.status === '已填写'"
              class="link"
              type="button"
              @click="openAudit(row)"
            >审批</button>
            <button
              v-if="row.status !== '已审核'"
              class="link danger"
              type="button"
              @click="openJump(row)"
            >跳步提交</button>
            <span v-if="row.status === '已审核'" class="done-text">已审结</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">当前筛选下没有班次记录</td>
        </tr>
      </tbody>
    </table>

    <!-- 班次待办：实时派生，审核完成立即出列 -->
    <table v-else-if="activeTab === 'pending'" class="data-table">
      <thead>
        <tr>
          <th>日志编号</th><th>钻孔编号</th><th>班次</th><th>孔深区间(m)</th>
          <th>钻探人员</th><th>状态</th><th>待办动作</th><th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in todos" :key="item.日志编号">
          <td>{{ item.日志编号 }}</td>
          <td>{{ item.钻孔编号 }}</td>
          <td>{{ item.班次日期 }} {{ item.班次 }}</td>
          <td>[{{ item.孔深起 || '?' }}, {{ item.孔深止 || '?' }})</td>
          <td>{{ item.钻探人员 || '—' }}</td>
          <td>{{ item.状态 }}</td>
          <td>{{ item.待办动作 }}</td>
          <td>
            <button
              class="link"
              type="button"
              @click="activeTab = 'shifts'; reload()"
            >去处理</button>
          </td>
        </tr>
        <tr v-if="!todos.length">
          <td colspan="8" class="empty-state">没有待办——已审核班次不会残留在待办里</td>
        </tr>
      </tbody>
    </table>

    <!-- 钻探台账 -->
    <table v-else-if="activeTab === 'ledger'" class="data-table">
      <thead>
        <tr>
          <th>钻孔编号</th><th>班次总数</th><th>待填写</th><th>已填写</th><th>已审核</th>
          <th>补充上报累计孔深(m)</th><th>现场终孔确认值(m)</th><th>台账采用孔深(m)</th><th>深度基准</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in ledger" :key="item.钻孔编号">
          <td>{{ item.钻孔编号 }}</td>
          <td>{{ item.班次总数 }}</td>
          <td>{{ item.待填写班次数 }}</td>
          <td>{{ item.已填写班次数 }}</td>
          <td>{{ item.已审核班次数 }}</td>
          <td>{{ item.补充上报累计孔深 ?? '—' }}</td>
          <td>{{ item.现场终孔确认值 ?? '—' }}</td>
          <td><strong>{{ item.台账采用孔深 ?? '—' }}</strong></td>
          <td>
            <span :class="item.深度基准 === '现场终孔确认' ? 'tag ok' : 'tag'">{{ item.深度基准 }}</span>
          </td>
        </tr>
      </tbody>
    </table>

    <!-- 孔深曲线 -->
    <div v-else-if="activeTab === 'curve'" class="curve-panel">
      <p class="page-desc">
        曲线按班次推进顺序绘制累计孔深；现场终孔确认值优先，末点收敛到确认深度（
        <span class="tag ok">现场终孔确认</span>），补充上报点保留原基准（<span class="tag">原上报</span>）。
      </p>
      <svg class="depth-svg" :viewBox="`0 0 760 ${svgHeight}`" role="img">
        <line x1="80" :y1="axisY" x2="720" :y2="axisY" class="axis" />
        <line x1="80" y1="20" x2="80" :y2="axisY" class="axis" />
        <text x="40" :y="axisY + 4" class="svg-text">孔深(m)</text>
        <polyline
          v-if="curvePoints.length > 1"
          :points="curvePolyline"
          class="curve-line"
          fill="none"
        />
        <template v-for="(p, idx) in curvePoints" :key="`${p.日志编号}-${idx}`">
          <circle
            :cx="pointX(idx)"
            :cy="pointY(p.累计孔深)"
            r="5"
            :class="p.基准 === '现场终孔确认' ? 'curve-point final' : 'curve-point'"
          />
          <text :x="pointX(idx) - 24" :y="pointY(p.累计孔深) - 10" class="svg-text">
            {{ p.累计孔深 }}
          </text>
          <text :x="pointX(idx) - 28" :y="axisY + 18" class="svg-text small">
            {{ p.班次 }}{{ p.日志编号 === 'FINAL' ? '终孔' : '' }}
          </text>
        </template>
        <text v-if="!curvePoints.length" x="300" y="120" class="svg-text">请选择有班次数据的钻孔</text>
      </svg>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条班次记录 · 已同步操作序号 {{ lastSeq }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 班前排程 / 补录 / 审批 / 跳步 共用弹窗 -->
    <div v-if="dialog.open" class="modal-mask" @click.self="closeDialog">
      <div class="modal">
        <h3>{{ dialog.title }}</h3>
        <p v-if="dialog.hint" class="page-desc">{{ dialog.hint }}</p>
        <div class="form-grid">
          <template v-for="field in dialog.fields" :key="field.key">
            <label v-if="field.type !== 'textarea'" class="form-item">
              <span>{{ field.label }}<i v-if="field.required">*</i></span>
              <input
                v-if="field.type !== 'select'"
                v-model="form[field.key]"
                :placeholder="field.placeholder || ''"
              />
              <select v-else v-model="form[field.key]">
                <option v-for="opt in field.options" :key="opt" :value="opt">{{ opt }}</option>
              </select>
            </label>
            <label v-else class="form-item wide">
              <span>{{ field.label }}<i v-if="field.required">*</i></span>
              <textarea v-model="form[field.key]" rows="3" :placeholder="field.placeholder || ''"></textarea>
            </label>
          </template>
        </div>
        <div class="modal-actions">
          <button class="btn" type="button" @click="closeDialog">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitDialog">
            {{ submitting ? '提交中…' : dialog.confirmText }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/drilling_log'
const ACCOUNTS = ['account-A', 'account-B', '王审核', '现场终孔组']
const statuses = ['待填写', '已填写', '已审核']
const columns = [
  '日志编号', '钻孔编号', '班次', '班次日期', '孔深起', '孔深止',
  '钻进深度', '回次进尺', '岩层描述', '水位深度', '钻探人员', '日志状态',
]

const operator = ref('account-A')
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const submitting = ref(false)
const filters = ref<Record<string, string>>({ borehole: '', status: '', keyword: '' })
const boreholes = ref<string[]>([])
const todos = ref<Row[]>([])
const ledger = ref<Row[]>([])
const curvePoints = ref<Row[]>([])
const SEQ_STORAGE_KEY = 'drilling-log.last-seq'
const lastSeq = ref(Number(localStorage.getItem(SEQ_STORAGE_KEY) ?? 0) || 0)

const activeTab = ref<'shifts' | 'pending' | 'ledger' | 'curve'>('shifts')
const tabs = computed(() => [
  { key: 'shifts' as const, label: '班次编录', badge: 0 },
  { key: 'pending' as const, label: '班次待办', badge: todos.value.length },
  { key: 'ledger' as const, label: '钻探台账', badge: 0 },
  { key: 'curve' as const, label: '孔深曲线', badge: 0 },
])

const stats = computed(() => [
  { label: '待填写班次', value: rows.value.filter((r) => r.status === '待填写').length },
  { label: '已填写待审', value: rows.value.filter((r) => r.status === '已填写').length },
  { label: '已审核班次', value: rows.value.filter((r) => r.status === '已审核').length },
  { label: '未完成待办', value: todos.value.length },
])

// 弹窗描述
type FieldSpec = {
  key: string
  label: string
  required?: boolean
  type?: 'text' | 'number' | 'select' | 'textarea' | 'date'
  options?: string[]
  placeholder?: string
}
const dialog = reactive<{
  open: boolean
  mode: '' | 'create' | 'fill' | 'audit' | 'jump' | 'final'
  title: string
  hint: string
  confirmText: string
  targetId: number | null
  fields: FieldSpec[]
}>({ open: false, mode: '', title: '', hint: '', confirmText: '', targetId: null, fields: [] })
const form = reactive<Record<string, string>>({})

const SHIFT_FIELDS_BASE: FieldSpec[] = [
  { key: '孔深起', label: '孔深起(m)', required: true, type: 'number' },
  { key: '孔深止', label: '孔深止(m)', required: true, type: 'number' },
  { key: '回次进尺', label: '回次进尺(m)', type: 'number', placeholder: '不填按止深-起深' },
  { key: '水位深度', label: '水位深度(m)', type: 'number' },
  { key: '钻探人员', label: '钻探人员', type: 'text' },
  { key: '岩层描述', label: '岩层描述', required: true, type: 'textarea' },
]

function openDialog(mode: typeof dialog.mode, title: string, hint: string, confirmText: string,
  fields: FieldSpec[], init: Record<string, string>, targetId: number | null = null) {
  errorMessage.value = ''
  dialog.open = true
  dialog.mode = mode
  dialog.title = title
  dialog.hint = hint
  dialog.confirmText = confirmText
  dialog.fields = fields
  dialog.targetId = targetId
  Object.keys(form).forEach((k) => delete form[k])
  Object.assign(form, init)
}

function closeDialog() {
  dialog.open = false
  dialog.mode = ''
  dialog.targetId = null
}

function openCreate() {
  openDialog(
    'create',
    '班前排程登记（待填写占位）',
    '占位区间立即纳入同孔占用校验，两个账号登记重叠区间只允许一个成功。',
    '登记占位',
    [
      { key: '日志编号', label: '日志编号', required: true },
      { key: '钻孔编号', label: '钻孔编号', required: true, placeholder: '如 ZK-01' },
      { key: '班次日期', label: '班次日期', required: true, type: 'date' },
      { key: '班次', label: '班次', required: true, type: 'select', options: ['白班', '夜班'] },
      { key: '孔深起', label: '孔深起(m)', required: true, type: 'number' },
      { key: '孔深止', label: '孔深止(m)', required: true, type: 'number' },
      { key: '钻探人员', label: '值班钻探人员' },
    ],
    { 班次: '白班', 班次日期: new Date().toISOString().slice(0, 10), 钻孔编号: filters.value.borehole || '' },
  )
}

function openFill(row: Row) {
  openDialog(
    'fill',
    `补录编录 · ${row.日志编号}`,
    '只能补录「待填写」班次；提交后推进为已填写。已填写/已审核班次按原上报基准留存，不能覆盖。',
    '提交补录',
    [
      ...SHIFT_FIELDS_BASE,
      { key: '补录原因', label: '补录原因（事后补录时填写）', type: 'textarea' },
    ],
    {
      孔深起: String(row.孔深起 ?? ''), 孔深止: String(row.孔深止 ?? ''),
      钻探人员: String(row.钻探人员 ?? operator.value), operator: operator.value,
    },
    Number(row.id),
  )
}

function openAudit(row: Row) {
  openDialog(
    'audit',
    `审批 · ${row.日志编号}`,
    '审批结论将同源同步到钻探台账、孔深曲线与班次待办，审核完成后该班次立即离开待办。',
    '审核通过',
    [{ key: '审批结论', label: '审批结论', type: 'textarea', placeholder: '不填默认「编录与进尺相符，审核通过」' }],
    {},
    Number(row.id),
  )
}

function openJump(row: Row) {
  openDialog(
    'jump',
    `跳步提交 · ${row.日志编号}`,
    '跳过「已填写」中间档必须说明原因；编录内容仍要齐全，提交后直接到已审核。',
    '跳步提交审核',
    [
      ...SHIFT_FIELDS_BASE.map((f) =>
        f.key === '岩层描述' ? { ...f, required: true, placeholder: `原值：${row.岩层描述 || '空'}` } : f),
      { key: '跳步原因', label: '跳步原因', required: true, type: 'textarea', placeholder: '为什么可以跳过中间档' },
      { key: '审批结论', label: '审批结论', type: 'textarea' },
    ],
    {
      孔深起: String(row.孔深起 ?? ''), 孔深止: String(row.孔深止 ?? ''),
      岩层描述: String(row.岩层描述 ?? ''), 回次进尺: String(row.回次进尺 ?? ''),
      水位深度: String(row.水位深度 ?? ''), 钻探人员: String(row.钻探人员 ?? operator.value),
    },
    Number(row.id),
  )
}

function confirmFinal() {
  openDialog(
    'final',
    '现场终孔确认',
    '现场终孔确认值优先于补充上报：台账采用孔深与曲线末点以确认值为准；既有班次保留原上报基准。',
    '确认终孔',
    [
      { key: '钻孔编号', label: '钻孔编号', required: true },
      { key: '终孔深度', label: '终孔深度(m)', required: true, type: 'number' },
      { key: '备注', label: '备注', type: 'textarea' },
    ],
    { 钻孔编号: filters.value.borehole || (boreholes.value[0] ?? '') },
  )
}

async function readError(response: Response): Promise<string> {
  try {
    const data = await response.json()
    return data.detail || data.message || `操作未生效（HTTP ${response.status}）`
  } catch {
    return `操作未生效（HTTP ${response.status}）`
  }
}

// 每次写入带幂等键：掉线重发同一操作不会重复生效
function idemKey() {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

async function postAction(path: string, values: Record<string, unknown>) {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify({ values: { ...values, operator: operator.value }, idem_key: idemKey() }),
  })
  if (!response.ok) {
    const message = await readError(response)
    throw Object.assign(new Error(message), { status: response.status })
  }
  return response.json()
}

async function submitDialog() {
  errorMessage.value = ''
  const missing = dialog.fields
    .filter((f) => f.required && !String(form[f.key] ?? '').trim())
    .map((f) => f.label.replace('*', ''))
  if (missing.length) {
    errorMessage.value = `请先填写：${missing.join('、')}`
    return
  }
  submitting.value = true
  try {
    const payload: Record<string, unknown> = {}
    dialog.fields.forEach((f) => {
      if (String(form[f.key] ?? '').trim() !== '') payload[f.key] = form[f.key]
    })
    let path = ENDPOINT
    if (dialog.mode === 'create') path = `${ENDPOINT}/shifts`
    if (dialog.mode === 'fill') path = `${ENDPOINT}/${dialog.targetId}/fill`
    if (dialog.mode === 'audit') path = `${ENDPOINT}/${dialog.targetId}/audit`
    if (dialog.mode === 'jump') path = `${ENDPOINT}/${dialog.targetId}/jump-submit`
    if (dialog.mode === 'final') path = `${ENDPOINT}/final-depth`

    const result = await postAction(path, payload)
    if (result.last_seq) {
      lastSeq.value = result.last_seq
      localStorage.setItem(SEQ_STORAGE_KEY, String(lastSeq.value))
    }
    closeDialog()
    await refreshAll()
  } catch (error) {
    const e = error as Error & { status?: number }
    errorMessage.value = e.status === 409
      ? `区间冲突（另一账号已先占用）：${e.message}。列表已刷新，请按最新区间续做`
      : e.message
    if (e.status === 409) {
      closeDialog()
      await refreshAll()
    }
  } finally {
    submitting.value = false
  }
}

function resetFilters() {
  filters.value = { borehole: '', status: '', keyword: '' }
  void reload()
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(
    Object.entries(filters.value).filter(([, v]) => v) as [string, string][],
  ).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) throw new Error('班次编录列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    boreholes.value = [...new Set(rows.value.map((r: Row) => String(r.钻孔编号)).filter(Boolean))].sort()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '班次编录列表读取失败'
  }
}

async function loadSideData() {
  const bh = filters.value.borehole || undefined
  const [pendingRes, ledgerRes, curveRes] = await Promise.all([
    request(`${ENDPOINT}/pending${bh ? `?borehole=${encodeURIComponent(bh)}` : ''}`),
    request(`${ENDPOINT}/ledger${bh ? `?borehole=${encodeURIComponent(bh)}` : ''}`),
    bh
      ? request(`${ENDPOINT}/depth-curve?borehole=${encodeURIComponent(bh)}`)
      : Promise.resolve(null),
  ])
  if (pendingRes.ok) todos.value = (await pendingRes.json()).items ?? []
  if (ledgerRes.ok) ledger.value = (await ledgerRes.json()).items ?? []
  if (curveRes && curveRes.ok) curvePoints.value = (await curveRes.json()).points ?? []
  if (!bh) curvePoints.value = []
}

async function refreshAll() {
  await reload()
  await loadSideData()
}

function switchTab(key: typeof activeTab.value) {
  activeTab.value = key
  void loadSideData()
}

// 掉线续传：按本地记住的操作序号拉增量，再用回执里的同源快照刷新台账/待办
async function resumeSync() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/sync?since=${lastSeq.value}`)
    if (!response.ok) throw new Error('续传失败')
    const data = await response.json()
    lastSeq.value = data.last_seq
    localStorage.setItem(SEQ_STORAGE_KEY, String(data.last_seq))
    if (data.ledger) ledger.value = data.ledger.items ?? []
    if (data.pending) todos.value = data.pending.items ?? []
    await reload()
    const count = data.operations?.length ?? 0
    errorMessage.value = ''
    window.alert(`已按序号 ${data.since} 续传，接收 ${count} 条操作，当前序号 ${data.last_seq}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '续传失败'
  }
}

// 孔深曲线坐标：深度向下增大
const svgHeight = computed(() => 260)
const axisY = computed(() => svgHeight.value - 40)
const curveMaxDepth = computed(() =>
  Math.max(10, ...curvePoints.value.map((p) => Number(p.累计孔深) || 0)),
)
const chartHeight = computed(() => axisY.value - 30)
function pointX(idx: number) {
  const n = Math.max(curvePoints.value.length - 1, 1)
  return 110 + idx * (600 / n)
}
function pointY(depth: number) {
  return axisY.value - (Number(depth) / curveMaxDepth.value) * chartHeight.value
}
const curvePolyline = computed(() =>
  curvePoints.value.map((p, idx) => `${pointX(idx)},${pointY(Number(p.累计孔深))}`).join(' '),
)

onMounted(refreshAll)
</script>

<style scoped>
.page-actions { display: flex; gap: 8px; align-items: flex-end; flex-wrap: wrap; }
.operator-switch { font-size: 12px; color: var(--muted); display: flex; flex-direction: column; gap: 2px; }
.operator-switch select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.tab-bar { display: flex; gap: 4px; margin-bottom: 10px; border-bottom: 1px solid var(--border); }
.tab { border: none; background: none; padding: 8px 14px; cursor: pointer; font-size: 13px; color: var(--muted); border-bottom: 2px solid transparent; }
.tab.active { color: var(--brand); border-bottom-color: var(--brand); font-weight: 600; }
.tab-badge { font-style: normal; background: #b42318; color: #fff; border-radius: 9px; font-size: 11px; padding: 0 6px; margin-left: 6px; }
.muted-cell { color: #b6bfca; }
.done-text { color: #067647; font-size: 12px; }
.danger { color: #b42318; }
.tag { display: inline-block; font-size: 11px; padding: 1px 8px; border-radius: 10px; background: #eef2f7; color: var(--muted); }
.tag.ok { background: #e7f6ec; color: #067647; }
.modal-mask { position: fixed; inset: 0; background: rgba(16, 24, 40, 0.45); display: flex; align-items: center; justify-content: center; z-index: 20; }
.modal { background: #fff; border-radius: 10px; padding: 20px 24px; width: 620px; max-width: 92vw; max-height: 88vh; overflow: auto; }
.modal h3 { margin: 0 0 6px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; margin-top: 12px; }
.form-item { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.form-item.wide { grid-column: 1 / -1; }
.form-item i { color: #b42318; font-style: normal; margin-left: 2px; }
.form-item input, .form-item select, .form-item textarea { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; color: #1f2937; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
.curve-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.depth-svg { width: 100%; height: 360px; }
.axis { stroke: #94a3b8; stroke-width: 1; }
.svg-text { font-size: 11px; fill: var(--muted); }
.svg-text.small { font-size: 10px; }
.curve-line { stroke: var(--brand); stroke-width: 2; }
.curve-point { fill: var(--brand); }
.curve-point.final { fill: #b54708; }
</style>
