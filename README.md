# 多源数据采集与RPA自动化平台

面向网页抓取、API接入、增量采集、RPA流程编排、数据校验与采集监控的数据工程平台。

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
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据源配置 | `datasource` | 数据源 | 源端编号、连接类型、接入地址 |
| 网页采集规则 | `crawler` | 采集规则 | 规则编号、目标网址、页面选择器 |
| API接入管理 | `apiclient` | API客户端 | 客户端编号、接口名称、请求方法 |
| 增量采集 | `incremental` | 增量游标 | 游标编号、来源标识、增量字段 |
| RPA任务编排 | `robot_task` | RPA任务 | 任务编号、任务名称、页面定位器 |
| 数据接入通道 | `ingestion` | 接入通道 | 通道编号、通道名称、输入格式 |
| 格式解析器 | `parser` | 解析配置 / 解析批次 | 解析编号、输入格式、输出格式 |
| 采集结果校验 | `validator` | 校验规则 / 批次核对 | 规则编号、校验字段、校验类型 |
| 采集去重 | `dedup` | 去重策略 | 策略编号、输入来源、去重键 |
| 采集调度 | `schedule` | 调度计划 | 计划编号、关联任务、调度周期 |
| 采集监控 | `monitor` | 监控面板 | 面板编号、采集源总数、今日采集量 |
| 采集日志 | `log_viewer` | 日志条目 | 日志编号、关联任务、日志级别 |
| 凭证管理 | `auth` | 认证凭证 | 凭证编号、凭证类型、关联数据源 |
| 采集转换管道 | `transform` | 转换节点 | 节点编号、输入来源、输出目标 |
| 数据流转编排 | `flow` | 流转管道 | 管道编号、管道名称、起始节点 |
| 数据血缘追踪 | `lineage` | 血缘关系 | 关系编号、上游节点、下游节点 |
| 采集报告 | `report` | 采集报告 | 报告编号、采集源、统计周期 |

## 多格式批量解析（格式解析器 × 采集结果校验）

格式解析器页支持一次勾选多条 CSV / JSON / XML 输入（`/api/parser/inputs`），
提交后逐条生成 **解析批次清单** 与 **标准化字段包**：

- **批次前置**：先为每条输入锁定唯一映射版本，再汇总整批字段类型表。
  - `最新确认`：整批统一用各格式最新确认的可用映射；
  - `按输入命中`：按每条输入登记的命中映射解析，保持与历史映射连续。
  - 命中的映射不可用、同名字段出现两种类型、输入重复或不存在时，整批不落库，
    错误逐条给到「哪条输入 / 哪个字段 / 哪份映射」。
- **批次产物**：字段顺序与字段类型表在批次内固定；每条输入保留来源文件、
  映射编号与版本、标准化字段结果、缺失字段/类型异常及失败原因。
- **重算**：`POST /api/parser/batches/{id}/reparse` 以批次冻结的旧映射快照重算，
  原摘要保留，当前摘要单独刷新；已有复核结论转为「复核失效」。
- **校验协作**（采集结果校验页）：校验服务把两份产物当只读输入，按来源文件汇总
  字段缺失与类型异常，回写批次清单/字段包，生成待复核清单与业务记录
  （`parser_record`）。同一批次并发复核只保留一份有效结论，重算后需带复核版本重新核对。
- **回查**：`GET /api/validator/batches/{id}/conclusion` 返回当前有效复核结论；
  业务记录可通过 `GET /api/parser/batches/{id}/records` 按批次读取。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
