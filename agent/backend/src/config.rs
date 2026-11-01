//! 全局配置：环境变量 / backend/.env 覆盖，均有安全默认值（与 Python 版一一对应）。

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

pub struct Settings {
    // 面板监听
    pub host: String,
    pub port: u16,
    pub agent_host: String,
    pub agent_port: u16,

    // 认证
    pub jwt_secret: String,
    pub jwt_expire_minutes: i64,
    pub api_key: String,
    pub node_token: String,

    // 首次启动管理员
    pub admin_username: String,
    pub admin_password: String,
    /// 独立面板本地管理员口令（AGENT_ADMIN_PASSWORD）；空 = 首次启动自动生成
    pub local_admin_password: String,

    // 存储
    pub data_root: String,
    pub db_url: String,
    pub docker_volume_root: String,

    // 开放开通面
    pub open_api_key: String,
    pub panel_public_url: String,
    pub webhook_url: String,
    pub webhook_warn_days: i64,
    pub expired_panel_mode: String,

    // 实例参数
    pub port_start: u16,
    pub port_end: u16,
    pub inner_app_port: u16,
    pub upload_limit_mb: i64,
    pub pids_limit: i64,
    pub docker_network: String,
    pub python_images: Vec<String>,
    pub php_images: Vec<String>,
    pub node_images: Vec<String>,
    pub go_images: Vec<String>,
    pub php_disable_functions: String,

    // 容器名前缀（被控可用 CONTAINER_PREFIX 覆盖，如 pagent-）
    pub container_prefix: String,
    // 容器加固开关（no-new-privileges / init / cap_drop）
    pub container_hardening: bool,
}

impl Settings {
    pub fn all_images(&self) -> Vec<String> {
        let mut v = Vec::new();
        v.extend(self.python_images.iter().cloned());
        v.extend(self.php_images.iter().cloned());
        v.extend(self.node_images.iter().cloned());
        v.extend(self.go_images.iter().cloned());
        v
    }
}

fn env(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

fn env_int(key: &str, default: i64) -> i64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.trim().parse::<i64>().ok())
        .unwrap_or(default)
}

/// 列表配置：JSON 数组优先，其次逗号分隔。
fn env_list(key: &str, default: &[&str]) -> Vec<String> {
    match std::env::var(key) {
        Ok(raw) if !raw.trim().is_empty() => {
            if let Ok(serde_json::Value::Array(arr)) = serde_json::from_str::<serde_json::Value>(&raw) {
                return arr
                    .into_iter()
                    .map(|x| x.as_str().unwrap_or_default().to_string())
                    .collect();
            }
            raw.split(',').map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).collect()
        }
        _ => default.iter().map(|s| s.to_string()).collect(),
    }
}

/// 定位 backend/.env：优先 PPANEL_ENV_FILE，其次当前目录及上级目录。
fn env_file() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("PPANEL_ENV_FILE") {
        let p = PathBuf::from(p);
        if p.is_file() {
            return Some(p);
        }
    }
    let mut dir = std::env::current_dir().ok()?;
    for _ in 0..4 {
        let f = dir.join(".env");
        if f.is_file() {
            return Some(f);
        }
        if let Some(parent) = dir.parent() {
            dir = parent.to_path_buf();
        } else {
            break;
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        let mut d: Option<&Path> = exe.parent();
        while let Some(cur) = d {
            let f = cur.join(".env");
            if f.is_file() {
                return Some(f);
            }
            d = cur.parent();
        }
    }
    None
}

impl Settings {
    fn from_env() -> Self {
        if let Some(f) = env_file() {
            let _ = dotenvy::from_path(f);
        }
        Settings {
            host: env("PANEL_HOST", "127.0.0.1"),
            port: env_int("PANEL_PORT", 8000) as u16,
            agent_host: env("AGENT_HOST", "0.0.0.0"),
            agent_port: env_int("AGENT_PORT", 9100) as u16,
            jwt_secret: env("JWT_SECRET", "ppanel-dev-secret-change-me-to-a-long-random-string"),
            jwt_expire_minutes: env_int("JWT_EXPIRE_MINUTES", 720),
            api_key: env("PANEL_API_KEY", ""),
            node_token: env("NODE_TOKEN", ""),
            admin_username: env("ADMIN_USERNAME", "admin"),
            admin_password: env("ADMIN_PASSWORD", "admin123"),
            local_admin_password: env("AGENT_ADMIN_PASSWORD", ""),
            data_root: env("DATA_ROOT", "/data/inst"),
            db_url: env("DB_URL", "sqlite:///./data/ppanel.db"),
            docker_volume_root: env("DOCKER_VOLUME_ROOT", ""),
            open_api_key: env("OPEN_API_KEY", ""),
            panel_public_url: env("PANEL_PUBLIC_URL", ""),
            webhook_url: env("WEBHOOK_URL", ""),
            webhook_warn_days: env_int("WEBHOOK_WARN_DAYS", 3),
            expired_panel_mode: env("EXPIRED_PANEL_MODE", "readonly"),
            port_start: env_int("PORT_START", 30000) as u16,
            port_end: env_int("PORT_END", 39999) as u16,
            inner_app_port: env_int("INNER_APP_PORT", 8000) as u16,
            upload_limit_mb: env_int("UPLOAD_LIMIT_MB", 50),
            pids_limit: env_int("PIDS_LIMIT", 256),
            docker_network: env("DOCKER_NETWORK", "ppanel-net"),
            python_images: env_list(
                "PYTHON_IMAGES",
                &["python:3.9-slim", "python:3.10-slim", "python:3.11-slim", "python:3.12-slim"],
            ),
            php_images: env_list("PHP_IMAGES", &["php:8.2-cli", "php:8.3-cli", "php:8.4-cli"]),
            node_images: env_list("NODE_IMAGES", &["node:20-slim", "node:22-slim"]),
            go_images: env_list("GO_IMAGES", &["golang:1.23-alpine", "golang:1.24-alpine"]),
            php_disable_functions: env(
                "PHP_DISABLE_FUNCTIONS",
                "exec,shell_exec,system,passthru,proc_open,popen,pcntl_exec,pcntl_fork,\
                 pcntl_waitpid,show_source,dl,syslog,ini_alter,ini_restore,chroot,chgrp,chown",
            ),
            container_prefix: env("CONTAINER_PREFIX", "ppanel-"),
            // 容器加固：no-new-privileges + init(tini) + 去危险 capability；0 关闭（排障用）
            container_hardening: env("CONTAINER_HARDENING", "1") != "0",
        }
    }
}

static SETTINGS: OnceLock<Settings> = OnceLock::new();

pub fn init() -> &'static Settings {
    SETTINGS.get_or_init(Settings::from_env)
}

pub fn settings() -> &'static Settings {
    SETTINGS.get_or_init(Settings::from_env)
}
