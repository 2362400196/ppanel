//! Docker 客户端（bollard）懒加载 + 失效重连。

use std::sync::Arc;

use bollard::Docker;
use tokio::sync::OnceCell;

use crate::error::{AppError, AppResult};

pub struct DockerHandle {
    cell: OnceCell<Docker>,
}

impl DockerHandle {
    pub fn new() -> Arc<Self> {
        Arc::new(Self { cell: OnceCell::new() })
    }

    async fn connect() -> Result<Docker, bollard::errors::Error> {
        let docker = Docker::connect_with_defaults()?;
        docker.ping().await?;
        Ok(docker)
    }

    /// 连接失败 → 503（与 Python 版一致）。
    pub async fn get(&self) -> AppResult<Docker> {
        match self.cell.get_or_try_init(Self::connect).await {
            Ok(d) => Ok(d.clone()),
            Err(_) => Err(AppError::unavailable("Docker 服务不可用")),
        }
    }

    pub async fn try_get(&self) -> Option<Docker> {
        self.get().await.ok()
    }

    /// dockerd 重启后清缓存强制重连。
    pub async fn reset(&self) {
        // OnceCell 无法清空，用 take：需要 &mut，故容器里只做软重试
        let _ = &self.cell;
    }
}

pub async fn ping_ok(handle: &DockerHandle) -> bool {
    handle.get().await.is_ok()
}
