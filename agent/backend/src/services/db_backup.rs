//! 本机 SQLite 自备份：每日 `VACUUM INTO` 生成一致性快照，保留最近 7 份。
//!
//! 被控的实例登记、面板会话、操作日志都在 agent.db 里；磁盘损坏或误删时
//! 靠这份快照兜底。VACUUM INTO 在 WAL 模式下也能产出一致的完整副本，
//! 快照文件落在备份目录（/opt/ppanel/backups，可被 PPANEL_BACKUP_DIR 覆盖）。

use std::time::Duration;

use crate::error::{AppError, AppResult};
use crate::services::backup;
use crate::state::AppState;

const KEEP: usize = 7;
const TAG: &str = "agentdb";
const EXT: &str = ".db";

pub async fn run_loop(state: AppState) {
    // 启动后稍等（等建表迁移完成）先做一次，之后每天一次
    tokio::time::sleep(Duration::from_secs(30)).await;
    loop {
        if let Err(e) = once(&state).await {
            eprintln!("[agent] SQLite 自备份失败：{}", e.detail);
        }
        tokio::time::sleep(Duration::from_secs(24 * 3600)).await;
    }
}

async fn once(state: &AppState) -> AppResult<()> {
    let root = backup::backup_root();
    tokio::fs::create_dir_all(&root)
        .await
        .map_err(|e| AppError::bad_gateway(format!("备份目录创建失败：{e}")))?;
    let name = format!("{}_{}{}", TAG, backup::ts(), EXT);
    let dest = format!("{}/{}", root.trim_end_matches('/'), name);
    let sql = format!("VACUUM INTO '{}'", dest);
    sqlx::query(&sql)
        .execute(&state.db)
        .await
        .map_err(|e| AppError::bad_gateway(format!("VACUUM INTO 失败：{e}")))?;
    backup::cleanup_snapshots(TAG, EXT, KEEP);
    println!("[agent] SQLite 自备份完成：{name}");
    Ok(())
}
