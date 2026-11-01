//! 鉴权：bcrypt 密码、JWT（用户 / 面板令牌）、节点令牌与 API Key 校验。

use axum::extract::FromRequestParts;
use axum::http::request::Parts;
use chrono::{Duration, Utc};
use jsonwebtoken::{decode, encode, Algorithm, DecodingKey, EncodingKey, Header, Validation};
use serde::{Deserialize, Serialize};

use crate::config::settings;
use crate::error::AppError;
use crate::state::AppState;

pub fn hash_password(raw: &str) -> String {
    bcrypt::hash(raw, bcrypt::DEFAULT_COST).unwrap_or_default()
}

pub fn verify_password(raw: &str, hashed: &str) -> bool {
    bcrypt::verify(raw, hashed).unwrap_or(false)
}

#[derive(Debug, Serialize, Deserialize)]
struct Claims {
    sub: String,
    exp: i64,
    /// 面板会话绑定的令牌指纹。管理员重置 panel_token 后指纹不再匹配，
    /// 已签发的旧会话立即失效（否则重置令牌只是挡住新登录，旧会话仍能操作容器）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pt: Option<String>,
}

/// 面板令牌指纹：只把 hash 放进会话，避免把令牌原文写进 JWT 载荷。
pub fn panel_token_fp(raw: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(raw.as_bytes());
    h.finalize()[..8].iter().map(|b| format!("{b:02x}")).collect()
}

fn jwt_keys() -> (EncodingKey, DecodingKey) {
    let secret = settings().jwt_secret.as_bytes();
    (EncodingKey::from_secret(secret), DecodingKey::from_secret(secret))
}

pub fn create_user_token(user_id: i64) -> String {
    let payload = Claims {
        sub: user_id.to_string(),
        exp: (Utc::now() + Duration::minutes(settings().jwt_expire_minutes)).timestamp(),
        pt: None,
    };
    encode(&Header::new(Algorithm::HS256), &payload, &jwt_keys().0).unwrap_or_default()
}

/// `panel_token` 是实例当前的面板令牌原文，其指纹会写进会话，
/// 用于在重置令牌/吊销后让旧会话失效。
pub fn create_panel_token(iid: i64, panel_token: &str) -> String {
    let payload = Claims {
        sub: format!("panel:{iid}"),
        exp: (Utc::now() + Duration::minutes(settings().jwt_expire_minutes)).timestamp(),
        pt: Some(panel_token_fp(panel_token)),
    };
    encode(&Header::new(Algorithm::HS256), &payload, &jwt_keys().0).unwrap_or_default()
}

/// 独立面板本地管理员会话（/panel-admin/*）。
pub fn create_admin_token() -> String {
    let payload = Claims {
        sub: "local-admin".to_string(),
        // 管理员会话比面板会话更长效（口令登录不频繁）
        exp: (Utc::now() + Duration::minutes(settings().jwt_expire_minutes * 7)).timestamp(),
        pt: None,
    };
    encode(&Header::new(Algorithm::HS256), &payload, &jwt_keys().0).unwrap_or_default()
}

pub fn decode_admin_session(token: &str) -> bool {
    decode_claims(token)
        .map(|c| c.sub == "local-admin")
        .unwrap_or(false)
}

fn decode_claims(token: &str) -> Option<Claims> {
    let mut val = Validation::new(Algorithm::HS256);
    val.validate_exp = true;
    decode::<Claims>(token, &jwt_keys().1, &val).ok().map(|d| d.claims)
}

pub fn decode_user_token(token: &str) -> Option<i64> {
    decode_claims(token).and_then(|c| c.sub.parse::<i64>().ok())
}

pub fn decode_panel_session(token: &str) -> Option<(i64, Option<String>)> {
    let c = decode_claims(token)?;
    let iid = c.sub.strip_prefix("panel:").and_then(|v| v.parse::<i64>().ok())?;
    Some((iid, c.pt))
}

/// 面板 Bearer / ?t= 兜底（浏览器下载不带 header）。返回实例 id 与会话绑定的令牌指纹。
pub fn panel_session_from_parts(parts: &Parts) -> Option<(i64, Option<String>)> {
    panel_session(&parts.headers, parts.uri.query().unwrap_or(""))
}

/// 从请求头 + query 提取面板会话（Bearer 优先，其次 ?t=）。
pub fn panel_session(headers: &axum::http::HeaderMap, query: &str) -> Option<(i64, Option<String>)> {
    let auth = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let token = auth.strip_prefix("Bearer ").unwrap_or("");
    if !token.is_empty() {
        return decode_panel_session(token);
    }
    query_param(query, "t").and_then(|t| decode_panel_session(&t))
}

pub fn query_param(query: &str, key: &str) -> Option<String> {
    for pair in query.split('&') {
        let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
        if k == key {
            return Some(percent_decode(v));
        }
    }
    None
}

pub fn percent_decode(v: &str) -> String {
    percent_encoding::percent_decode_str(&v.replace('+', " "))
        .decode_utf8_lossy()
        .to_string()
}

pub fn node_ok(token: &str) -> bool {
    let expect = &settings().node_token;
    !expect.is_empty() && constant_eq(token, expect)
}

pub fn api_key_ok(key: &str) -> bool {
    let s = settings();
    (!s.open_api_key.is_empty() && constant_eq(key, &s.open_api_key))
        || (!s.api_key.is_empty() && constant_eq(key, &s.api_key))
}

pub fn node_or_api_ok(node: &str, api: &str) -> bool {
    node_ok(node) || api_key_ok(api)
}

fn constant_eq(a: &str, b: &str) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.bytes().zip(b.bytes()) {
        diff |= x ^ y;
    }
    diff == 0
}

fn header<'a>(parts: &'a Parts, name: &str) -> &'a str {
    parts
        .headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
}

/// 节点令牌提取器（X-Node-Token）。
pub struct NodeAuth;

impl FromRequestParts<AppState> for NodeAuth {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, _s: &AppState) -> Result<Self, Self::Rejection> {
        if node_ok(header(parts, "x-node-token")) {
            Ok(NodeAuth)
        } else {
            Err(AppError::unauthorized("节点凭证无效"))
        }
    }
}

/// 宿主机管理接口：X-Node-Token 或 X-API-Key 任一匹配。
pub struct NodeOrApi;

impl FromRequestParts<AppState> for NodeOrApi {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, _s: &AppState) -> Result<Self, Self::Rejection> {
        if node_or_api_ok(header(parts, "x-node-token"), header(parts, "x-api-key")) {
            Ok(NodeOrApi)
        } else {
            Err(AppError::unauthorized(
                "凭证无效（需 X-Node-Token 或 X-API-Key）",
            ))
        }
    }
}

/// 用户 JWT / API Key（面板用户体系；Agent 仅登录/管理接口使用）。
pub struct Principal {
    pub user_id: Option<i64>,
    pub is_admin: bool,
    pub via_api_key: bool,
}

impl FromRequestParts<AppState> for Principal {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, state: &AppState) -> Result<Self, Self::Rejection> {
        let api = header(parts, "x-api-key");
        if !api.is_empty() && api_key_ok(api) {
            return Ok(Principal { user_id: None, is_admin: true, via_api_key: true });
        }
        let auth = header(parts, "authorization");
        if let Some(token) = auth.strip_prefix("Bearer ") {
            if let Some(uid) = decode_user_token(token) {
                let user: Option<(i64, String)> =
                    sqlx::query_as("SELECT id, role FROM users WHERE id = ?")
                        .bind(uid)
                        .fetch_optional(&state.db)
                        .await
                        .ok()
                        .flatten();
                if let Some((id, role)) = user {
                    return Ok(Principal {
                        user_id: Some(id),
                        is_admin: role == "admin",
                        via_api_key: false,
                    });
                }
            }
        }
        Err(AppError::unauthorized("未登录或凭证已失效"))
    }
}

pub struct AdminPrincipal;

impl FromRequestParts<AppState> for AdminPrincipal {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, state: &AppState) -> Result<Self, Self::Rejection> {
        let p = Principal::from_request_parts(parts, state).await?;
        if p.is_admin {
            Ok(AdminPrincipal)
        } else {
            Err(AppError::forbidden("需要管理员权限"))
        }
    }
}

/// 独立面板本地管理员提取器：Bearer 会话（sub=local-admin）。
pub struct LocalAdmin;

impl FromRequestParts<AppState> for LocalAdmin {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, _s: &AppState) -> Result<Self, Self::Rejection> {
        if decode_admin_session(header(parts, "authorization").strip_prefix("Bearer ").unwrap_or("")) {
            Ok(LocalAdmin)
        } else {
            Err(AppError::unauthorized("本地管理员会话无效"))
        }
    }
}

/// 面板令牌随机值（secrets.token_urlsafe(24) 等价：32 字符）。
pub fn new_panel_token() -> String {
    use base64::Engine;
    use rand::Rng;
    let mut rng = rand::thread_rng();
    let bytes: Vec<u8> = (0..24).map(|_| rng.gen::<u8>()).collect();
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}
