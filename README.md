# 地质勘探数据管理平台

面向地质勘探的钻孔编录、岩心取样、物探数据、化探分析、测绘资料与储量估算的综合数据管理后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 钻孔编录 | `borehole` | 钻孔 | 钻孔编号、勘探区、孔口坐标 |
| 岩心管理 | `core` | 岩心样本 | 岩心编号、所属钻孔、取样深度起 |
| 地层划分 | `stratigraphy` | 地层单元 | 单元编号、钻孔编号、地层名称 |
| 地球物理 | `geophysics` | 物探测线 | 测线编号、勘探区、物探方法 |
| 化探分析 | `geochem` | 化探样品 | 样品编号、样品类型、采样点位 |
| 化验数据 | `assay` | 化验结果 | 化验编号、样品编号、元素名称 |
| 地质填图 | `mapping` | 填图单元 | 图幅编号、图幅名称、比例尺 |
| 测绘控制 | `survey_point` | 控制点 | 点号、点类型、坐标X |
| 钻探日志 | `drilling_log` | 班次编录 | 日志编号、钻孔编号、班次、孔深起/孔深止、岩层描述 |
| 储量估算 | `reserve` | 矿体块段 | 块段编号、矿体名称、面积 |
| 样品登记 | `sample_registry` | 送检样品 | 送检编号、样品名称、采样位置 |
| 勘探设备 | `equipment` | 勘探仪器 | 仪器编号、仪器名称、型号规格 |
| 水文地质 | `hydro` | 水文观测点 | 观测编号、观测类型、所在钻孔 |
| 剖面编录 | `section` | 实测剖面 | 剖面编号、剖面名称、剖面长度 |
| 地质报告 | `geological_report` | 勘探报告 | 报告编号、勘探区、报告类型 |
| 遥感解译 | `remote` | 遥感数据 | 数据编号、数据源、分辨率 |
| 矿产评价 | `mineral` | 矿化线索 | 线索编号、勘探区、矿种 |
| 环境地质 | `environmental` | 环境调查点 | 调查编号、调查区域、灾害类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 钻探日志：班次编录的写入约定

班次编录按孔深区间单向推进，所有写入只走 `DrillingLogService` 这一条路径；
钻探台账、孔深曲线、班次待办都是同一份班次数据的实时投影，不再各算一套。

- **状态单向推进**：一条班次日志只能 `待填写 → 已填写 → 已审核`，不能回退；
  补录只针对「待填写」班次，已填写/已审核班次按原上报基准留存，不能覆盖。
- **孔深区间**：班次表示为 `[孔深起, 孔深止)`（米）。同孔区间不允许重叠
  （首尾相接合法），且只能向更深方向推进；待填写占位只排程、不占用推进基准。
- **跳步提交**：正常逐档推进；确需跳过「已填写」直接送审，必须带 `跳步原因`。
- **审批同源**：审核结论随写入同步刷新台账、曲线、待办；审核完成班次立即离开待办。
- **终孔确认优先**：现场终孔确认值优先于补充上报（台账采用孔深、曲线末点以确认值
  为准）；历史班次仍保留原上报值与差异。
- **并发与续传**：同区间并发写入只允许一个成功，冲突返回 `409`；每次写入返回单调
  递增的操作序号 `last_seq`，掉线后用 `GET /api/drilling_log/sync?since=<序号>`
  续做；请求带 `idem_key` 时同一操作重放不重复生效。

写接口（请求体统一为 `{ "values": {...}, "idem_key"?: "..." }`）：

| 接口 | 含义 |
| --- | --- |
| `POST /api/drilling_log/shifts` | 班前排程，登记待填写占位 |
| `POST /api/drilling_log/{id}/fill` | 补录/填写编录（待填写 → 已填写，可带补录原因） |
| `POST /api/drilling_log/{id}/audit` | 审批（已填写 → 已审核，带审批结论） |
| `POST /api/drilling_log/{id}/jump-submit` | 跳步提交（必须带跳步原因） |
| `POST /api/drilling_log/final-depth` | 现场终孔确认（确认值优先） |

读侧投影：`GET /pending`（班次待办）、`/ledger`（钻探台账）、
`/depth-curve?borehole=`（孔深曲线）、`/sync?since=`（按序号续传）、
`/ops`（操作流水）、`/export/data`（含台账/待办快照的导出）。
