//! 定时任务调度：实例定时任务（instance_crons）+ 宿主机备份任务（host_backup_jobs）
//! + 过期 phpMyAdmin 临时容器清理。
//!
//! 对应 Python 版 app/services/cron_service.py（每 20s 唤醒）与
//! app/services/host_backup_service.py 的 tick_all（同一循环内触发）。
//! 本移植版由主流程 spawn `run_loop`，每 30s 轮询一次：
//!   1) 实例任务：cron 命中且本分钟未触发 → docker exec 执行并回写 last_run/status/output
//!   2) 宿主机备份任务：cron 命中且本分钟未触发 → 复用 backup::run_job 执行（含 COS 联动）
//!   3) pma 清理：label ppanel.pma-expire 已过期的临时容器强删，并删除对应临时账号
//!
//! cron 表达式：5 段（分 时 日 月 周）。命中判断以 Python `cron_match` 语义为准
//! （dow 0=周日）；同时用 cron crate 转换后做一次 `Schedule::includes` 判定，
//! 转换不可用时回退到手写字段匹配，保证与 Python 结果一致。
//! 所有等待均为 async sleep，不占用线程池。

use std::str::FromStr;
use std::time::Duration;

use chrono::{Datelike, NaiveDateTime, Timelike};

use crate::models::{utcnow, Instance, InstanceCron, MySqlService};
use crate::services::{backup, instance, mysql};
use crate::state::AppState;

/// 轮询间隔（Python 为 20s；Rust 版按任务要求 30s）。
pub const SCHED_WAIT_SECS: u64 = 30;

// ---------- 字段匹配（严格照抄 Python _field_hit） ----------

/// 单字段命中：支持 `*`、`*/n`、`a-b`、`a,b`、`a-b/n`；非法返回 false。
pub fn field_hit(expr: &str, val: i64, lo: i64, hi: i64) -> bool {
    let expr = expr.trim();
    if expr == "*" {
        return true;
    }
    let mut ok = false;
    for part in expr.split(',') {
        let mut step: i64 = 1;
        let mut part = part;
        if let Some((head, step_s)) = part.split_once('/') {
            part = head;
            step = match step_s.trim().parse::<i64>() {
                Ok(v) => v.max(1),
                Err(_) => return false,
            };
        }
        let (a, b) = if part == "*" {
            (lo, hi)
        } else if let Some((a_s, b_s)) = part.split_once('-') {
            match (a_s.trim().parse::<i64>(), b_s.trim().parse::<i64>()) {
                (Ok(a), Ok(b)) => (a, b),
                _ => return false,
            }
        } else {
            match part.trim().parse::<i64>() {
                Ok(a) => (a, a),
                Err(_) => return false,
            }
        };
        if a <= val && val <= b && (val - a) % step == 0 {
            ok = true;
        }
    }
    ok
}

/// 5 段表达式匹配（非法表达式返回 false）。
pub fn cron_match(schedule: &str, now: NaiveDateTime) -> bool {
    let fields: Vec<&str> = schedule.split_whitespace().collect();
    if fields.len() != 5 {
        return false;
    }
    let dow = now.weekday().num_days_from_sunday() as i64; // 0=周日
    field_hit(fields[0], now.minute() as i64, 0, 59)
        && field_hit(fields[1], now.hour() as i64, 0, 23)
        && field_hit(fields[2], now.day() as i64, 1, 31)
        && field_hit(fields[3], now.month() as i64, 1, 12)
        && field_hit(fields[4], dow, 0, 6)
}

/// 把 Python 5 段表达式的「周字段」翻译成 cron crate 的 6 段（1=周日…7=周六）。
/// 无法安全翻译时返回 None（调用方回退到手写匹配）。
fn crate_spec(schedule: &str) -> Option<String> {
    let fields: Vec<&str> = schedule.split_whitespace().collect();
    if fields.len() != 5 {
        return None;
    }
    let map_dow = |n: i64| -> Option<i64> {
        match n {
            0 | 7 => Some(1),
            1..=6 => Some(n + 1),
            _ => None,
        }
    };
    let mut dow_parts: Vec<String> = Vec::new();
    for part in fields[4].split(',') {
        let (base, step) = match part.split_once('/') {
            Some((b, s)) => (b, Some(s)),
            None => (part, None),
        };
        let base_out = if base.trim() == "*" {
            "*".to_string()
        } else if let Some((a_s, b_s)) = base.split_once('-') {
            let a = map_dow(a_s.trim().parse().ok()?)?;
            let b = map_dow(b_s.trim().parse().ok()?)?;
            if a > b {
                // 交叉范围（如 6-0）无法表达，回退
                return None;
            }
            format!("{a}-{b}")
        } else {
            let a = map_dow(base.trim().parse().ok()?)?;
            a.to_string()
        };
        dow_parts.push(match step {
            Some(s) => format!("{base_out}/{s}"),
            None => base_out,
        });
    }
    let dow = dow_parts.join(",");
    Some(format!(
        "0 {} {} {} {} {} *",
        fields[0], fields[1], fields[2], fields[3], dow
    ))
}

/// 优先用 cron crate 判定；翻译/解析失败时回退到 Python 语义的手写匹配。
pub fn cron_hit(schedule: &str, now: NaiveDateTime) -> bool {
    if let Some(spec) = crate_spec(schedule) {
        if let Ok(sched) = cron::Schedule::from_str(&spec) {
            let dt = chrono::DateTime::<chrono::Utc>::from_naive_utc_and_offset(now, chrono::Utc);
            return sched.includes(dt);
        }
    }
    cron_match(schedule, now)
}

/// 字段级可达性校验（文案与 Python `validate` 完全一致）。
pub fn validate(schedule: &str) -> Option<String> {
    let fields: Vec<&str> = schedule.split_whitespace().collect();
    if fields.len() != 5 {
        return Some("格式须为 5 段：分 时 日 月 周".to_string());
    }
    if !(0..60).any(|m| field_hit(fields[0], m, 0, 59)) {
        return Some("分钟字段在任何分钟都不会触发".to_string());
    }
    if !(0..24).any(|h| field_hit(fields[1], h, 0, 23)) {
        return Some("小时字段在任何小时都不会触发".to_string());
    }
    // 两年内找一天满足 日+月+周 组合（拦「2 月 30 日」这类永不存在的组合）
    let base = utcnow().date();
    for days in 0..731i64 {
        let Some(t) = base.checked_add_signed(chrono::Duration::days(days)) else {
            break;
        };
        if field_hit(fields[2], t.day() as i64, 1, 31)
            && field_hit(fields[3], t.month() as i64, 1, 12)
            && field_hit(fields[4], t.weekday().num_days_from_sunday() as i64, 0, 6)
        {
            return None;
        }
    }
    Some("日/月/周组合在可预见时间内不会出现（例如 2 月 31 日）".to_string())
}

// ---------- 实例定时任务 ----------

fn is_expired(inst: &Instance, now: NaiveDateTime) -> bool {
    matches!(inst.expire_at, Some(t) if now >= t)
}

async fn run_one(state: &AppState, cron_id: i64) {
    let row = sqlx::query_as::<_, InstanceCron>("SELECT * FROM instance_crons WHERE id = ?")
        .bind(cron_id)
        .fetch_optional(&state.db)
        .await
        .ok()
        .flatten();
    let Some(row) = row else { return };
    if row.enabled == 0 {
        return;
    }

    // 各分支必定赋值且只赋一次，无需初始值与 mut（避免多余告警）
    let out;
    let mut status = "fail";
    let cname = instance::container_name(row.instance_id);
    match state.docker.get().await {
        Ok(docker) => match docker.inspect_container(&cname, None).await {
            Ok(info) => {
                let running = info.state.as_ref().and_then(|s| s.running).unwrap_or(false);
                if !running {
                    out = "容器未运行".to_string();
                } else {
                    match backup::exec_demux(&docker, &cname, vec!["sh".into(), "-c".into(), row.command.clone()])
                        .await
                    {
                        Ok((code, stdout, stderr)) => {
                            // Python：成功只取 stdout，失败取 stdout+stderr
                            let text = if code == 0 {
                                stdout
                            } else {
                                format!("{stdout}{stderr}")
                            };
                            out = text.trim().to_string();
                            status = if code == 0 { "ok" } else { "fail" };
                        }
                        Err(e) => out = e.detail.chars().take(500).collect(),
                    }
                }
            }
            Err(e) => out = e.to_string().chars().take(500).collect(),
        },
        Err(e) => out = e.detail.chars().take(500).collect(),
    }

    let output: String = out.chars().take(2000).collect();
    let _ = sqlx::query(
        "UPDATE instance_crons SET last_run = ?, last_status = ?, last_output = ? WHERE id = ?",
    )
    .bind(utcnow())
    .bind(status)
    .bind(&output)
    .bind(cron_id)
    .execute(&state.db)
    .await;
}

/// 一轮实例任务调度（命中且本分钟未触发的任务各自 spawn，不阻塞本轮）。
async fn tick_instance_crons(state: &AppState) -> usize {
    let now = utcnow();
    let minute_start = now
        .with_second(0)
        .and_then(|t| t.with_nanosecond(0))
        .unwrap_or(now);
    let rows: Vec<InstanceCron> = match sqlx::query_as("SELECT * FROM instance_crons WHERE enabled = 1")
        .fetch_all(&state.db)
        .await
    {
        Ok(v) => v,
        Err(_) => return 0,
    };
    let mut started = 0;
    for row in rows {
        if !cron_hit(&row.schedule, now) {
            continue;
        }
        if matches!(row.last_run, Some(t) if t >= minute_start) {
            continue; // 本分钟已触发过
        }
        let inst = sqlx::query_as::<_, Instance>("SELECT * FROM instances WHERE id = ?")
            .bind(row.instance_id)
            .fetch_optional(&state.db)
            .await
            .ok()
            .flatten();
        let Some(inst) = inst else { continue };
        if is_expired(&inst, now) {
            continue;
        }
        let st = state.clone();
        let id = row.id;
        tokio::spawn(async move {
            run_one(&st, id).await;
        });
        started += 1;
    }
    started
}

// ---------- 宿主机备份任务 ----------

/// 一轮备份任务调度（与 Python `tick_all` 一致：命中且本分钟未触发即执行）。
async fn tick_host_backup_jobs(state: &AppState) -> usize {
    let now = utcnow();
    let minute_start = now
        .with_second(0)
        .and_then(|t| t.with_nanosecond(0))
        .unwrap_or(now);
    let rows: Vec<crate::models::HostBackupJob> =
        match sqlx::query_as("SELECT * FROM host_backup_jobs WHERE enabled = 1")
            .fetch_all(&state.db)
            .await
        {
            Ok(v) => v,
            Err(_) => return 0,
        };
    let mut started = 0;
    for job in rows {
        if !cron_hit(&job.schedule, now) {
            continue;
        }
        if matches!(job.last_run, Some(t) if t >= minute_start) {
            continue;
        }
        let st = state.clone();
        let job_id = job.id;
        tokio::spawn(async move {
            // request=None 语义：任务日志 id 传空串，不产生 X-Task-Id 记录
            let _ = backup::run_job(&st, job_id, "").await;
        });
        started += 1;
    }
    started
}

// ---------- 过期 phpMyAdmin 临时容器清理 ----------

async fn cleanup_pma(state: &AppState) {
    let Ok(docker) = state.docker.get().await else {
        return;
    };
    let mut filters: std::collections::HashMap<String, Vec<String>> = Default::default();
    filters.insert(
        "label".to_string(),
        vec!["ppanel.managed=true".to_string()],
    );
    let list = docker
        .list_containers(Some(bollard::container::ListContainersOptions::<String> {
            all: true,
            filters,
            ..Default::default()
        }))
        .await;
    let Ok(list) = list else { return };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    for c in list {
        let labels = c.labels.clone().unwrap_or_default();
        let Some(exp) = labels.get("ppanel.pma-expire") else {
            continue;
        };
        let Ok(exp) = exp.parse::<f64>() else { continue };
        if exp >= now {
            continue;
        }
        let name = c
            .names
            .as_ref()
            .and_then(|n| n.first())
            .map(|n| n.trim_start_matches('/').to_string())
            .unwrap_or_default();
        if name.is_empty() {
            continue;
        }
        let removed = docker
            .remove_container(
                &name,
                Some(bollard::container::RemoveContainerOptions {
                    force: true,
                    ..Default::default()
                }),
            )
            .await;
        if removed.is_ok() {
            if let Some(user) = labels.get("ppanel.pma-user") {
                drop_pma_user(state, user).await;
            }
        }
    }
}

/// 删除 pma 临时账号（遍历所有 MySQL 服务容器，root 凭证取自 mysql_services）。
async fn drop_pma_user(state: &AppState, user: &str) {
    if !crate::services::backup::db_ok(user) {
        return;
    }
    let rows: Vec<MySqlService> = match sqlx::query_as("SELECT * FROM mysql_services")
        .fetch_all(&state.db)
        .await
    {
        Ok(v) => v,
        Err(_) => return,
    };
    for svc in rows {
        let _ = mysql::exec_sql(
            state,
            &svc.version,
            &svc.root_password,
            &format!("DROP USER IF EXISTS '{user}'@'%'"),
        )
        .await;
    }
}

// ---------- 调度主循环 ----------

/// 后台调度循环（主流程 spawn）：实例任务 + 宿主机备份任务 + pma 清理。
pub async fn run_loop(state: AppState) {
    loop {
        let _ = tick_instance_crons(&state).await;
        let _ = tick_host_backup_jobs(&state).await;
        cleanup_pma(&state).await;
        tokio::time::sleep(Duration::from_secs(SCHED_WAIT_SECS)).await;
    }
}

// ---------- 供路由复用的小工具 ----------

/// 当前分钟是否命中该表达式（「立即执行」前的即时校验用）。
pub fn hit_now(schedule: &str) -> bool {
    cron_hit(schedule, utcnow())
}

/// 面板「立即执行」：按任务 ID 异步跑一次，不阻塞请求（结果写回 last_* 字段）。
pub fn spawn_run(state: AppState, cron_id: i64) {
    tokio::spawn(async move {
        run_one(&state, cron_id).await;
    });
}
