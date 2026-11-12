//! 宿主机监控采样与阈值告警（对齐 Python 版 app/services/host_metrics_service.py）。
//!
//! - 每分钟采样 CPU / 内存 / 磁盘 / 负载 / 网络累计与速率，落 `host_metrics`，保留 7 天；
//! - 告警配置存 `host_alert_config`（单行 id=1），触发记录存 `host_alert_logs`（兼作静默去重游标）；
//! - Webhook 支持飞书 / 钉钉 / Telegram / 自定义（自定义收 `{"event","message"}`）。

use std::collections::BTreeMap;
use std::time::Duration as StdDuration;

use base64::Engine;
use chrono::{Duration, NaiveDateTime, Timelike, Utc};
use hmac::{Hmac, Mac};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::Sha256;
use sqlx::SqlitePool;

use crate::error::{AppError, AppResult};
use crate::models::{dt_iso, round1, round2, utcnow};
use crate::state::AppState;

pub const KEEP_DAYS: i64 = 7;

const VIRTUAL_IF: &[&str] = &[
    "lo", "docker", "veth", "br-", "virbr", "tun", "tap", "kube", "cni", "flannel", "vpp",
    "cali", "wg", "zt",
];

fn virtual_iface(name: &str) -> bool {
    VIRTUAL_IF
        .iter()
        .any(|p| name == *p || name.starts_with(p))
}

// ---------- /proc 采集（阻塞，只在 spawn_blocking 里调） ----------

#[derive(Debug, Default, Clone, Copy)]
struct Raw {
    cpu: f64,
    mem_used: f64,
    mem_total: f64,
    disk_used: f64,
    disk_total: f64,
    load1: f64,
    rx_mb: f64,
    tx_mb: f64,
}

fn read_proc(path: &str) -> String {
    std::fs::read_to_string(path).unwrap_or_default()
}

fn cpu_percent() -> f64 {
    fn snap() -> Option<(u64, u64)> {
        let text = read_proc("/proc/stat");
        let first = text.lines().next()?;
        let mut it = first.split_whitespace();
        if it.next()? != "cpu" {
            return None;
        }
        let nums: Vec<u64> = it.filter_map(|x| x.parse::<u64>().ok()).collect();
        if nums.len() < 4 {
            return None;
        }
        // idle + iowait 视为空闲（与 host_info 概览保持一致）
        let idle = nums[3] + nums.get(4).copied().unwrap_or(0);
        Some((idle, nums.iter().sum()))
    }
    let Some(a) = snap() else { return 0.0 };
    std::thread::sleep(StdDuration::from_millis(250));
    let Some(b) = snap() else { return 0.0 };
    let dt = b.1.saturating_sub(a.1);
    let di = b.0.saturating_sub(a.0);
    if dt == 0 {
        0.0
    } else {
        round1((dt - di) as f64 / dt as f64 * 100.0)
    }
}

fn mem_mb() -> (f64, f64) {
    let text = read_proc("/proc/meminfo");
    let mut total = 0.0f64;
    let mut avail: Option<f64> = None;
    let mut free: Option<f64> = None;
    for line in text.lines() {
        let Some((k, v)) = line.split_once(':') else {
            continue;
        };
        let val = v
            .split_whitespace()
            .next()
            .and_then(|x| x.parse::<f64>().ok())
            .map(|kb| kb / 1024.0)
            .unwrap_or(0.0);
        match k.trim() {
            "MemTotal" => total = val,
            "MemAvailable" => avail = Some(val),
            "MemFree" => free = Some(val),
            _ => {}
        }
    }
    let a = avail.or(free).unwrap_or(0.0);
    (round1((total - a).max(0.0)), round1(total))
}

#[cfg(unix)]
fn disk_gb() -> (f64, f64) {
    unsafe {
        let mut st: libc::statvfs = std::mem::zeroed();
        if libc::statvfs(b"/\0".as_ptr() as *const libc::c_char, &mut st) == 0 {
            let fr = st.f_frsize as f64;
            let total = st.f_blocks as f64 * fr;
            let avail = st.f_bavail as f64 * fr;
            let used = (total - avail).max(0.0);
            return (round1(used / 1073741824.0), round1(total / 1073741824.0));
        }
    }
    (0.0, 0.0)
}

#[cfg(not(unix))]
fn disk_gb() -> (f64, f64) {
    (0.0, 0.0)
}

fn net_mb() -> (f64, f64) {
    let text = read_proc("/proc/net/dev");
    let mut rx = 0u64;
    let mut tx = 0u64;
    for line in text.lines().skip(2) {
        let Some((name, rest)) = line.split_once(':') else {
            continue;
        };
        let name = name.trim();
        if name.is_empty() || virtual_iface(name) {
            continue;
        }
        let f: Vec<&str> = rest.split_whitespace().collect();
        if f.len() < 9 {
            continue;
        }
        rx += f[0].parse::<u64>().unwrap_or(0);
        tx += f[8].parse::<u64>().unwrap_or(0);
    }
    (
        round2(rx as f64 / 1048576.0),
        round2(tx as f64 / 1048576.0),
    )
}

fn load1() -> f64 {
    read_proc("/proc/loadavg")
        .split_whitespace()
        .next()
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(0.0)
}

fn collect() -> Raw {
    let (mem_used, mem_total) = mem_mb();
    let (disk_used, disk_total) = disk_gb();
    let (rx_mb, tx_mb) = net_mb();
    Raw {
        cpu: cpu_percent(),
        mem_used,
        mem_total,
        disk_used,
        disk_total,
        load1: round2(load1()),
        rx_mb,
        tx_mb,
    }
}

// ---------- 采样落库 ----------

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct MetricRow {
    pub id: i64,
    pub ts: Option<NaiveDateTime>,
    pub cpu_percent: f64,
    pub mem_used_mb: f64,
    pub mem_total_mb: f64,
    pub disk_used_gb: f64,
    pub disk_total_gb: f64,
    pub load1: f64,
    pub net_rx_mb: f64,
    pub net_tx_mb: f64,
    pub rx_rate_kbps: f64,
    pub tx_rate_kbps: f64,
}

const METRIC_COLS: &str = "id, ts, cpu_percent, mem_used_mb, mem_total_mb, disk_used_gb, \
                           disk_total_gb, load1, net_rx_mb, net_tx_mb, rx_rate_kbps, tx_rate_kbps";

/// 采样一次写入 host_metrics；顺带清理 7 天前旧数据。
pub async fn sample_once(db: &SqlitePool) -> AppResult<()> {
    let raw = tokio::task::spawn_blocking(collect)
        .await
        .map_err(|e| AppError::internal(format!("采样线程异常：{e}")))?;
    let now = utcnow();

    let prev: Option<(NaiveDateTime, f64, f64)> = sqlx::query_as(
        "SELECT ts, net_rx_mb, net_tx_mb FROM host_metrics WHERE ts IS NOT NULL ORDER BY id DESC LIMIT 1",
    )
    .fetch_optional(db)
    .await
    .ok()
    .flatten();

    let (rx_rate, tx_rate) = match prev {
        Some((ts, prx, ptx)) if raw.rx_mb > 0.0 && prx > 0.0 => {
            let dt = (now - ts).num_milliseconds() as f64 / 1000.0;
            if dt > 0.0 {
                (
                    round1(((raw.rx_mb - prx).max(0.0)) * 1024.0 / dt),
                    round1(((raw.tx_mb - ptx).max(0.0)) * 1024.0 / dt),
                )
            } else {
                (0.0, 0.0)
            }
        }
        _ => (0.0, 0.0),
    };

    sqlx::query(
        "INSERT INTO host_metrics \
         (ts, cpu_percent, mem_used_mb, mem_total_mb, disk_used_gb, disk_total_gb, load1, \
          net_rx_mb, net_tx_mb, rx_rate_kbps, tx_rate_kbps) \
         VALUES (?,?,?,?,?,?,?,?,?,?,?)",
    )
    .bind(now)
    .bind(raw.cpu)
    .bind(raw.mem_used)
    .bind(raw.mem_total)
    .bind(raw.disk_used)
    .bind(raw.disk_total)
    .bind(raw.load1)
    .bind(raw.rx_mb)
    .bind(raw.tx_mb)
    .bind(rx_rate)
    .bind(tx_rate)
    .execute(db)
    .await?;

    let cutoff = now - Duration::days(KEEP_DAYS);
    let _ = sqlx::query("DELETE FROM host_metrics WHERE ts < ?")
        .bind(cutoff)
        .execute(db)
        .await;
    Ok(())
}

/// 历史曲线：24h（5 分钟粒度）/ 7d（30 分钟粒度），含区间收发流量合计。
pub async fn aggregate(db: &SqlitePool, hours: i64) -> Value {
    let hours = if hours > 24 { 168 } else { hours.clamp(1, 24) };
    let bucket: i64 = if hours <= 24 { 5 } else { 30 };
    let since = utcnow() - Duration::hours(hours);

    let rows: Vec<MetricRow> = sqlx::query_as(&format!(
        "SELECT {METRIC_COLS} FROM host_metrics WHERE ts >= ? ORDER BY ts ASC"
    ))
    .bind(since)
    .fetch_all(db)
    .await
    .unwrap_or_default();

    if rows.is_empty() {
        return json!({"points": [], "totals": {"rx_mb": 0, "tx_mb": 0}, "bucket_min": bucket});
    }

    let mut buckets: BTreeMap<NaiveDateTime, Vec<&MetricRow>> = BTreeMap::new();
    for r in &rows {
        let Some(ts) = r.ts else { continue };
        let base = ts
            .with_second(0)
            .and_then(|t| t.with_nanosecond(0))
            .unwrap_or(ts);
        let key = base - Duration::minutes(base.minute() as i64 % bucket);
        buckets.entry(key).or_default().push(r);
    }

    let mut points: Vec<Value> = Vec::new();
    for (key, items) in buckets {
        let n = items.len().max(1) as f64;
        let last = items.last().copied();
        let mem_total = last.map(|r| r.mem_total_mb).unwrap_or(0.0);
        let mem_sum: f64 = items.iter().map(|r| r.mem_used_mb).sum();
        let cpu_sum: f64 = items.iter().map(|r| r.cpu_percent).sum();
        let load_sum: f64 = items.iter().map(|r| r.load1).sum();
        let rx_sum: f64 = items.iter().map(|r| r.rx_rate_kbps).sum();
        let tx_sum: f64 = items.iter().map(|r| r.tx_rate_kbps).sum();
        let disk = last
            .filter(|r| r.disk_total_gb > 0.0)
            .map(|r| round1(r.disk_used_gb / r.disk_total_gb * 100.0))
            .unwrap_or(0.0);
        points.push(json!({
            "ts": dt_iso(&key),
            "cpu": round1(cpu_sum / n),
            "mem": if mem_total > 0.0 { round1(mem_sum / n / mem_total * 100.0) } else { 0.0 },
            "disk": disk,
            "load": round2(load_sum / n),
            "rx_rate": round1(rx_sum / n),
            "tx_rate": round1(tx_sum / n),
        }));
    }

    let first = rows.first();
    let last = rows.last();
    let (rx_mb, tx_mb) = match (first, last) {
        (Some(f), Some(l)) => (
            round1((l.net_rx_mb - f.net_rx_mb).max(0.0)),
            round1((l.net_tx_mb - f.net_tx_mb).max(0.0)),
        ),
        _ => (0.0, 0.0),
    };
    json!({
        "points": points,
        "totals": {"rx_mb": rx_mb, "tx_mb": tx_mb},
        "bucket_min": bucket,
    })
}

/// 实时流量：各物理网卡累计收发 + 近 24h 合计。
pub async fn traffic(db: &SqlitePool) -> Value {
    let text = read_proc("/proc/net/dev");
    let mut ifaces: Vec<Value> = Vec::new();
    for line in text.lines().skip(2) {
        let Some((name, rest)) = line.split_once(':') else {
            continue;
        };
        let name = name.trim();
        if name.is_empty() || virtual_iface(name) {
            continue;
        }
        let f: Vec<&str> = rest.split_whitespace().collect();
        if f.len() < 9 {
            continue;
        }
        ifaces.push(json!({
            "name": name,
            "rx_mb": round1(f[0].parse::<u64>().unwrap_or(0) as f64 / 1048576.0),
            "tx_mb": round1(f[8].parse::<u64>().unwrap_or(0) as f64 / 1048576.0),
        }));
    }

    let since = utcnow() - Duration::hours(24);
    let rows: Vec<MetricRow> = sqlx::query_as(&format!(
        "SELECT {METRIC_COLS} FROM host_metrics WHERE ts >= ? ORDER BY ts ASC"
    ))
    .bind(since)
    .fetch_all(db)
    .await
    .unwrap_or_default();
    let totals = match (rows.first(), rows.last()) {
        (Some(f), Some(l)) => json!({
            "rx_mb": round1((l.net_rx_mb - f.net_rx_mb).max(0.0)),
            "tx_mb": round1((l.net_tx_mb - f.net_tx_mb).max(0.0)),
        }),
        _ => json!({"rx_mb": 0, "tx_mb": 0}),
    };
    json!({"interfaces": ifaces, "totals_24h": totals})
}

// ---------- 告警配置 ----------

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct AlertRow {
    pub enabled: i64,
    pub cpu_threshold: f64,
    pub mem_threshold: f64,
    pub disk_threshold: f64,
    pub webhook_type: String,
    pub webhook_url: String,
    pub webhook_extra: String,
    pub quiet_minutes: i64,
}

#[derive(Debug, Deserialize)]
pub struct AlertIn {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "d90")]
    pub cpu_threshold: f64,
    #[serde(default = "d90")]
    pub mem_threshold: f64,
    #[serde(default = "d90")]
    pub disk_threshold: f64,
    #[serde(default)]
    pub webhook_type: String,
    #[serde(default)]
    pub webhook_url: String,
    #[serde(default)]
    pub webhook_extra: String,
    #[serde(default = "d10")]
    pub quiet_minutes: i64,
}

fn d90() -> f64 {
    90.0
}
fn d10() -> i64 {
    10
}

async fn ensure_config(db: &SqlitePool) -> AppResult<AlertRow> {
    sqlx::query("INSERT OR IGNORE INTO host_alert_config (id, updated_at) VALUES (1, ?)")
        .bind(utcnow())
        .execute(db)
        .await?;
    let row = sqlx::query_as::<_, AlertRow>(
        "SELECT enabled, cpu_threshold, mem_threshold, disk_threshold, webhook_type, \
                webhook_url, webhook_extra, quiet_minutes \
         FROM host_alert_config WHERE id = 1",
    )
    .fetch_one(db)
    .await?;
    Ok(row)
}

pub async fn alerts_get(db: &SqlitePool) -> AppResult<Value> {
    let cfg = ensure_config(db).await?;
    #[derive(sqlx::FromRow)]
    struct LogRow {
        ts: Option<NaiveDateTime>,
        metric: String,
        value: f64,
        threshold: f64,
        message: String,
        sent: i64,
    }
    let logs: Vec<LogRow> = sqlx::query_as(
        "SELECT ts, metric, value, threshold, message, sent FROM host_alert_logs \
         ORDER BY id DESC LIMIT 50",
    )
    .fetch_all(db)
    .await
    .unwrap_or_default();
    let items: Vec<Value> = logs
        .iter()
        .map(|l| {
            json!({
                "ts": l.ts.map(|t| dt_iso(&t)).unwrap_or_default(),
                "metric": l.metric,
                "value": l.value,
                "threshold": l.threshold,
                "message": l.message,
                "sent": l.sent != 0,
            })
        })
        .collect();
    Ok(json!({
        "config": {
            "enabled": cfg.enabled != 0,
            "cpu_threshold": cfg.cpu_threshold,
            "mem_threshold": cfg.mem_threshold,
            "disk_threshold": cfg.disk_threshold,
            "webhook_type": cfg.webhook_type,
            "webhook_url": cfg.webhook_url,
            "webhook_extra": cfg.webhook_extra,
            "quiet_minutes": cfg.quiet_minutes,
        },
        "logs": items,
    }))
}

pub async fn alerts_put(db: &SqlitePool, body: &AlertIn) -> AppResult<Value> {
    for (name, v) in [
        ("CPU", body.cpu_threshold),
        ("内存", body.mem_threshold),
        ("磁盘", body.disk_threshold),
    ] {
        if !(1.0..=100.0).contains(&v) {
            return Err(AppError::bad_request(format!("{name}阈值需在 1-100 之间")));
        }
    }
    let ty = body.webhook_type.trim();
    if !ty.is_empty() && !matches!(ty, "feishu" | "dingtalk" | "telegram" | "custom") {
        return Err(AppError::bad_request("不支持的 Webhook 类型"));
    }
    let _ = ensure_config(db).await?;
    sqlx::query(
        "UPDATE host_alert_config SET enabled = ?, cpu_threshold = ?, mem_threshold = ?, \
         disk_threshold = ?, webhook_type = ?, webhook_url = ?, webhook_extra = ?, \
         quiet_minutes = ?, updated_at = ? WHERE id = 1",
    )
    .bind(if body.enabled { 1 } else { 0 })
    .bind(body.cpu_threshold)
    .bind(body.mem_threshold)
    .bind(body.disk_threshold)
    .bind(ty)
    .bind(body.webhook_url.trim())
    .bind(body.webhook_extra.trim())
    .bind(body.quiet_minutes.clamp(1, 1440))
    .bind(utcnow())
    .execute(db)
    .await?;
    Ok(json!({"detail": "告警配置已保存"}))
}

pub async fn alerts_test(db: &SqlitePool) -> AppResult<Value> {
    let cfg = ensure_config(db).await?;
    if cfg.webhook_url.trim().is_empty() {
        return Err(AppError::bad_request("请先填写 Webhook 地址"));
    }
    if !push_webhook(&cfg, "[PPanel 节点告警] 这是一条测试消息，收到即说明推送链路正常").await {
        return Err(AppError::bad_gateway(
            "推送失败：请检查 Webhook 地址/密钥/网络",
        ));
    }
    Ok(json!({"detail": "测试消息已发送"}))
}

pub async fn alerts_clear_logs(db: &SqlitePool) -> AppResult<Value> {
    sqlx::query("DELETE FROM host_alert_logs")
        .execute(db)
        .await?;
    Ok(json!({"detail": "告警记录已清空"}))
}

// ---------- Webhook 推送 ----------

fn feishu_sign(secret: &str) -> (String, String) {
    let ts = Utc::now().timestamp().to_string();
    let key = format!("{ts}\n{secret}");
    let mut mac = Hmac::<Sha256>::new_from_slice(key.as_bytes()).expect("hmac key");
    mac.update(b"");
    let sign = base64::engine::general_purpose::STANDARD.encode(mac.finalize().into_bytes());
    (ts, sign)
}

fn dingtalk_sign(secret: &str) -> String {
    let ts = Utc::now().timestamp_millis().to_string();
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).expect("hmac key");
    mac.update(format!("{ts}\n{secret}").as_bytes());
    let raw = base64::engine::general_purpose::STANDARD.encode(mac.finalize().into_bytes());
    let enc = percent_encoding::utf8_percent_encode(&raw, percent_encoding::NON_ALPHANUMERIC)
        .to_string();
    format!("&timestamp={ts}&sign={enc}")
}

async fn push_webhook(cfg: &AlertRow, text: &str) -> bool {
    let url = cfg.webhook_url.trim();
    if url.is_empty() {
        return false;
    }
    let extra = cfg.webhook_extra.trim();
    let client = reqwest::Client::builder()
        .timeout(StdDuration::from_secs(8))
        .build()
        .unwrap_or_default();
    let resp = match cfg.webhook_type.trim() {
        "feishu" => {
            let mut payload = json!({"msg_type": "text", "content": {"text": text}});
            if !extra.is_empty() {
                let (ts, sign) = feishu_sign(extra);
                payload["timestamp"] = json!(ts);
                payload["sign"] = json!(sign);
            }
            client.post(url).json(&payload).send().await
        }
        "dingtalk" => {
            let full = if extra.is_empty() {
                url.to_string()
            } else {
                format!("{url}{}", dingtalk_sign(extra))
            };
            client
                .post(full)
                .json(&json!({"msgtype": "text", "text": {"content": text}}))
                .send()
                .await
        }
        "telegram" => {
            client
                .post(url)
                .json(&json!({
                    "chat_id": extra,
                    "text": text,
                    "disable_web_page_preview": true
                }))
                .send()
                .await
        }
        _ => {
            client
                .post(url)
                .json(&json!({"event": "ppanel_host_alert", "message": text}))
                .send()
                .await
        }
    };
    match resp {
        Ok(r) => r.status().is_success(),
        Err(_) => false,
    }
}

// ---------- 阈值检测 ----------

/// 越限且不在静默期 → 落告警记录 + 推送 Webhook。
pub async fn check_alerts(db: &SqlitePool) {
    let row: Option<MetricRow> = sqlx::query_as(&format!(
        "SELECT {METRIC_COLS} FROM host_metrics ORDER BY id DESC LIMIT 1"
    ))
    .fetch_optional(db)
    .await
    .ok()
    .flatten();
    let Some(row) = row else { return };
    let Ok(cfg) = ensure_config(db).await else {
        return;
    };
    if cfg.enabled == 0 {
        return;
    }

    let mem_p = if row.mem_total_mb > 0.0 {
        round1(row.mem_used_mb / row.mem_total_mb * 100.0)
    } else {
        0.0
    };
    let disk_p = if row.disk_total_gb > 0.0 {
        round1(row.disk_used_gb / row.disk_total_gb * 100.0)
    } else {
        0.0
    };
    let checks = [
        (
            "cpu",
            row.cpu_percent,
            cfg.cpu_threshold,
            format!("CPU 使用率 {}%（阈值 {}%）", row.cpu_percent, cfg.cpu_threshold),
        ),
        (
            "mem",
            mem_p,
            cfg.mem_threshold,
            format!("内存使用率 {mem_p}%（阈值 {}%）", cfg.mem_threshold),
        ),
        (
            "disk",
            disk_p,
            cfg.disk_threshold,
            format!("磁盘使用率 {disk_p}%（阈值 {}%）", cfg.disk_threshold),
        ),
    ];
    let quiet = cfg.quiet_minutes.max(1) * 60;
    let now = utcnow();
    for (metric, value, threshold, msg) in checks {
        if value < threshold {
            continue;
        }
        let last: Option<(Option<NaiveDateTime>,)> = sqlx::query_as(
            "SELECT ts FROM host_alert_logs WHERE metric = ? ORDER BY id DESC LIMIT 1",
        )
        .bind(metric)
        .fetch_optional(db)
        .await
        .ok()
        .flatten();
        if let Some((Some(ts),)) = last {
            if (now - ts).num_seconds() < quiet {
                continue;
            }
        }
        let sent = if cfg.webhook_url.trim().is_empty() {
            false
        } else {
            push_webhook(&cfg, &format!("[PPanel 节点告警] {msg}")).await
        };
        let _ = sqlx::query(
            "INSERT INTO host_alert_logs (ts, metric, value, threshold, message, sent) \
             VALUES (?,?,?,?,?,?)",
        )
        .bind(now)
        .bind(metric)
        .bind(value)
        .bind(threshold)
        .bind(&msg)
        .bind(if sent { 1 } else { 0 })
        .execute(db)
        .await;
    }
}

// ---------- 后台循环 ----------

/// 每 60s 采样一次并做阈值检测（首次延后 8s，避开启动瞬间的实例巡检）。
pub async fn run_loop(state: AppState) {
    tokio::time::sleep(StdDuration::from_secs(8)).await;
    loop {
        if let Err(e) = sample_once(&state.db).await {
            tracing::debug!("宿主采样失败：{}", e.detail);
        }
        check_alerts(&state.db).await;
        tokio::time::sleep(StdDuration::from_secs(60)).await;
    }
}
