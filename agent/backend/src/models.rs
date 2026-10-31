//! 数据模型（与 Python SQLAlchemy 表结构保持完全兼容，可直接复用旧库）。

use chrono::{NaiveDateTime, Utc};
use serde::Serialize;

pub fn utcnow() -> NaiveDateTime {
    Utc::now().naive_utc()
}

/// Python isoformat 风格（无微秒时不带小数位）。
pub fn dt_iso(v: &NaiveDateTime) -> String {
    if v.nanosecond() > 0 {
        v.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
    } else {
        v.format("%Y-%m-%dT%H:%M:%S").to_string()
    }
}

pub fn opt_dt_iso(v: &Option<NaiveDateTime>) -> Option<String> {
    v.as_ref().map(dt_iso)
}

use chrono::Timelike;

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct Instance {
    pub id: i64,
    pub user_id: i64,
    pub name: String,
    pub image: String,
    pub start_cmd: String,
    pub ext_port: i64,
    pub cpu_limit: f64,
    pub mem_limit: i64,
    pub disk_quota: i64,
    pub note: String,
    pub status: String,
    pub container_id: Option<String>,
    pub host_dir: String,
    pub panel_token: Option<String>,
    pub domain: Option<String>,
    pub expire_at: Option<NaiveDateTime>,
    pub owner_ref: String,
    pub disabled_funcs: String,
    pub traffic_gb: Option<f64>,
    pub traffic_used_mb: f64,
    pub traffic_last_mb: f64,
    pub traffic_month: String,
    pub created_at: Option<NaiveDateTime>,
}

impl Instance {
    /// InstanceOut 字段（与 Python Pydantic 输出一致）。
    pub fn out_json(&self) -> serde_json::Value {
        serde_json::json!({
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "image": self.image,
            "start_cmd": self.start_cmd,
            "ext_port": self.ext_port,
            "cpu_limit": self.cpu_limit,
            "mem_limit": self.mem_limit,
            "disk_quota": self.disk_quota,
            "note": self.note,
            "status": self.status,
            "created_at": opt_dt_iso(&self.created_at),
            "expire_at": opt_dt_iso(&self.expire_at),
            "traffic_gb": self.traffic_gb,
            "traffic_used_mb": round1(self.traffic_used_mb),
        })
    }

    pub fn detail_json(&self, started_at: Option<String>) -> serde_json::Value {
        let mut v = self.out_json();
        v["host_dir"] = serde_json::json!(self.host_dir);
        v["started_at"] = serde_json::json!(started_at);
        v
    }
}

pub fn round1(v: f64) -> f64 {
    (v * 10.0).round() / 10.0
}
pub fn round2(v: f64) -> f64 {
    (v * 100.0).round() / 100.0
}

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct User {
    pub id: i64,
    pub username: String,
    pub password_hash: String,
    pub role: String,
    pub created_at: Option<NaiveDateTime>,
}

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct InstanceSite {
    pub instance_id: i64,
    pub mode: String,
    pub static_dir: String,
    pub auth_user: String,
    pub auth_hash: String,
    pub ip_whitelist: String,
    pub ip_blacklist: String,
    pub updated_at: Option<NaiveDateTime>,
}

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct InstanceDb {
    pub id: i64,
    pub instance_id: i64,
    pub version: String,
    pub db_name: String,
    pub db_user: String,
    pub db_password: String,
    pub created_at: Option<NaiveDateTime>,
}

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct MySqlService {
    pub version: String,
    pub container_id: String,
    pub root_password: String,
    pub host_port: i64,
    pub created_at: Option<NaiveDateTime>,
}

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct InstanceCron {
    pub id: i64,
    pub instance_id: i64,
    pub schedule: String,
    pub command: String,
    pub enabled: i64,
    pub last_run: Option<NaiveDateTime>,
    pub last_status: String,
    pub last_output: String,
    pub created_at: Option<NaiveDateTime>,
}

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct HostBackupJob {
    pub id: i64,
    pub schedule: String,
    pub kind: String,
    pub target: String,
    pub dir_path: String,
    pub table_name: String,
    pub keep: i64,
    pub dest: String,
    pub enabled: i64,
    pub last_run: Option<NaiveDateTime>,
    pub last_status: String,
    pub last_output: String,
    pub created_at: Option<NaiveDateTime>,
}

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct CosConfig {
    pub id: i64,
    pub secret_id: String,
    pub secret_key: String,
    pub bucket: String,
    pub region: String,
    pub prefix: String,
    pub keep_local: i64,
    pub enabled: i64,
    pub updated_at: Option<NaiveDateTime>,
}

#[derive(Debug, Clone, serde::Deserialize, Serialize)]
pub struct PortRule {
    pub port: String,
    #[serde(default = "default_proto")]
    pub proto: String,
}

fn default_proto() -> String {
    "tcp".to_string()
}
