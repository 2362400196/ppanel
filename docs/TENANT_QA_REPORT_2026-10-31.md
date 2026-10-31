# 租户容器全功能测试报告

- 测试时间：2026-10-31 01:35 ~ 02:05（GMT+8）
- 测试对象：主控 `http://64.90.1.52:8001`，节点「本机」上的租户容器
- 测试账号：`2362400196`（普通用户，余额 ¥1000）
- 被测实例：`面板-br53`（`fd257e7f-3429-4e6f-b5b2-4d408c0a3f97`，python:3.11-slim，1C/512M/2G，35034→8000）
- 测试方式：直连 HTTP/WS 接口 + 宿主机 `docker inspect` 交叉核对真实状态，共约 190 次断言
- 结论：**核心功能全部可用**（开通、启停、文件、备份、域名、独立面板、WS、工单、钱包、商城、代码部署均通过）；发现 **7 项缺陷**，其中 2 项影响用户体验需优先修

---

## 一、通过项（按模块）

| 模块 | 覆盖内容 | 结果 |
|---|---|---|
| 登录与鉴权 | 正确/错误密码、`/api/me`、无 token 401、伪造 token 401 | 通过 |
| 实例只读 | 列表、详情、实时 stats、历史 metrics（60s 采样 9 点）、日志、操作记录、端口 probe | 通过 |
| 电源操作 | stop→容器真实 `exited` 且端口拒连；start→`running` 且 HTTP 200；restart→`StartedAt` 变化 | 通过 |
| 文件管理 | 列目录、mkdir、上传、读、保存、重命名（`src/dst`）、下载、删除、越界拒绝（`../../etc/passwd` → 403） | 17 项全通过 |
| 备份恢复 | 备份为合法 tar.gz（含 main.py）、运行中恢复被 409 拦截、停机后恢复成功且内容还原 | 通过 |
| 域名绑定 | 绑定 → Host 头反代到实例（200）→ 非法格式 400 → 跨实例冲突 409 → 解绑后不再反代 | 通过 |
| 独立面板 `/panel` | 令牌签发/登录、实例信息、stats、logs、ops、文件读/写/删、重启、无凭证 401、错误令牌 401 | 31 项全通过 |
| WebSocket | 无 token 握手 403；日志流推送；依赖安装任务流 8 行 `exit=0`，`six 1.16.0` 真实装入容器 | 通过 |
| 实例生命周期 | 非法镜像 400、CPU/内存越界 422、镜像拉取开通 33.5s、端口自动分配、回收后容器+数据卷+目录全清、重复回收 404 | 通过 |
| 工单 | 提交、详情、回复、附件上传与下载（内容校验）、关闭、关闭后回复 400、不存在 404 | 通过 |
| 福利中心 | 概览、重复签到 400（幂等）、积分不足兑换 400、无效券 404 | 通过 |
| 钱包 | 余额、流水、未开通支付时充值 400 明确提示、金额越界 400 | 通过 |
| 商城 | 购买「轻量型」成功：扣款 100000→99500、返积分 10→60、流水 `kind=shop/paid`、实例生成、不存在商品 404 | 通过 |
| 代码部署 | 上传 Flask 应用 + requirements.txt → 重启自动 pip → **16 秒上线**，`/health` 返回 `{"app":"qa-flask"}` | 通过 |
| 越权隔离 | 8 个管理端接口（`/admin/*`、`/host/*`、`/docker/*`）普通用户一律 403；`?all=1` 仍只返回本人实例 | 通过 |
| 前端 | 门户/登录/实例/钱包页 200，静态资源加载正常 | 通过 |

---

## 二、缺陷清单（2026-10-31 已全部修复并复测）

> 修复落点见每节末的「修复」行；复测结果见文末「五、修复验证记录」。

### P1-1 开通成功但实例不自动启动（用户开完就是死的）

- 现象：新建实例返回 `status=created`，容器停在 `Created`，端口不可达；手动点「启动」后立刻正常。
- 证据：`docker inspect ppanel-4` → `status=created`；`GET /instances/{uuid}` → `exited`；手动 start 后端口 200。
- 根因：`agent/backend/src/services/provision.rs::create_instance` 只 `create_container`，没有 `start_instance`；前端 `CreateInstanceModal.vue` 创建成功后也不调用 `/start`。
- 影响：新客户开通后看到"已停止"，以为坏了；商城购买同样如此。
- 建议：开通流程末尾直接启动（状态置 `running`），或前端创建成功后串一次 `/start`。
- **修复**：`provision.rs` 建容器后立即调 `start_instance`；启动失败不回滚（容器与数据保留，状态按真实情况落库）。前端 `CreateInstanceModal.vue` 按返回状态给出"已开通并启动"或"启动失败请查日志"。实测创建 1.6s 返回 `running`，端口立即 200。

### P1-2 崩溃重启循环被显示为「运行中」

- 现象：容器内代码语法错误 → 进程秒退 → Docker 无限重启（`restarts=8`、`Status=restarting`），但主控上报 `running`，界面绿灯。
- 证据：`docker inspect` → `status=restarting restarting=true restarts=8 exit=1`；同一时刻 API 详情 `status=running`；`instance_events` 无崩溃记录，`alerts` 为空。
- 影响：服务实际不可用却显示正常，且稳定性评分与告警中心都抓不到 → 用户投诉前没人知道。
- 建议：`sync_status` 识别 `restarting`（或 RestartCount 持续增长）→ 状态置 `crash`，写 `instance_crashed` 事件并触发 `instance_crash` 告警。
- **修复**：被控 `sync_status` 把 `Restarting=true` / `Status=restarting` 判为独立状态 `restarting`（`container_stats` 同时返回 `running=false`）；主控 `status_sync` 记录 `crash` 事件；`alerts._collect` 新增 4c 规则——实例当前处于 `restarting` 直接 `critical` 告警，不必等崩溃次数攒够阈值。前端 `STATUS_TEXT`/`StatusDot` 增加"崩溃重启中"红色态，列表的停止按钮对 `restarting` 也可见（便于止损）。实测状态 `restarting`、告警 `critical` 触发并随修复自动 `resolved`。

### P2-3 重置面板令牌后旧会话仍然有效

- 现象：`POST /panel-token?reset=1` 后，旧令牌无法再登录（正确），但**已登录的旧会话仍能读写容器**（返回 200）。
- 根因：`load_panel_inst` 只校验 `panel_token` 非空，未校验会话与当前令牌的绑定版本。
- 建议：面板 JWT 内嵌令牌指纹（如 `sha256(panel_token)[:8]`），提取时比对；重置即令旧会话失效。
- **修复**：`Claims` 增加 `pt`（`sha256(panel_token)[..8]`），`create_panel_token(iid, panel_token)` 签发时写入；`load_panel_inst` 与面板 WebSocket 都比对指纹，不匹配返回 401「面板会话已失效，请重新登录」。实测重置后旧会话立刻 401，新令牌重新登录正常，旧令牌也无法再登录。

### P2-4 停止/重启固定耗时约 10 秒

- 实测：stop 10.4s、restart 10.6~11.0s。
- 根因：被控 `StopContainerOptions { t: 10 }`，容器 PID 1 不处理 SIGTERM 时要等满宽限期。
- 建议：改两阶段（t=3 → 超时 SIGKILL），或把 t 调到 3~5 秒。
- **修复**：`stop_instance`/`restart_instance` 宽限期 10s → 3s，并在 stop 前 inspect 短路"已经停了"的情况。实测 stop 3.40s（容器 PID 1 为 `sh`、不转发 SIGTERM 时会等满宽限期）、start 0.96s。

### P2-5 用户端「操作记录」漏记依赖安装等动作

- 现象：`POST /instances/{uuid}/deps/install` 成功后，`GET /instances/{uuid}/ops` 无该记录（只有主控自己写的 create/start/stop/panel_token 等）。
- 根因：master 的转发类接口未写 `op_logs`；被控只写自己的库。
- 建议：master 侧转发接口补 `_oplog`（与 create/delete 对齐），或 ops 聚合 `/panel/instance/ops`。
- **修复**：`install_deps` 成功后写 `op_logs(action="install_deps")`。实测操作记录出现 `install_deps`（最近动作 `install_deps → start → stop → create_instance`）。

### P3-6 未知 API 路径返回 200 HTML

- 现象：`GET /api/nonexistent`、`GET /api/instances/xxx/yyy` 均返回 `200 text/html`（SPA 兜底 index.html）。
- 影响：客户端与监控会把错误当成功；排查困难。
- 建议：`/api/*` 未命中时返回 404 JSON。
- **修复**：`main.py::catch_all` 在域名反代之后、SPA 兜底之前拦截 `api` / `api/*`，返回 `404 {"detail":"接口不存在：…"}`。实测 `/api/nonexistent`、`/api/instances/xxx/yyy` 均 404 JSON，前端路由 `/alerts` 仍返回 SPA。

### P3-7 任务日志接口未鉴权

- 现象：`GET /api/tasks/lines?task_id=<uuid>` 不带任何凭证返回 200，内容含实例名、节点名、分配端口、开通步骤。
- 影响：需先知道 task_id（uuid4）才能读，风险低，但属信息泄露面。
- 建议：加登录校验（本任务归属或管理员）。
- **修复**：`/api/tasks/lines` 加 `get_principal`；`tasks.start` 记录发起人 `owner`，非发起人（且非管理员）只拿到空结果。实测匿名 401、登录后可达。

### 附带：文件接口路径语义容易误解

- 面板/文件接口的「根」是实例工作目录，传 `/app/main.py` 会被拼成 `/app/app/main.py`，而响应体回显的 `path` 仍是传入值。
- 前端传的是根相对路径（`/main.py`），行为正确；但外部调用者（含本次测试第一轮）极易踩坑。
- 建议：对以实例根绝对路径开头的入参做规范化或明确报错，并在文档中写明"路径相对实例根"。

---

## 三、测试期间对环境做过的操作（已全部复原）

| 操作 | 复原情况 |
|---|---|
| 创建 4 个测试实例（qa-auto / qa-diag / qa-tasklog / 商城实例） | 全部回收，容器、数据卷、`/data/inst/*` 目录已清 |
| 停止/启动/重启被测实例多次 | 已恢复 `running`，`restarts=0`，35034 端口 200 |
| 上传测试文件、requirements.txt、Flask 应用 | 已删除；`main.py` 已还原为原始 535 字节示例 |
| 绑定测试域名 `qa-20261031.localhost` | 已解绑，`domains` 表清空 |
| 提交测试工单（含附件） | 已删除工单、消息、附件记录 |
| 商城购买扣款（¥5）与返积分 | 订单删除，余额 100000、积分 10、等级经验 0 已还原 |
| 重置面板令牌（2 次） | 面板令牌为重置后的新值，可在面板重新签发 |
| 清理测试用 op_logs（panel_token/bind_domain 等） | 已删 |

> 说明：实例的操作记录（ops）保留了 stop/start/restart 等真实业务动作，未做删改，便于你核对。

---

## 四、复测命令（可在任意机器重跑）

测试脚本位于 `C:\Users\23624\AppData\Local\Temp\pp_test\`：
`t1_read_power.py`（只读+电源）、`t2b_files.py`（文件+备份）、`t3_domain_panel.py`（域名+面板）、`t4_ws.py`（WS+依赖）、`t5_create.py`（开通回收）、`t6_business.py`（工单/福利/钱包/商城）、`t7_authz.py`（越权）、`t8_deploy.py`（代码部署）。

依赖：`pip install requests websocket-client`。

---

## 五、修复验证记录（2026-10-31）

环境：`64.90.1.52`，被控 `ppanel-agent 0.1.0 (x86_64-unknown-linux-musl, built 2026-10-30T18:51:18Z)`，主控容器已重启加载新代码。

| # | 验证项 | 实测结果 |
|---|---|---|
| T1 | 开通即启动 | 创建 1.6s 返回 `status=running`，未手动启动即端口 200 |
| T2 | 崩溃重启状态 | `status=restarting`（Docker `restarting`，restarts 持续增长），`stats.running=false` |
| T2 | 崩溃告警 | `alerts` 出现 `instance_crash / critical / 实例 X 处于崩溃重启循环`；修复后自动 `resolved` |
| T2 | 崩溃事件 | `instance_events` 落 `crash`（顺带修掉"手动 stop→start 后 crash 被误判人为停机"） |
| T3 | 面板会话吊销 | 重置令牌后旧 JWT 立刻 401；新令牌登录 200；旧令牌无法再登录 401 |
| T4 | 停止/启动耗时 | stop 3.40s（原 10.4s）、start 0.96s |
| T5 | 依赖安装留痕 | `ops` 出现 `install_deps` |
| T6 | 未知 API | `/api/nonexistent`、`/api/instances/xxx/yyy` → 404 JSON；`/alerts` 仍 200 SPA |
| T7 | 任务日志鉴权 | 匿名 401；登录后按 `owner` 过滤 |

合计 32 项断言全部通过。测试实例、告警、事件已清理，现场仅剩用户实例 `面板-br53`（`running`，35034 → 200）。
