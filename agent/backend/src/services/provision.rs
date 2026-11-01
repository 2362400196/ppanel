//! 实例开通/回收内核：被 /agent/instances、/open/*、独立面板共用，保证三条路径行为一致。

use chrono::NaiveDateTime;
use sqlx::SqlitePool;

use crate::auth::new_panel_token;
use crate::error::{AppError, AppResult};
use crate::models::Instance;
use crate::services::{file, instance};
use crate::state::AppState;

#[derive(Debug, Clone, Default)]
pub struct ProvisionReq {
    pub user_id: i64,
    pub name: String,
    pub image: String,
    pub start_cmd: String,
    pub cpu: f64,
    pub mem: i64,
    pub disk_quota: i64,
    pub note: String,
    pub owner_ref: String,
    pub expire_at: Option<NaiveDateTime>,
    pub traffic_gb: Option<f64>,
    pub issue_panel_token: bool,
}

/// 开通：登记 → 建目录 + 入口文件 → 建容器（失败回滚登记与目录）。
pub async fn create_instance(state: &AppState, req: ProvisionReq, action: &str) -> AppResult<Instance> {
    if req.name.trim().is_empty() {
        return Err(AppError::bad_request("实例名称不能为空"));
    }
    let allowed = instance::allowed_images(state).await;
    if !allowed.contains(&req.image) {
        return Err(AppError::bad_request("不支持的镜像版本"));
    }
    instance::ensure_image(state, &req.image).await?;
    let port = instance::alloc_port(&state.db).await?;

    let start_cmd = if req.start_cmd.trim().is_empty() {
        instance::default_start_cmd(&req.image, Some(req.mem))
    } else {
        req.start_cmd.trim().to_string()
    };
    let token = if req.issue_panel_token { Some(new_panel_token()) } else { None };
    let now = crate::models::utcnow();

    let id: i64 = sqlx::query_scalar(
        "INSERT INTO instances (user_id, name, image, start_cmd, ext_port, cpu_limit, mem_limit, disk_quota, \
         note, status, container_id, host_dir, panel_token, domain, expire_at, owner_ref, disabled_funcs, \
         traffic_gb, traffic_used_mb, traffic_last_mb, traffic_month, created_at) \
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
    )
    .bind(req.user_id)
    .bind(req.name.trim())
    .bind(&req.image)
    .bind(&start_cmd)
    .bind(port as i64)
    .bind(req.cpu)
    .bind(req.mem)
    .bind(req.disk_quota)
    .bind(&req.note)
    .bind("creating")
    .bind::<Option<String>>(None)
    .bind("")
    .bind(token.as_deref())
    .bind::<Option<String>>(None)
    .bind(req.expire_at)
    .bind(&req.owner_ref)
    .bind("")
    .bind(req.traffic_gb)
    .bind(0.0f64)
    .bind(0.0f64)
    .bind("")
    .bind(now)
    .fetch_one(&state.db)
    .await?;

    let host_dir = instance::host_dir_for(id);
    let mut inst = instance::fetch_instance(&state.db, id).await?;
    inst.host_dir = host_dir.clone();
    let _ = sqlx::query("UPDATE instances SET host_dir = ? WHERE id = ?")
        .bind(&host_dir)
        .bind(id)
        .execute(&state.db)
        .await;

    let res: AppResult<()> = async {
        std::fs::create_dir_all(&host_dir)?;
        instance::ensure_entry_file(&host_dir, &inst.image)?;
        let cid = instance::create_container(state, &inst).await?;
        inst.container_id = Some(cid);
        inst.status = "created".to_string();
        // 建完立刻启动：开通即可用。原先只建不启，用户新开的机器是停着的、端口不通，
        // 还以为实例坏了（商城购买同样受影响）。启动失败不整体回滚——容器与数据保留，
        // 状态按真实情况落库，用户看日志就能定位。
        if let Err(e) = instance::start_instance(state, &mut inst).await {
            eprintln!("[provision] 实例 #{id} 自动启动失败：{}", e.detail);
            let _ = instance::sync_status(state, &mut inst).await;
        }
        sqlx::query("UPDATE instances SET container_id = ?, status = ? WHERE id = ?")
            .bind(inst.container_id.as_deref().unwrap_or(""))
            .bind(&inst.status)
            .bind(id)
            .execute(&state.db)
            .await?;
        Ok(())
    }
    .await;

    match res {
        Ok(_) => {
            let _ = instance::log_op(&state.db, id, action, &format!("开通 owner_ref={}", inst.owner_ref)).await;
            Ok(inst)
        }
        Err(e) => {
            rollback(state, &inst).await;
            Err(match e.status.as_u16() {
                400 | 404 | 409 => e,
                _ => AppError::bad_gateway(format!("创建实例失败：{}", e.detail)),
            })
        }
    }
}

/// 失败回滚：删登记 + 删目录（尽力而为）。
pub async fn rollback(state: &AppState, inst: &Instance) {
    let _ = sqlx::query("DELETE FROM instances WHERE id = ?")
        .bind(inst.id)
        .execute(&state.db)
        .await;
    if !inst.host_dir.is_empty() {
        let _ = std::fs::remove_dir_all(&inst.host_dir);
    }
}

/// 回收：快照（purge 时，失败即中止）→ 删容器 → 联动删实例库 → 删登记 →（可选）删目录。
pub async fn delete_instance_full(state: &AppState, inst: &Instance, purge: bool) -> AppResult<serde_json::Value> {
    if purge && !inst.host_dir.is_empty() {
        // 安全网：删除前先快照数据目录，失败则整体中止（什么都不删，可重试）
        instance::snapshot_host_dir(inst).await?;
    }
    if let Err(e) = crate::services::mysql::drop_instance_db(&state.db, inst.id).await {
        println!("[agent] 回收实例数据库失败（忽略）：{}", e.detail);
    }
    instance::remove_container(state, inst).await?;
    sqlx::query("DELETE FROM instances WHERE id = ?")
        .bind(inst.id)
        .execute(&state.db)
        .await?;
    if purge && !inst.host_dir.is_empty() {
        let _ = std::fs::remove_dir_all(&inst.host_dir);
    }
    Ok(serde_json::json!({"detail": "实例已回收", "host_dir": inst.host_dir, "purged": purge && !inst.host_dir.is_empty()}))
}

pub async fn reset_panel_token(pool: &SqlitePool, iid: i64) -> AppResult<String> {
    let token = new_panel_token();
    sqlx::query("UPDATE instances SET panel_token = ? WHERE id = ?")
        .bind(&token)
        .bind(iid)
        .execute(pool)
        .await?;
    Ok(token)
}

/// 备份打包实例目录 → 临时 tar.gz（面板/宿主机备份共用）。
pub async fn backup_dir(inst: &Instance) -> AppResult<std::path::PathBuf> {
    file::create_backup(&inst.host_dir)
}
