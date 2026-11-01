//! SQLite 初始化：建表 + 存量库补列 + Agent 占位账号（与 Python 版行为一致）。

use std::str::FromStr;
use std::time::Duration;

use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous};
use sqlx::SqlitePool;

use crate::config::settings;
use crate::error::{AppError, AppResult};
use crate::models::utcnow;

pub async fn init() -> AppResult<SqlitePool> {
    let raw = settings().db_url.clone();
    let path = raw.strip_prefix("sqlite:///").unwrap_or(&raw).to_string();
    if let Some(parent) = std::path::Path::new(&path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)
                .map_err(|e| AppError::internal(format!("创建数据目录失败：{e}")))?;
        }
    }
    let opts = SqliteConnectOptions::from_str(&path)
        .map_err(|e| AppError::internal(format!("数据库配置错误：{e}")))?
        .create_if_missing(true)
        // 采样线程与 API 请求并发写同一个库：delete 模式下彼此等锁，
        // 实测出现过 2.3s 的慢 INSERT（主控侧踩过同样的坑，切 WAL 后消失）。
        .journal_mode(SqliteJournalMode::Wal)
        .synchronous(SqliteSynchronous::Normal)
        .busy_timeout(Duration::from_secs(5));
    let pool = SqlitePoolOptions::new()
        .max_connections(8)
        .connect_with(opts)
        .await?;

    // 显式打印真实落盘路径：DB_URL 少一个斜杠（绝对路径要写 sqlite:////opt/...）时，
    // 这里会被当成相对路径新建一个空库，外部表现是「实例全部消失」，有这一行才能立刻看出问题
    match std::fs::canonicalize(&path) {
        Ok(abs) => eprintln!("[agent] 数据库：{}", abs.display()),
        Err(_) => eprintln!("[agent] 数据库：{path}（未能解析为绝对路径）"),
    }

    for ddl in DDL {
        sqlx::query(ddl).execute(&pool).await.ok();
    }
    // 轻量迁移：老库补列（CREATE TABLE IF NOT EXISTS 不会给已有表加列）
    for ddl in ALTERS {
        sqlx::query(ddl).execute(&pool).await.ok();
    }
    Ok(pool)
}

pub async fn ensure_agent_user(pool: &SqlitePool) -> AppResult<()> {
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM users")
        .fetch_one(pool)
        .await?;
    if count == 0 {
        let hash = crate::auth::hash_password(&random_hex(32));
        sqlx::query(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
        )
        .bind("__agent__")
        .bind(hash)
        .bind("user")
        .bind(utcnow())
        .execute(pool)
        .await?;
    }
    Ok(())
}

/// 独立面板本地管理员引导：
/// - 配置了 AGENT_ADMIN_PASSWORD → 用它（已存在则更新哈希，支持改口令）；
/// - 未配置且不存在 → 生成随机口令存库（bcrypt），并把原文打印到日志一次（journald 可查）。
/// 登录入口见 routers/panel_admin.rs 的 POST /panel-admin/login。
pub async fn ensure_local_admin(pool: &SqlitePool) -> AppResult<()> {
    let configured = settings().local_admin_password.trim().to_string();
    let existing: Option<(i64,)> =
        sqlx::query_as("SELECT id FROM users WHERE username = '__local_admin__'")
            .fetch_optional(pool)
            .await?;
    match (configured.is_empty(), existing) {
        // 未配置且已有 → 保持不变
        (true, Some(_)) => {}
        // 未配置且没有 → 自动生成
        (true, None) => {
            let raw = random_hex(24);
            let hash = crate::auth::hash_password(&raw);
            sqlx::query(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
            )
            .bind("__local_admin__")
            .bind(hash)
            .bind("admin")
            .bind(utcnow())
            .execute(pool)
            .await?;
            eprintln!(
                "[独立面板] 本地管理员口令（首次自动生成，请立即保存；也可用环境变量 AGENT_ADMIN_PASSWORD 固定）：{raw}"
            );
        }
        // 已配置 → 写入/更新
        (false, _) => {
            let hash = crate::auth::hash_password(&configured);
            if let Some((id,)) = existing {
                sqlx::query("UPDATE users SET password_hash = ?, role = 'admin' WHERE id = ?")
                    .bind(hash)
                    .bind(id)
                    .execute(pool)
                    .await?;
            } else {
                sqlx::query(
                    "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
                )
                .bind("__local_admin__")
                .bind(hash)
                .bind("admin")
                .bind(utcnow())
                .execute(pool)
                .await?;
            }
            eprintln!("[独立面板] 本地管理员口令已按 AGENT_ADMIN_PASSWORD 设置生效");
        }
    }
    Ok(())
}

pub fn random_hex(n: usize) -> String {
    use rand::Rng;
    let mut rng = rand::thread_rng();
    let mut out = String::with_capacity(n);
    for _ in 0..n {
        out.push(char::from_digit(rng.gen_range(0..16), 16).unwrap());
    }
    out
}

const DDL: &[&str] = &[
    r#"CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username VARCHAR(64) NOT NULL UNIQUE,
        password_hash VARCHAR(128) NOT NULL,
        role VARCHAR(16) NOT NULL DEFAULT 'user',
        created_at DATETIME
    )"#,
    r#"CREATE TABLE IF NOT EXISTS instances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        name VARCHAR(64) NOT NULL,
        image VARCHAR(128) NOT NULL,
        start_cmd VARCHAR(512) NOT NULL DEFAULT '',
        ext_port INTEGER NOT NULL,
        cpu_limit FLOAT NOT NULL DEFAULT 1.0,
        mem_limit INTEGER NOT NULL DEFAULT 512,
        disk_quota INTEGER NOT NULL DEFAULT 2048,
        note VARCHAR(255) NOT NULL DEFAULT '',
        status VARCHAR(16) NOT NULL DEFAULT 'creating',
        container_id VARCHAR(64),
        host_dir VARCHAR(255) NOT NULL DEFAULT '',
        panel_token VARCHAR(64),
        domain VARCHAR(255),
        expire_at DATETIME,
        owner_ref VARCHAR(128) NOT NULL DEFAULT '',
        disabled_funcs VARCHAR(512) NOT NULL DEFAULT '',
        traffic_gb FLOAT,
        traffic_used_mb FLOAT NOT NULL DEFAULT 0,
        traffic_last_mb FLOAT NOT NULL DEFAULT 0,
        traffic_month VARCHAR(7) NOT NULL DEFAULT '',
        created_at DATETIME
    )"#,
    r#"CREATE TABLE IF NOT EXISTS agent_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_id INTEGER NOT NULL,
        ts DATETIME,
        cpu_percent FLOAT NOT NULL DEFAULT 0,
        mem_used_mb FLOAT NOT NULL DEFAULT 0,
        mem_limit_mb FLOAT NOT NULL DEFAULT 0,
        net_rx_mb FLOAT NOT NULL DEFAULT 0,
        net_tx_mb FLOAT NOT NULL DEFAULT 0
    )"#,
    r#"CREATE TABLE IF NOT EXISTS agent_op_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_id INTEGER NOT NULL,
        action VARCHAR(32) NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        created_at DATETIME
    )"#,
    r#"CREATE TABLE IF NOT EXISTS mysql_services (
        version VARCHAR(16) PRIMARY KEY,
        container_id VARCHAR(64) NOT NULL DEFAULT '',
        root_password VARCHAR(64) NOT NULL DEFAULT '',
        host_port INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME
    )"#,
    r#"CREATE TABLE IF NOT EXISTS instance_dbs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_id INTEGER NOT NULL UNIQUE,
        version VARCHAR(16) NOT NULL,
        db_name VARCHAR(64) NOT NULL,
        db_user VARCHAR(64) NOT NULL,
        db_password VARCHAR(64) NOT NULL,
        created_at DATETIME
    )"#,
    r#"CREATE TABLE IF NOT EXISTS instance_crons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_id INTEGER NOT NULL,
        schedule VARCHAR(64) NOT NULL,
        command VARCHAR(512) NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_run DATETIME,
        last_status VARCHAR(16) NOT NULL DEFAULT '',
        last_output TEXT NOT NULL DEFAULT '',
        created_at DATETIME
    )"#,
    r#"CREATE TABLE IF NOT EXISTS host_backup_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule VARCHAR(64) NOT NULL,
        kind VARCHAR(16) NOT NULL,
        target VARCHAR(128) NOT NULL,
        dir_path VARCHAR(255) NOT NULL DEFAULT '',
        table_name VARCHAR(64) NOT NULL DEFAULT '',
        keep INTEGER NOT NULL DEFAULT 5,
        dest VARCHAR(8) NOT NULL DEFAULT 'local',
        enabled INTEGER NOT NULL DEFAULT 1,
        last_run DATETIME,
        last_status VARCHAR(16) NOT NULL DEFAULT '',
        last_output TEXT NOT NULL DEFAULT '',
        created_at DATETIME
    )"#,
    r#"CREATE TABLE IF NOT EXISTS cos_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        secret_id VARCHAR(128) NOT NULL DEFAULT '',
        secret_key VARCHAR(128) NOT NULL DEFAULT '',
        bucket VARCHAR(128) NOT NULL DEFAULT '',
        region VARCHAR(64) NOT NULL DEFAULT '',
        prefix VARCHAR(128) NOT NULL DEFAULT 'ppanel-backups',
        keep_local INTEGER NOT NULL DEFAULT 1,
        enabled INTEGER NOT NULL DEFAULT 0,
        updated_at DATETIME
    )"#,
    r#"CREATE TABLE IF NOT EXISTS instance_sites (
        instance_id INTEGER PRIMARY KEY,
        mode VARCHAR(16) NOT NULL DEFAULT 'proxy',
        static_dir VARCHAR(255) NOT NULL DEFAULT 'public_html',
        auth_user VARCHAR(64) NOT NULL DEFAULT '',
        auth_hash VARCHAR(128) NOT NULL DEFAULT '',
        ip_whitelist TEXT NOT NULL DEFAULT '',
        ip_blacklist TEXT NOT NULL DEFAULT '',
        updated_at DATETIME
    )"#,
    r#"CREATE TABLE IF NOT EXISTS op_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        instance_id INTEGER,
        action VARCHAR(32) NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        created_at DATETIME
    )"#,
    "CREATE INDEX IF NOT EXISTS ix_instances_user_id ON instances (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_instances_panel_token ON instances (panel_token)",
    "CREATE INDEX IF NOT EXISTS ix_instances_domain ON instances (domain)",
    "CREATE INDEX IF NOT EXISTS ix_agent_metrics_instance_id ON agent_metrics (instance_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_metrics_ts ON agent_metrics (ts)",
];

const ALTERS: &[&str] = &[
    "ALTER TABLE instances ADD COLUMN panel_token VARCHAR(64)",
    "ALTER TABLE instances ADD COLUMN domain VARCHAR(255)",
    "ALTER TABLE instances ADD COLUMN expire_at DATETIME",
    "ALTER TABLE instances ADD COLUMN owner_ref VARCHAR(128) DEFAULT ''",
    "ALTER TABLE instances ADD COLUMN disabled_funcs VARCHAR(512) DEFAULT ''",
    "ALTER TABLE instances ADD COLUMN traffic_gb REAL",
    "ALTER TABLE instances ADD COLUMN traffic_used_mb REAL DEFAULT 0",
    "ALTER TABLE instances ADD COLUMN traffic_last_mb REAL DEFAULT 0",
    "ALTER TABLE instances ADD COLUMN traffic_month VARCHAR(7) DEFAULT ''",
    // 实例来源：master=主控商城开通（默认），local=独立面板本地创建
    "ALTER TABLE instances ADD COLUMN source VARCHAR(16) DEFAULT 'master'",
    "ALTER TABLE host_backup_jobs ADD COLUMN dest VARCHAR(8) DEFAULT 'local'",
];
