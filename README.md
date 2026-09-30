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
| 钻探日志（班次编录） | `drilling_log` | 班次日志 | 日志编号、钻孔编号、班次、深度起/深度止、上报基准、审批结论 |
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

### 班次编录写入路径（钻探日志）

班次日志只能经 `DrillingLogService` 这一条写入路径修改，派生数据全部由它同步，
前端与其他接口只读：

- 状态单向推进：`待填写 → 已填写 → 已审核`，没有回退；已审核锁定，
  跳步提交必须带「原因」，原因随操作序号流水留痕。
- 同钻孔班次按孔深区间 `[深度起, 深度止)` 单向推进，区间重叠返回 409，
  首尾相接允许。
- 审核通过即在同一把锁内 upsert 钻探台账（`/api/drilling_log/ledger`）、
  重算孔深曲线（`/api/drilling_log/curve`）、清除班次待办
  （`/api/drilling_log/todos`），`/summary` 可核对三处同步一致性。
- 终孔深度经 `/api/drilling_log/final_depth` 登记：现场终孔确认优先于补充上报，
  一旦有现场值，补充上报不能覆盖；台账/曲线的原始上报深度保留，
  「生效深度」按现场值封顶并标记越界班次。
- 每个钻孔维护独立操作序号（`/api/drilling_log/journal`）。写请求带
  `expected_seq` 做乐观并发：同区间并发补录只放行一个，冲突方拿 409 和最新序号；
  带 `operation_id` 的重发按原结果幂等返回（掉线续做不重复写入）。
- 规则验证脚本：`python3 backend/verify_shift_rules.py`（需要 fastapi/httpx）。
