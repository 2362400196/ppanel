"""宿主机监控采样与阈值告警（零依赖 /proc 采集 + SQLite 时序 + Webhook 推送）。

- 每分钟采样 CPU / 内存 / 磁盘 / 负载 / 网络流量，落 host_metrics 表，保留 7 天；
- 告警配置存 host_alert_config（单行），触发记录存 host_alert_logs（兼作静默去重游标）；
- Webhook 支持飞书 / 钉钉 / Telegram / 自定义（自定义收 JSON {"message": ...}）。
"""
import base64
import hashlib
import hmac
import shutil
import time
import urllib.parse

from sqlalchemy.orm import Session

from app.models import HostAlertConfig, HostAlertLog, HostMetric, utcnow

KEEP_DAYS = 7          # 曲线数据保留天数
VIRTUAL_IF = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap", "kube", "cni", "flannel", "vpp", "cali", "wg", "zt")


# ---------- /proc 采集 ----------

def _read_proc(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _cpu_percent() -> float:
    """两次 /proc/stat 采样算 CPU 使用率（阻塞约 0.25s，仅后台线程调用）。"""
    def snap():
        line = _read_proc("/proc/stat").splitlines()
        if not line or not line[0].startswith("cpu "):
            return None
        vals = [int(x) for x in line[0].split()[1:]]
        return vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)

    t1 = snap()
    if t1 is None:
        return 0.0
    time.sleep(0.25)
    t2 = snap()
    if t2 is None:
        return 0.0
    dt, di = t2[1] - t1[1], t2[0] - t1[0]
    return round((1 - di / dt) * 100, 1) if dt > 0 else 0.0


def _meminfo_mb() -> tuple[float, float]:
    info = {}
    for line in _read_proc("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts:
            try:
                info[k.strip()] = int(parts[0]) / 1024  # kB → MB
            except ValueError:
                pass
    total = info.get("MemTotal", 0.0)
    avail = info.get("MemAvailable", info.get("MemFree", 0.0))
    return round(total - avail, 1), round(total, 1)


def _disk_gb() -> tuple[float, float]:
    try:
        u = shutil.disk_usage("/")
        return round(u.used / 1024 ** 3, 1), round(u.total / 1024 ** 3, 1)
    except OSError:
        return 0.0, 0.0


def _net_bytes() -> tuple[int, int]:
    """物理网卡累计收发字节数之和（排除 lo / docker / veth 等虚拟接口）。"""
    rx = tx = 0
    for line in _read_proc("/proc/net/dev").splitlines()[2:]:
        name, _, data = line.partition(":")
        name = name.strip()
        if not name or any(name.startswith(p) or name == p for p in VIRTUAL_IF):
            continue
        cols = data.split()
        if len(cols) >= 9:
            try:
                rx += int(cols[0])
                tx += int(cols[8])
            except ValueError:
                pass
    return rx, tx


def _load1() -> float:
    parts = _read_proc("/proc/loadavg").split()
    try:
        return float(parts[0])
    except (IndexError, ValueError):
        return 0.0


# ---------- 采样落库 ----------

def sample_once(db: Session) -> HostMetric | None:
    """采样一次写入 host_metrics；顺带清理 7 天前旧数据。"""
    now = time.time()
    cpu = _cpu_percent()
    mem_used, mem_total = _meminfo_mb()
    disk_used, disk_total = _disk_gb()
    rx, tx = _net_bytes()

    last = (db.query(HostMetric).order_by(HostMetric.id.desc()).first())
    if last and rx and tx:
        dt = now - last.ts.replace(tzinfo=None).timestamp()
        prev_rx_mb, prev_tx_mb = last.net_rx_mb, last.net_tx_mb
        if dt > 0 and prev_rx_mb > 0:
            rx_rate = max(0.0, (rx / 1024 / 1024 - prev_rx_mb)) * 1024 / dt  # KB/s
            tx_rate = max(0.0, (tx / 1024 / 1024 - prev_tx_mb)) * 1024 / dt
        else:
            rx_rate = tx_rate = 0.0
    else:
        rx_rate = tx_rate = 0.0

    row = HostMetric(
        ts=utcnow(), cpu_percent=cpu,
        mem_used_mb=mem_used, mem_total_mb=mem_total,
        disk_used_gb=disk_used, disk_total_gb=disk_total,
        load1=_load1(),
        net_rx_mb=round(rx / 1024 / 1024, 2), net_tx_mb=round(tx / 1024 / 1024, 2),
        rx_rate_kbps=round(rx_rate, 1), tx_rate_kbps=round(tx_rate, 1),
    )
    db.add(row)
    cutoff = utcnow().replace(microsecond=0)
    from datetime import timedelta
    db.query(HostMetric).filter(
        HostMetric.ts < cutoff - timedelta(days=KEEP_DAYS)).delete()
    db.commit()
    return row


# ---------- Webhook 推送 ----------

def _feishu_sign(secret: str) -> dict:
    ts = str(int(time.time()))
    digest = hmac.new(f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
    return {"timestamp": ts, "sign": base64.b64encode(digest).decode()}


def _dingtalk_sign(secret: str) -> str:
    ts = str(round(time.time() * 1000))
    digest = hmac.new(secret.encode(), f"{ts}\n{secret}".encode(),
                      digestmod=hashlib.sha256).digest()
    return f"&timestamp={ts}&sign={urllib.parse.quote_plus(base64.b64encode(digest))}"


def push_webhook(cfg: HostAlertConfig, text: str) -> bool:
    """按渠道类型推送文本消息；返回是否发送成功。"""
    import httpx

    url = (cfg.webhook_url or "").strip()
    if not url:
        return False
    extra = (cfg.webhook_extra or "").strip()
    try:
        if cfg.webhook_type == "feishu":
            payload = {"msg_type": "text", "content": {"text": text}}
            if extra:
                payload.update(_feishu_sign(extra))
            r = httpx.post(url, json=payload, timeout=8)
        elif cfg.webhook_type == "dingtalk":
            r = httpx.post(url + (_dingtalk_sign(extra) if extra else ""),
                           json={"msgtype": "text", "text": {"content": text}}, timeout=8)
        elif cfg.webhook_type == "telegram":
            r = httpx.post(url, json={"chat_id": extra, "text": text,
                                      "disable_web_page_preview": True}, timeout=8)
        else:  # custom：通用 JSON，message 字段承载内容
            r = httpx.post(url, json={"event": "ppanel_host_alert", "message": text}, timeout=8)
        return r.status_code < 300
    except Exception:  # noqa: BLE001 推送失败不阻塞采样
        return False


# ---------- 阈值告警检测 ----------

def get_or_init_config(db: Session) -> HostAlertConfig:
    cfg = db.get(HostAlertConfig, 1)
    if not cfg:
        cfg = HostAlertConfig(id=1)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def check_alerts(db: Session, row: HostMetric | None = None) -> None:
    """阈值检测：越限且不在静默期 → 落告警记录 + 推送 Webhook。"""
    row = row or (db.query(HostMetric).order_by(HostMetric.id.desc()).first())
    if not row:
        return
    cfg = get_or_init_config(db)
    if not cfg.enabled:
        return

    mem_p = round(row.mem_used_mb / row.mem_total_mb * 100, 1) if row.mem_total_mb else 0
    disk_p = round(row.disk_used_gb / row.disk_total_gb * 100, 1) if row.disk_total_gb else 0
    checks = [
        ("cpu", row.cpu_percent, cfg.cpu_threshold, f"CPU 使用率 {row.cpu_percent}%（阈值 {cfg.cpu_threshold}%）"),
        ("mem", mem_p, cfg.mem_threshold, f"内存使用率 {mem_p}%（阈值 {cfg.mem_threshold}%）"),
        ("disk", disk_p, cfg.disk_threshold, f"磁盘使用率 {disk_p}%（阈值 {cfg.disk_threshold}%）"),
    ]
    quiet = max(1, int(cfg.quiet_minutes or 10)) * 60
    now_ts = utcnow().replace(tzinfo=None).timestamp()
    for metric, value, threshold, msg in checks:
        if value < threshold:
            continue
        last = (db.query(HostAlertLog).filter(HostAlertLog.metric == metric)
                .order_by(HostAlertLog.id.desc()).first())
        if last and now_ts - last.ts.timestamp() < quiet:
            continue
        sent = 0
        if cfg.webhook_url:
            sent = 1 if push_webhook(cfg, f"[PPanel 节点告警] {msg}") else 0
        db.add(HostAlertLog(ts=utcnow(), metric=metric, value=value,
                            threshold=threshold, message=msg, sent=sent))
        db.commit()
