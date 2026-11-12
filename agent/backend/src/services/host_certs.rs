//! SSL 证书到期检测（对齐 Python 版 host_monitor.py 的 `/host/certs`）。
//!
//! 域名来源：Caddy 站点配置 `/etc/caddy/sites/*.caddy` 首行 + 实例绑定的域名。
//! 检测方式：TLS 握手直取证书有效期（走 `openssl s_client`），不依赖证书链校验，
//! 自签与 Let's Encrypt 都能测；单域名失败只影响该行，不影响整体。

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use sqlx::SqlitePool;
use tokio::sync::Semaphore;

use crate::services::{caddy, host_ops};

/// 域名白名单校验：只允许字母数字点横线，避免拼进 shell 造成注入。
fn valid_domain(d: &str) -> bool {
    if d.is_empty() || d.len() > 253 || !d.contains('.') {
        return false;
    }
    d.chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_'))
}

/// 从 Caddy 站点文件首行取域名（形如 `panel.zlovey.top {`）。
fn domains_from_caddy() -> Vec<String> {
    let mut out = Vec::new();
    let Ok(rd) = std::fs::read_dir(caddy::SITES_DIR) else {
        return out;
    };
    for entry in rd.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("caddy") {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(&path) else {
            continue;
        };
        for line in text.lines() {
            let s = line.trim();
            if s.is_empty() || s.starts_with('#') {
                continue;
            }
            let Some(head) = s.strip_suffix('{') else {
                continue;
            };
            let head = head.trim();
            // 只认单域名行；带空白的是指令（如 `encode gzip {`）
            if head.is_empty() || head.contains(char::is_whitespace) {
                continue;
            }
            out.push(head.to_string());
            break;
        }
    }
    out
}

async fn collect_domains(db: &SqlitePool) -> Vec<String> {
    let mut set: BTreeSet<String> = BTreeSet::new();
    for d in domains_from_caddy() {
        set.insert(d);
    }
    let rows: Vec<(Option<String>,)> = sqlx::query_as(
        "SELECT domain FROM instances \
         WHERE domain IS NOT NULL AND domain != '' AND ext_port > 0",
    )
    .fetch_all(db)
    .await
    .unwrap_or_default();
    for (d,) in rows {
        if let Some(d) = d {
            let d = d.trim().to_string();
            if !d.is_empty() {
                set.insert(d);
            }
        }
    }
    set.into_iter().filter(|d| valid_domain(d)).collect()
}

/// 解析 openssl 的 `notAfter`：`Feb  2 12:47:48 2027 GMT`（日期与日之间有两个空格）。
fn parse_not_after(raw: &str) -> Option<DateTime<Utc>> {
    let norm = raw.split_whitespace().collect::<Vec<_>>().join(" ");
    let body = norm
        .trim_end_matches("GMT")
        .trim_end_matches("UTC")
        .trim()
        .to_string();
    for fmt in ["%b %d %H:%M:%S %Y", "%Y-%m-%d %H:%M:%S", "%b %e %H:%M:%S %Y"] {
        if let Ok(naive) = chrono::NaiveDateTime::parse_from_str(&body, fmt) {
            return Some(naive.and_utc());
        }
    }
    None
}

/// TLS 握手取 notAfter；失败如实上报错误类型（不抛异常，避免整页 500）。
async fn check_cert(domain: &str) -> Value {
    let script = format!(
        "openssl s_client -servername {d} -connect {d}:443 < /dev/null 2>/dev/null \
         | openssl x509 -noout -enddate 2>/dev/null",
        d = domain
    );
    let (code, out) = host_ops::run(&["sh", "-c", &script], 20).await;
    let mut item = json!({
        "domain": domain,
        "not_after": "",
        "days_left": Value::Null,
        "ok": false,
        "error": "",
    });
    if code != 0 {
        item["error"] = json!("handshake_failed");
        return item;
    }
    let raw = out
        .lines()
        .find_map(|l| l.trim().strip_prefix("notAfter="))
        .unwrap_or("")
        .trim()
        .to_string();
    if raw.is_empty() {
        item["error"] = json!("no_cert");
        return item;
    }
    match parse_not_after(&raw) {
        Some(dt) => {
            item["not_after"] = json!(dt.format("%Y-%m-%d %H:%M").to_string());
            item["days_left"] = json!((dt - Utc::now()).num_days());
            item["ok"] = json!(true);
        }
        None => {
            item["error"] = json!("parse_failed");
        }
    }
    item
}

/// 并发 8 路检测全部域名，按剩余天数升序（None 排最后）。
pub async fn certs(db: &SqlitePool) -> Value {
    let domains = collect_domains(db).await;
    if domains.is_empty() {
        return json!({ "rows": [] });
    }
    let sem = Arc::new(Semaphore::new(8));
    let mut tasks = Vec::new();
    for d in domains {
        let sem = sem.clone();
        tasks.push(tokio::spawn(async move {
            let _permit = sem.acquire_owned().await.ok();
            let row = check_cert(&d).await;
            row
        }));
    }
    let mut rows: Vec<Value> = Vec::new();
    for t in tasks {
        match tokio::time::timeout(Duration::from_secs(25), t).await {
            Ok(Ok(v)) => rows.push(v),
            _ => {}
        }
    }
    rows.sort_by(|a, b| {
        let da = a.get("days_left").and_then(|x| x.as_i64());
        let db_ = b.get("days_left").and_then(|x| x.as_i64());
        match (da, db_) {
            (Some(x), Some(y)) => x.cmp(&y),
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => std::cmp::Ordering::Equal,
        }
    });
    json!({ "rows": rows })
}
