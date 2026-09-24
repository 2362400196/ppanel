# PPanel 独立面板 · 商城对接文档（/open/*）

> 版本：v1.0（2026-09-23）
> 适用对象：任何商城 / 开通系统 / 自动化售卖平台，直连 PPanel 被控节点完成容器实例的**开通、查询、续期、回收、令牌管理**。
> 面板与商城完全解耦：商城只管商业化（订单、支付、到期），面板只管单容器操作，二者通过本组 API 与 Webhook 连接。

---

## 1. 架构与对接模型

```
第三方商城 / 开通系统（你的系统）
   │  X-API-Key（本组 /open/* 接口）
   ▼
PPanel 被控节点（agent，默认端口 9100）
   │  开通时下发 panel_url + panel_token
   ▼
独立面板 /panel（买家直接使用）
   概览 / 用量统计 / 文件管理 / 域名绑定 / 依赖 / 终端 / 日志 / 操作记录 / 备份
```

- **一个实例 = 一个容器 = 一个独立面板**。买家拿一键登录链接即可进入自己的面板，无需注册登录你的商城以外的任何账号体系。
- 被控节点可脱离 PPanel 主控独立运行，也可以与主控共存（主控走 `/agent/*` + `X-Node-Token`，与 `/open/*` 互不干扰）。
- `owner_ref`（对账单号）由商城在开通时传入，贯穿查询与 Webhook，用于订单 ↔ 实例关联。

---

## 2. 节点侧配置（一次性）

被控节点配置文件（默认 `/root/ppanel-agent/.env`，或环境变量）：

| 配置项 | 必填 | 说明 |
|---|---|---|
| `OPEN_API_KEY` | 是 | 开通接口密钥，**未配置则 /open/* 整组禁用（403）**。建议 `ppopen_` 前缀 + 16 位以上随机串 |
| `PANEL_PUBLIC_URL` | 建议 | 面板对外地址，如 `http://1.2.3.4:9100/panel` 或 `https://p.example.com/panel`；不配置则按请求 Host 自动拼接（经过反向代理且无该头时可能拼错，建议显式配置） |
| `WEBHOOK_URL` | 否 | 商城回调地址，配置后接收实例崩溃 / 到期通知（见第 7 节） |
| `WEBHOOK_WARN_DAYS` | 否 | 到期提醒提前天数，默认 3 |
| `EXPIRED_PANEL_MODE` | 否 | 到期后面板行为：`readonly`（默认，只读可备份）/ `block`（完全锁死） |

修改后重启服务：`systemctl restart ppanel-agent`。

---

## 3. 鉴权

所有 `/open/*` 请求携带请求头：

```
X-API-Key: <OPEN_API_KEY>
```

| 场景 | 状态码 |
|---|---|
| 未携带 / Key 错误 | `401` |
| 节点未配置 OPEN_API_KEY | `403` |

> 注意：`/open/*` 的 Key 与被控 `X-Node-Token`（主控专用）、面板 `panel_token`（买家专用）是三套互相独立的凭证，不可混用。

---

## 4. 接口详解

### 4.1 开通实例

```
POST /open/provision
```

请求体（JSON）：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `name` | string | 是 | - | 实例名称，1-64 字符 |
| `image` | string | 是 | - | 镜像，仅支持节点配置的 `PYTHON_IMAGES` 列表（默认 `python:3.9-slim` ~ `python:3.12-slim`） |
| `days` | int | 否 | 30 | 有效期天数，1-3650 |
| `cpu` | float | 否 | 1.0 | CPU 核数上限，0-8 |
| `mem` | int | 否 | 512 | 内存上限 MB，64-16384 |
| `disk_quota` | int | 否 | 2048 | 磁盘配额 MB（预留字段） |
| `start_cmd` | string | 否 | `python main.py` | 启动命令 |
| `owner_ref` | string | 否 | `""` | 商城订单号/用户标识（≤128 字符），对账用 |

响应示例：

```json
{
  "iid": 9,
  "name": "shop-test",
  "panel_url": "https://p.example.com/panel",
  "panel_token": "vAuoFpRc64iRMOPwvg-THQYQenaLfRbm",
  "ext_port": 35646,
  "expire_at": "2026-10-23T15:25:27.845069",
  "owner_ref": "ORDER-1001"
}
```

| 字段 | 说明 |
|---|---|
| `iid` | 实例 ID，后续接口的主键 |
| `panel_url` | 独立面板入口 |
| `panel_token` | 面板令牌，与 `panel_url` 组成一键登录链接（见第 5 节） |
| `ext_port` | 实例对外服务端口（30000-39999），买家应用监听容器内 8000 映射到此 |
| `expire_at` | 到期时间（UTC，ISO 格式），`null` 表示永久 |

错误：`400` 镜像不支持/参数非法；`502` 拉取镜像或创建容器失败；`503` 外部端口耗尽。

开通成功后实例处于 `created` 状态（容器已创建未运行），买家在面板点击「启动」或调用启动接口即可运行；实例目录已自动生成默认 `main.py`（常驻 HTTP 服务），开箱即跑。

### 4.2 查询实例

```
GET /open/instances/{iid}
```

响应示例：

```json
{
  "iid": 9,
  "name": "shop-test",
  "panel_url": "https://p.example.com/panel",
  "panel_token": "Cy2hHUIgqlTAd0QE2EOl2DKk5kF0T3SV",
  "ext_port": 35646,
  "expire_at": "2026-11-22T15:25:27.845069",
  "owner_ref": "ORDER-1001",
  "status": "running",
  "image": "python:3.9-slim",
  "cpu_limit": 0.5,
  "mem_limit": 256,
  "stats": {
    "running": true,
    "cpu_percent": 2.3,
    "mem_usage_mb": 45.1,
    "mem_limit_mb": 256,
    "mem_percent": 17.6,
    "net_rx_mb": 0.02,
    "net_tx_mb": 0.01
  }
}
```

`status` 取值：`creating` / `created` / `running` / `exited`。
`404`：实例不存在（含已被回收）。

### 4.3 续期

```
POST /open/instances/{iid}/renew
```

请求体：`{"days": 30}`（1-3650）

响应：`{"iid": 9, "expire_at": "2026-11-22T15:25:27.845069"}`

续期规则：**从 `max(当前到期时间, 当前时间)` 起顺延**——未到期续期叠加在原到期日之后，已到期续期从当前时间起算。续期后面板立即恢复（无需重启）。

### 4.4 回收

```
DELETE /open/instances/{iid}?purge=1
```

| 参数 | 说明 |
|---|---|
| 不带 `purge` | 停止并删除容器、删除数据库记录，**保留**实例目录（数据可手动找回） |
| `purge=1` | 额外删除实例目录，数据**不可恢复** |

响应：`{"ok": true, "iid": 9, "purged": true}`

> 回收后面板令牌同时失效（记录已删除），买家面板将无法登录。

### 4.5 重置面板令牌

```
POST /open/instances/{iid}/token-reset
```

响应：同开通响应（`iid` / `panel_url` / `panel_token` / `expire_at` / `owner_ref`）。

适用场景：令牌泄露、买家换手。重置后**旧令牌立即全部失效**（面板登录 401），将新链接交付买家即可。

---

## 5. 买家交付：一键登录链接

开通 / 重置令牌后，把以下链接交付买家（放入订单详情、站内信、邮件均可）：

```
{panel_url}?t={panel_token}
```

示例：`https://p.example.com/panel?t=vAuoFpRc64iRMOPwvg-THQYQenaLfRbm`

买家打开即自动登录进入面板，随后 URL 中的令牌参数会被自动清除（不留痕）。面板 JWT 有效期由节点 `JWT_EXPIRE_MINUTES` 控制（默认 720 分钟 = 12 小时），过期后面板要求重新输入令牌或再次使用链接进入。

> 建议商城在订单页提供「打开面板」按钮（服务端实时取 token 拼 URL），而非把长链接静态存在订单里——令牌重置后旧链接自动失效，实时拼接可避免发旧链接。

---

## 6. 到期机制

| 阶段 | 面板行为 |
|---|---|
| 到期前 ≤3 天 | 面板顶部橙色横幅：「实例将在 N 天后到期，请联系商家续期」 |
| 已到期（readonly 模式，默认） | 红色横幅提示；**可看不可改**：能查看状态/用量/日志/操作记录、能下载文件与备份；启动/停止/文件写入/上传/终端等一切写操作返回 `403 实例已到期，请联系商家续期` |
| 已到期（block 模式） | 面板完全锁死，所有接口 403 |
| 商城续期后 | 立即恢复全部功能，无需重启任何服务 |

注意：到期**不会自动停止容器**（买家服务继续对外提供，避免误伤），是否停机由商城通过续期/回收策略决定。若需到期即停，可在 Webhook 收到 `instance_expiring (tier=expired)` 后调用 `DELETE /open/instances/{iid}`（不带 purge 保留数据）。

---

## 7. Webhook 通知

配置 `WEBHOOK_URL` 后，节点在以下事件发生时向你 POST 一个 JSON（`Content-Type: application/json`，超时 5 秒，失败静默不影响节点运行）：

### 7.1 实例崩溃（每实例 1 小时最多一次）

```json
{
  "event": "instance_exited",
  "iid": 9,
  "name": "shop-test",
  "owner_ref": "ORDER-1001",
  "ts": "2026-09-23T15:30:00"
}
```

> 容器进程退出即触发（含买家主动停止）。可结合查询接口确认状态后决定是否提醒买家。

### 7.2 到期提醒（分档，每档每实例每天最多一次）

```json
{
  "event": "instance_expiring",
  "iid": 9,
  "name": "shop-test",
  "owner_ref": "ORDER-1001",
  "tier": "d3",
  "expire_at": "2026-10-23T15:25:27.845069",
  "days_left": 2.8,
  "ts": "2026-09-23T15:30:00"
}
```

`tier` 取值：`d3`（≤ WEBHOOK_WARN_DAYS 天，默认 3）/ `d1`（≤1 天）/ `expired`（已过期）。

### 7.3 商城侧建议

- 接口幂等：同一 `(event, iid, tier)` 可能因节点重启重复推送（去重状态存内存），按 `ts` 取最新即可。
- 收到 `expired` 后可触发你自己的停机/催缴流程。
- 校验来源：Webhook 请求体可加入你的节点标识（如按 `WEBHOOK_URL` 路径区分节点）。

---

## 8. 全流程对接示例

### 8.1 curl 全流程

```bash
B=http://1.2.3.4:9100
K=ppopen_xxxxxxxxxxxxxxxx

# 1. 开通 30 天实例
curl -s -X POST "$B/open/provision" \
  -H "X-API-Key: $K" -H "Content-Type: application/json" \
  -d '{"name":"buyer-1001","image":"python:3.11-slim","days":30,
       "cpu":1.0,"mem":512,"owner_ref":"ORDER-1001"}'
# → {"iid":9,"panel_url":"...","panel_token":"...",...}

# 2. 查询状态
curl -s "$B/open/instances/9" -H "X-API-Key: $K"

# 3. 买家付款，续期 30 天
curl -s -X POST "$B/open/instances/9/renew" \
  -H "X-API-Key: $K" -H "Content-Type: application/json" -d '{"days":30}'

# 4.（可选）令牌泄露，重置
curl -s -X POST "$B/open/instances/9/token-reset" -H "X-API-Key: $K"

# 5. 订单退款/过期不续，回收（连数据）
curl -s -X DELETE "$B/open/instances/9?purge=1" -H "X-API-Key: $K"
```

### 8.2 Python 对接示例

```python
import requests

BASE = "https://1.2.3.4:9100"
HEADERS = {"X-API-Key": "ppopen_xxxxxxxxxxxxxxxx"}

def provision(order_no: str, days: int = 30, **spec) -> dict:
    body = {"name": f"buyer-{order_no}", "image": "python:3.11-slim",
            "days": days, "owner_ref": order_no, **spec}
    r = requests.post(f"{BASE}/open/provision", json=body, headers=HEADERS, timeout=60)
    r.raise_for_status()
    data = r.json()
    # 交付买家的一键登录链接
    data["login_url"] = f"{data['panel_url']}?t={data['panel_token']}"
    return data

def renew(iid: int, days: int) -> dict:
    r = requests.post(f"{BASE}/open/instances/{iid}/renew",
                      json={"days": days}, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()

def reclaim(iid: int, purge: bool = True):
    requests.delete(f"{BASE}/open/instances/{iid}",
                    params={"purge": int(purge)}, headers=HEADERS, timeout=30)
```

---

## 9. 配置项汇总（被控节点）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `AGENT_HOST` / `AGENT_PORT` | `0.0.0.0` / `9100` | 节点监听 |
| `NODE_TOKEN` | 空 | 主控专用令牌（与 /open 无关） |
| `OPEN_API_KEY` | 空 | **/open/* 密钥，空=禁用** |
| `PANEL_PUBLIC_URL` | 空（按请求 Host 拼） | 面板对外地址 |
| `WEBHOOK_URL` | 空（关闭） | 商城回调地址 |
| `WEBHOOK_WARN_DAYS` | 3 | 到期提醒提前天数 |
| `EXPIRED_PANEL_MODE` | `readonly` | 到期面板行为：readonly / block |
| `PORT_START` / `PORT_END` | `30000` / `39999` | 实例外部端口范围 |
| `PYTHON_IMAGES` | python:3.9 ~ 3.12-slim | 允许开通的镜像 |
| `JWT_SECRET` / `JWT_EXPIRE_MINUTES` | 内置 / 720 | 面板 JWT 签名与有效期 |

---

## 10. 安全建议

1. **OPEN_API_KEY 保密**：仅商城服务端持有，切勿下发到前端；建议定期轮换（轮换 = 改配置重启节点，瞬时生效）。
2. **HTTPS**：生产环境建议给节点面板域名挂 TLS 反代（`PANEL_PUBLIC_URL` 配 https），否则 panel_token 明文传输。
3. **防火墙**：9100 端口只对商城服务器与买家出口开放（面板本身带令牌鉴权，但缩小暴露面更稳）；买家应用端口（30000-39999）按需开放。
4. **回收即删令牌**：确认回收后买家面板立即失效；若只是暂停，用到期（不续期）而非回收。
5. **审计**：所有 /open 操作均写入面板「操作记录」（action 前缀 `open.`），可在买家面板操作记录中追溯。

---

## 附：三套凭证速查

| 凭证 | 持有者 | 用途 | 失效方式 |
|---|---|---|---|
| `X-Node-Token` | PPanel 主控 | 主控管理节点全部接口 `/agent/*` | 改配置重启 |
| `X-API-Key` | 第三方商城 | 开通面 `/open/*` | 同上 |
| `panel_token` | 买家 | 登录独立面板 `/panel` | token-reset / 清空 / 实例回收 |
