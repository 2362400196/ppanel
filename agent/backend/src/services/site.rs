//! 实例站点（域名绑定 / 模式切换 / 访问保护）配置读写。
//!
//! 一条实例最多一个域名：`instance_sites` 存模式（proxy 反代 / static 静态）、
//! basic auth、IP 黑白名单；变更后立即重建 Caddy 配置并重载。
//! 与 Python app/routers/panel_ops.py 的站点段行为一致（面板与被控端共用本模块）。

use regex::Regex;
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::SqlitePool;

use crate::error::{AppError, AppResult};
use crate::models::{dt_iso, Instance, InstanceSite, utcnow};
use crate::services::{caddy, instance};
use crate::state::AppState;

#[derive(Debug, Clone, Deserialize)]
pub struct SiteInput {
    #[serde(default = "d_mode")]
    pub mode: String,
    #[serde(default = "d_public")]
    pub static_dir: String,
    #[serde(default)]
    pub auth_user: String,
    #[serde(default)]
    pub auth_pass: String,
    #[serde(default)]
    pub ip_whitelist: String,
    #[serde(default)]
    pub ip_blacklist: String,
}

fn d_mode() -> String {
    "proxy".to_string()
}

fn d_public() -> String {
    "public_html".to_string()
}

// ---------- 工具 ----------

fn ipv4_re() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$").unwrap())
}

fn ipv6_re() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^([0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(/\d{1,3})?$").unwrap())
}

fn cidr_ok(t: &str) -> bool {
    ipv4_re().is_match(t) || ipv6_re().is_match(t)
}

/// 校验并规范 IP/CIDR 列表（非法输入文案与 Python `_norm_ips` 一致）。
pub fn norm_ips(raw: &str) -> AppResult<String> {
    let toks: Vec<String> = raw
        .replace(',', " ")
        .split_whitespace()
        .map(|t| t.to_string())
        .collect();
    for t in &toks {
        let valid = cidr_ok(t)
            && t.split_once('/')
                .map(|(base, p)| {
                    let max: u32 = if base.contains(':') { 128 } else { 32 };
                    p.parse::<u32>().map(|v| v <= max).unwrap_or(false)
                })
                .unwrap_or(true);
        if !valid {
            return Err(AppError::bad_request(format!("IP/CIDR 无效：{t}")));
        }
    }
    Ok(toks.join(" "))
}

fn default_site(instance_id: i64) -> InstanceSite {
    InstanceSite {
        instance_id,
        mode: "proxy".to_string(),
        static_dir: "public_html".to_string(),
        auth_user: String::new(),
        auth_hash: String::new(),
        ip_whitelist: String::new(),
        ip_blacklist: String::new(),
        updated_at: None,
    }
}

/// 读取站点配置（未设置时返回默认值，不落库）。
pub async fn get_site(pool: &SqlitePool, instance_id: i64) -> AppResult<InstanceSite> {
    Ok(
        sqlx::query_as::<_, InstanceSite>("SELECT * FROM instance_sites WHERE instance_id = ?")
            .bind(instance_id)
            .fetch_optional(pool)
            .await?
            .unwrap_or_else(|| default_site(instance_id)),
    )
}

/// 站点配置 + Caddy 运行状态（面板展示用，字段与 Python `_site_out` 一致）。
pub async fn site_out(state: &AppState, inst: &Instance) -> AppResult<Value> {
    let s = get_site(&state.db, inst.id).await?;
    Ok(json!({
        "mode": s.mode,
        "static_dir": s.static_dir,
        "auth_user": s.auth_user,
        "has_auth": !s.auth_hash.is_empty(),
        "ip_whitelist": s.ip_whitelist,
        "ip_blacklist": s.ip_blacklist,
        "domain": inst.domain.clone().unwrap_or_default(),
        "caddy": caddy::caddy_info().await,
    }))
}

/// 实例数据目录（容器 /app 的宿主机挂载源，静态托管 root）。
pub fn app_root(inst: &Instance) -> String {
    if inst.host_dir.is_empty() {
        instance::host_dir_for(inst.id)
    } else {
        inst.host_dir.clone()
    }
}

/// 从 DB 读配置 → 渲染 Caddyfile → 重载（domain 为空则仅删除旧配置）。
pub async fn rebuild(state: &AppState, inst: &Instance) -> AppResult<()> {
    let Some(domain) = inst.domain.as_ref().filter(|d| !d.trim().is_empty()) else {
        let _ = caddy::remove_site(inst.id).await;
        return Ok(());
    };
    let s = get_site(&state.db, inst.id).await?;
    write_cfg(inst, domain.trim(), &s).await
}

async fn write_cfg(inst: &Instance, domain: &str, s: &InstanceSite) -> AppResult<()> {
    let cfg = caddy::SiteCfg {
        mode: s.mode.clone(),
        static_dir: s.static_dir.clone(),
        app_root: app_root(inst),
        auth_user: s.auth_user.clone(),
        auth_hash: s.auth_hash.clone(),
        ip_whitelist: s.ip_whitelist.clone(),
        ip_blacklist: s.ip_blacklist.clone(),
    };
    caddy::write_site_cfg(inst.id, domain, inst.ext_port, &cfg)
        .await
        .map_err(|e| AppError::bad_gateway(format!("Caddy 配置写入失败：{}", e.detail)))
}

/// 更新站点配置（模式 + 访问保护 + 静态目录），立即生效。
pub async fn upsert_site(state: &AppState, inst: &Instance, input: SiteInput) -> AppResult<Value> {
    let domain = inst.domain.clone().unwrap_or_default();
    if domain.trim().is_empty() {
        return Err(AppError::bad_request("请先在「域名与 SSL」绑定域名"));
    }
    let mode = input.mode.trim().to_string();
    if mode != "proxy" && mode != "static" {
        return Err(AppError::bad_request("mode 仅支持 proxy/static"));
    }
    let sub = input.static_dir.trim().trim_matches('/').to_string();
    if mode == "static" {
        if sub.contains("..") || sub.is_empty() || sub.chars().count() > 200 || sub.contains('/') {
            return Err(AppError::bad_request(
                "静态目录须为 /app 下单级目录名（如 public_html）",
            ));
        }
        let host_dir = std::path::Path::new(&app_root(inst)).join(&sub);
        if !host_dir.is_dir() {
            return Err(AppError::bad_request(format!(
                "目录不存在：容器内 /app/{sub}"
            )));
        }
    }
    let wl = norm_ips(&input.ip_whitelist)?;
    let bl = norm_ips(&input.ip_blacklist)?;
    if !wl.is_empty() && !bl.is_empty() {
        return Err(AppError::bad_request("白名单与黑名单二选一"));
    }

    let mut row = get_site(&state.db, inst.id).await?;
    let exists: bool = sqlx::query_scalar("SELECT COUNT(*) FROM instance_sites WHERE instance_id = ?")
        .bind(inst.id)
        .fetch_one(&state.db)
        .await
        .map(|n: i64| n > 0)?;

    row.mode = mode;
    row.static_dir = if sub.is_empty() {
        "public_html".to_string()
    } else {
        sub
    };
    let au = input.auth_user.trim().to_string();
    if !au.is_empty() && !input.auth_pass.is_empty() {
        row.auth_user = au;
        row.auth_hash = caddy::hash_password(&input.auth_pass)
            .map_err(|e| AppError::bad_gateway(format!("生成密码哈希失败：{}", e.detail)))?;
    } else if au.is_empty() {
        // 清空用户名即关闭密码保护
        row.auth_user = String::new();
        row.auth_hash = String::new();
    } else if row.auth_hash.is_empty() {
        return Err(AppError::bad_request("设置了用户名则必须提供密码"));
    } else {
        // 只改用户名，密码沿用
        row.auth_user = au;
    }
    row.ip_whitelist = wl;
    row.ip_blacklist = bl;
    row.updated_at = Some(utcnow());

    if exists {
        sqlx::query(
            "UPDATE instance_sites SET mode=?, static_dir=?, auth_user=?, auth_hash=?, \
             ip_whitelist=?, ip_blacklist=?, updated_at=? WHERE instance_id=?",
        )
        .bind(&row.mode)
        .bind(&row.static_dir)
        .bind(&row.auth_user)
        .bind(&row.auth_hash)
        .bind(&row.ip_whitelist)
        .bind(&row.ip_blacklist)
        .bind(row.updated_at)
        .bind(inst.id)
        .execute(&state.db)
        .await?;
    } else {
        sqlx::query(
            "INSERT INTO instance_sites (instance_id, mode, static_dir, auth_user, auth_hash, \
             ip_whitelist, ip_blacklist, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        )
        .bind(inst.id)
        .bind(&row.mode)
        .bind(&row.static_dir)
        .bind(&row.auth_user)
        .bind(&row.auth_hash)
        .bind(&row.ip_whitelist)
        .bind(&row.ip_blacklist)
        .bind(row.updated_at)
        .execute(&state.db)
        .await?;
    }

    write_cfg(inst, domain.trim(), &row).await?;
    site_out(state, inst).await
}

/// 删除实例站点：Caddy 配置 + 数据库记录（域名字段由调用方清空）。
pub async fn remove_site_for(state: &AppState, inst: &Instance) -> AppResult<()> {
    let _ = caddy::remove_site(inst.id).await;
    sqlx::query("DELETE FROM instance_sites WHERE instance_id = ?")
        .bind(inst.id)
        .execute(&state.db)
        .await?;
    Ok(())
}

/// 站点配置的最后更新时间（ISO），供概览展示。
pub async fn site_updated_at(state: &AppState, instance_id: i64) -> AppResult<Option<String>> {
    let s = get_site(&state.db, instance_id).await?;
    Ok(s.updated_at.as_ref().map(dt_iso))
}
