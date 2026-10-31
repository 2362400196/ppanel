//! 域名绑定 / 证书签发进度日志。
//!
//! 绑定域名是耗时操作：DNS 校验 → 写 Caddy 站点 → 重载 → Let's Encrypt 签发 → HTTPS 探测。
//! 面板需要看到每一步的进度和最终结果（否则只能盯着“已绑定”猜到底成没成），
//! 因此把全过程写成带时间戳的日志行，落在这个进程内的环形缓冲里，
//! 由 `GET /panel/instance/domain/log?since=N` 增量拉取（前端 1 秒轮询一次）。
//!
//! 设计要点：
//! - 绑定接口立即返回，真正流程 `tokio::spawn` 到后台，避免请求挂 1~2 分钟；
//! - 日志按实例 id 存放，重新绑定会重置（一个实例同时只有一条域名流水线）；
//! - Caddy 自身日志（journalctl / docker logs）按行去重后追加，能看到 ACME 的真实进度；
//! - 成功判定以「HTTPS 探测通过」优先，其次「证书文件刚落盘」（兼容 NAT 回环不通的机器）。

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::models::Instance;
use crate::services::caddy;
use crate::state::AppState;

/// 单条日志
#[derive(Clone)]
struct Line {
    t: String,
    text: String,
    level: String,
}

struct Job {
    domain: String,
    status: String, // running / ok / error
    lines: Vec<Line>,
    started: i64,
    ended: i64,
    /// 已收录的 Caddy 原始日志（去重用）
    seen: Vec<String>,
}

impl Default for Job {
    fn default() -> Self {
        Self {
            domain: String::new(),
            status: "running".to_string(),
            lines: Vec::new(),
            started: 0,
            ended: 0,
            seen: Vec::new(),
        }
    }
}

const MAX_LINES: usize = 500;
/// 等待签发的最长时间（秒）
const WAIT_SECS: u64 = 120;
/// 轮询间隔（秒）
const TICK_SECS: u64 = 3;

fn jobs() -> &'static Mutex<HashMap<i64, Job>> {
    static JOBS: OnceLock<Mutex<HashMap<i64, Job>>> = OnceLock::new();
    JOBS.get_or_init(|| Mutex::new(HashMap::new()))
}

/// 步骤序号 → ①②③④…
fn mark(n: usize) -> &'static str {
    const M: [&str; 6] = ["①", "②", "③", "④", "⑤", "⑥"];
    M.get(n.saturating_sub(1)).copied().unwrap_or("•")
}

fn now_hms() -> String {
    chrono::Local::now().format("%H:%M:%S").to_string()
}

fn push(iid: i64, level: &str, text: &str) {
    let mut g = match jobs().lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    let job = g.entry(iid).or_default();
    job.lines.push(Line {
        t: now_hms(),
        text: text.to_string(),
        level: level.to_string(),
    });
    if job.lines.len() > MAX_LINES {
        let cut = job.lines.len() - MAX_LINES;
        job.lines.drain(0..cut);
    }
}

/// 开始一轮新的绑定流程（重置历史日志）。
pub fn begin(iid: i64, domain: &str) {
    let mut g = match jobs().lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    let now = chrono::Utc::now().timestamp();
    g.insert(
        iid,
        Job {
            domain: domain.to_string(),
            status: "running".to_string(),
            lines: Vec::new(),
            started: now,
            ended: 0,
            seen: Vec::new(),
        },
    );
}

pub fn info(iid: i64, text: &str) {
    push(iid, "info", text);
}

pub fn ok(iid: i64, text: &str) {
    push(iid, "ok", text);
}

pub fn warn(iid: i64, text: &str) {
    push(iid, "warn", text);
}

pub fn err(iid: i64, text: &str) {
    push(iid, "err", text);
}

/// 结束本轮流程：`ok=false` 时状态为 error。
pub fn finish(iid: i64, ok_flag: bool, msg: &str) {
    if !msg.is_empty() {
        push(iid, if ok_flag { "ok" } else { "err" }, msg);
    }
    let mut g = match jobs().lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    if let Some(job) = g.get_mut(&iid) {
        job.status = if ok_flag { "ok".to_string() } else { "error".to_string() };
        job.ended = chrono::Utc::now().timestamp();
    }
}

/// 追加一条 Caddy 原生日志（同一条只收一次）。
fn push_caddy(iid: i64, raw: &str) {
    let text = clean_caddy_line(raw);
    if text.is_empty() {
        return;
    }
    let mut g = match jobs().lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    let Some(job) = g.get_mut(&iid) else { return };
    if job.seen.iter().any(|s| s == &text) {
        return;
    }
    job.seen.push(text.clone());
    if job.seen.len() > 300 {
        let cut = job.seen.len() - 300;
        job.seen.drain(0..cut);
    }
    job.lines.push(Line {
        t: now_hms(),
        text,
        level: "caddy".to_string(),
    });
    if job.lines.len() > MAX_LINES {
        let cut = job.lines.len() - MAX_LINES;
        job.lines.drain(0..cut);
    }
}

/// Caddy 日志裁剪：去掉时间戳/JSON 外壳，只留可读的一段。
fn clean_caddy_line(raw: &str) -> String {
    let s = raw.trim();
    if s.is_empty() {
        return String::new();
    }
    // 容器 tty 日志可能带 2026-10-31T02:11:22.123Z 前缀
    let mut body = s;
    if let Some(pos) = s.find('{') {
        if let Ok(v) = serde_json::from_str::<Value>(&s[pos..]) {
            let msg = v.get("msg").and_then(|m| m.as_str()).unwrap_or("");
            let logger = v.get("logger").and_then(|m| m.as_str()).unwrap_or("");
            let err = v
                .get("error")
                .and_then(|m| m.as_str())
                .unwrap_or("");
            let mut out = String::new();
            if !logger.is_empty() {
                out.push_str(&format!("[{logger}] "));
            }
            out.push_str(msg);
            if !err.is_empty() {
                out.push_str(&format!("：{err}"));
            }
            if !out.trim().is_empty() {
                return out.chars().take(300).collect();
            }
        }
        body = &s[..pos];
    }
    body.chars().take(300).collect()
}

/// 当前任务快照（`since` 为已读条数）。
pub fn snapshot(iid: i64, since: usize) -> Value {
    let g = match jobs().lock() {
        Ok(g) => g,
        Err(_) => return json!({"lines": [], "total": 0, "status": ""}),
    };
    match g.get(&iid) {
        None => json!({"lines": [], "total": 0, "status": ""}),
        Some(job) => {
            let from = since.min(job.lines.len());
            let lines: Vec<Value> = job.lines[from..]
                .iter()
                .map(|l| json!({"t": l.t, "text": l.text, "level": l.level}))
                .collect();
            json!({
                "domain": job.domain,
                "status": job.status,
                "started_at": job.started,
                "ended_at": job.ended,
                "lines": lines,
                "total": job.lines.len(),
            })
        }
    }
}

pub fn status_of(iid: i64) -> String {
    let g = match jobs().lock() {
        Ok(g) => g,
        Err(_) => return String::new(),
    };
    g.get(&iid).map(|j| j.status.clone()).unwrap_or_default()
}

fn domain_of(iid: i64) -> String {
    let g = match jobs().lock() {
        Ok(g) => g,
        Err(_) => return String::new(),
    };
    g.get(&iid).map(|j| j.domain.clone()).unwrap_or_default()
}

/// 本轮是否已被新的绑定覆盖（用户连续改域名时，旧任务应自行退出）。
fn superseded(iid: i64, domain: &str) -> bool {
    let d = domain_of(iid);
    !d.is_empty() && d != domain
}

/// 后台执行完整绑定流程。必须 `tokio::spawn` 调用。
pub async fn bind(state: AppState, inst: Instance, domain: String) {
    let iid = inst.id;
    let t0 = Instant::now();
    let epoch = chrono::Utc::now().timestamp();

    info(iid, &format!("开始为实例 #{} 绑定域名 {domain}", iid));

    // 步骤编号按实际执行顺序递增（同域名重绑会跳过“移除旧站点”，编号不能跳）
    let mut step = 1usize;

    // ---- 1. DNS ----
    info(
        iid,
        &format!("{} 检查 DNS 解析（域名需指向本机公网 IP）…", mark(step)),
    );
    let (dns_ok, dns_msg) = caddy::check_dns(&domain).await;
    if !dns_ok {
        err(iid, &format!("DNS 校验未通过：{dns_msg}"));
        finish(
            iid,
            false,
            "绑定失败：域名未正确解析到本机，改好 A 记录后重试",
        );
        return;
    }
    ok(iid, &format!("DNS 校验通过：{dns_msg}"));

    // ---- 2. 旧站点 ----
    let old = inst.domain.clone().unwrap_or_default();
    if !old.is_empty() && old != domain {
        step += 1;
        info(
            iid,
            &format!("{} 移除旧域名 {old} 的站点配置 …", mark(step)),
        );
        match caddy::remove_site(iid).await {
            Ok(()) => ok(iid, "旧站点配置已移除"),
            Err(e) => warn(iid, &format!("旧站点清理失败（继续执行）：{}", e.detail)),
        }
    }

    // ---- 3. 落库 + 写站点 ----
    step += 1;
    info(
        iid,
        &format!(
            "{} 写入 Caddy 站点配置（反向代理 → 127.0.0.1 实例端口）…",
            mark(step)
        ),
    );
    let sql_update = sqlx::query("UPDATE instances SET domain = ? WHERE id = ?")
        .bind(&domain)
        .bind(iid)
        .execute(&state.db)
        .await;
    if let Err(e) = sql_update {
        err(iid, &format!("写入数据库失败：{e}"));
        finish(iid, false, "绑定失败：数据库写入异常");
        return;
    }
    let mut inst2 = inst.clone();
    inst2.domain = Some(domain.clone());
    match crate::services::site::rebuild(&state, &inst2).await {
        Ok(()) => {
            ok(
                iid,
                &format!(
                    "站点配置已生效：{}（耗时 {} ms）",
                    caddy::site_file(iid).display(),
                    t0.elapsed().as_millis()
                ),
            );
            // 操作记录：绑定即生效（证书随后签发，失败会另记一条日志）
            let _ = crate::services::instance::log_op(
                &state.db,
                iid,
                "domain_bind",
                &format!("{domain}（{dns_msg}）"),
            )
            .await;
        }
        Err(e) => {
            // 回滚域名，避免“显示已绑定但没有站点”
            let rollback = sqlx::query("UPDATE instances SET domain = ? WHERE id = ?")
                .bind(if old.is_empty() { None } else { Some(old.clone()) })
                .bind(iid)
                .execute(&state.db)
                .await;
            let _ = rollback;
            err(iid, &format!("写入 Caddy 配置失败：{}", e.detail));
            finish(iid, false, "绑定失败：Caddy 配置写入/重载异常");
            return;
        }
    }

    step += 1;
    info(
        iid,
        &format!(
            "{} 已提交证书申请，等待 Let's Encrypt 签发（通常 5~30 秒）…",
            mark(step)
        ),
    );

    // ---- 4. 等签发 ----
    let mut last_report = 0u64;
    let mut said_no_cert_yet = false;
    loop {
        tokio::time::sleep(Duration::from_secs(TICK_SECS)).await;
        if superseded(iid, &domain) {
            info(iid, "检测到新的绑定请求，本轮流程结束");
            return;
        }

        // 4a. 搬运 Caddy 自己的日志
        for line in caddy::log_since(epoch).await.lines() {
            let l = line.trim();
            if l.is_empty() {
                continue;
            }
            if l.contains(&domain) || l.contains("certificate obtained") || l.contains("obtain") {
                push_caddy(iid, l);
            }
        }

        // 4b. HTTPS 探测（TLS 校验通过才算数）
        if let Some(code) = caddy::https_probe(&domain).await {
            if let Some(path) = caddy::find_cert(&domain).await {
                if let Some((issuer, not_after)) = caddy::cert_meta(&path).await {
                    ok(
                        iid,
                        &format!("证书信息：签发者 {issuer}，有效期至 {not_after}"),
                    );
                }
            }
            ok(iid, &format!("HTTPS 探测通过：https://{domain}/ → HTTP {code}"));
            finish(
                iid,
                true,
                &format!("绑定完成，用时 {} 秒，https 可直接访问", t0.elapsed().as_secs()),
            );
            return;
        }

        // 4c. 证书刚落盘但本机回环不通（NAT hairpin / 容器网络）也认为签发成功
        if let Some(path) = caddy::find_cert(&domain).await {
            let fresh = tokio::fs::metadata(&path)
                .await
                .ok()
                .and_then(|m| m.modified().ok())
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs() as i64 >= epoch - 5)
                .unwrap_or(false);
            if fresh {
                let note = caddy::cert_meta(&path)
                    .await
                    .map(|(i, n)| format!("签发者 {i}，有效期至 {n}"))
                    .unwrap_or_else(|| "证书文件已生成".to_string());
                ok(iid, &format!("证书已签发：{note}"));
                warn(iid, "本机 HTTPS 回环探测未通过（NAT 回环或网络策略），请用浏览器访问确认");
                finish(iid, true, "证书签发成功（本机探测受网络限制）");
                return;
            }
        }

        // 4d. 阶段提示
        let el = t0.elapsed().as_secs();
        if el >= last_report + 15 {
            last_report = el;
            if el >= 15 {
                info(iid, &format!("…仍在等待签发（已 {el} 秒），可继续留在此页"));
            }
        }
        if !said_no_cert_yet && el >= 30 {
            said_no_cert_yet = true;
            warn(
                iid,
                "超过 30 秒仍未拿到证书：常见原因是 80/443 未放行，或域名解析到了别的机器",
            );
        }
        if el >= WAIT_SECS {
            warn(iid, "已等待 120 秒，本轮超时（Caddy 会在后台继续申请并自动续期）");
            finish(
                iid,
                false,
                "证书尚未签发完成：请检查 80/443 是否放行、DNS 是否指向本机；稍后刷新本页查看",
            );
            return;
        }
    }
}

/// 供 `panel_get_domain` 使用的证书摘要。
pub async fn cert_summary(domain: &str) -> Value {
    if domain.is_empty() {
        return json!(null);
    }
    match caddy::find_cert(domain).await {
        None => json!({"exists": false}),
        Some(p) => {
            let (issuer, not_after) = caddy::cert_meta(&p).await.unwrap_or_default();
            json!({
                "exists": true,
                "path": p.to_string_lossy(),
                "issuer": issuer,
                "not_after": not_after,
            })
        }
    }
}
