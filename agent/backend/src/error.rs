//! 统一错误：HTTP 状态 + `{"detail": "..."}` 响应体（与 FastAPI 一致）。

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, Clone)]
pub struct AppError {
    pub status: StatusCode,
    pub detail: String,
}

impl AppError {
    pub fn new(status: u16, detail: impl Into<String>) -> Self {
        Self {
            status: StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
            detail: detail.into(),
        }
    }

    pub fn msg(status: StatusCode, detail: impl Into<String>) -> Self {
        Self { status, detail: detail.into() }
    }

    pub fn bad_request(d: impl Into<String>) -> Self {
        Self::new(400, d)
    }
    pub fn unauthorized(d: impl Into<String>) -> Self {
        Self::new(401, d)
    }
    pub fn forbidden(d: impl Into<String>) -> Self {
        Self::new(403, d)
    }
    pub fn not_found(d: impl Into<String>) -> Self {
        Self::new(404, d)
    }
    pub fn conflict(d: impl Into<String>) -> Self {
        Self::new(409, d)
    }
    pub fn too_large(d: impl Into<String>) -> Self {
        Self::new(413, d)
    }
    pub fn internal(d: impl Into<String>) -> Self {
        Self::new(500, d)
    }
    pub fn bad_gateway(d: impl Into<String>) -> Self {
        Self::new(502, d)
    }
    pub fn unavailable(d: impl Into<String>) -> Self {
        Self::new(503, d)
    }
    /// 422：请求体结构不合法（对齐 FastAPI 的校验失败状态码）。
    pub fn unprocessable(d: impl Into<String>) -> Self {
        Self::new(422, d)
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        (self.status, Json(json!({ "detail": self.detail }))).into_response()
    }
}

impl From<sqlx::Error> for AppError {
    fn from(e: sqlx::Error) -> Self {
        AppError::internal(format!("数据库错误：{e}"))
    }
}

impl From<std::io::Error> for AppError {
    fn from(e: std::io::Error) -> Self {
        AppError::internal(format!("IO 错误：{e}"))
    }
}

impl From<bollard::errors::Error> for AppError {
    fn from(e: bollard::errors::Error) -> Self {
        AppError::bad_gateway(format!("Docker 调用失败：{e}"))
    }
}

impl From<zip::result::ZipError> for AppError {
    fn from(e: zip::result::ZipError) -> Self {
        AppError::internal(format!("压缩包处理失败：{e}"))
    }
}

pub type AppResult<T> = Result<T, AppError>;

/// 统一 JSON 请求体提取器。
///
/// axum 内建 `Json` 的 rejection 是**纯文本**（如 `Failed to deserialize ...`），
/// 而 FastAPI 的校验失败一律返回 `{"detail": ...}`。机器对接方（商城 / 开放面）
/// 通常直接 `res.json()`，纯文本会导致解析异常，故此处统一收敛为 JSON 体。
pub struct AppJson<T>(pub T);

impl<T, S> axum::extract::FromRequest<S> for AppJson<T>
where
    axum::Json<T>: axum::extract::FromRequest<S, Rejection = axum::extract::rejection::JsonRejection>,
    S: Send + Sync,
{
    type Rejection = AppError;

    async fn from_request(
        req: axum::extract::Request,
        state: &S,
    ) -> Result<Self, Self::Rejection> {
        match axum::Json::<T>::from_request(req, state).await {
            Ok(axum::Json(v)) => Ok(AppJson(v)),
            Err(e) => Err(AppError::unprocessable(format!(
                "请求体格式错误：{}",
                e.body_text()
            ))),
        }
    }
}

/// 把阻塞/CPU 密集操作放到阻塞线程池执行。
pub async fn blocking<T, F>(f: F) -> AppResult<T>
where
    T: Send + 'static,
    F: FnOnce() -> AppResult<T> + Send + 'static,
{
    tokio::task::spawn_blocking(f)
        .await
        .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))?
}
