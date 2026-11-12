//! PPanel 被控 Agent 入口（Rust 版）：
//! 对外提供节点管理 API（/agent/*，X-Node-Token）、独立面板（/panel + /panel/*）、
//! 开放开通面（/open/*，X-API-Key），以及按 Host 的域名反代。
//!
//! 与 Python 版 agent_main.py 行为一致：
//!   - 启动：建表 + 补列 + 占位账号 + 存量启动命令升级 + 后台采样/调度
//!   - 后台：60s 采样用量（面板历史曲线）、同步实例状态并触发商城 webhook、清理孤儿容器
//!   - 静态：/panel 单文件 UI、/panel/vs（Monaco）、/panel/vendor（xterm）、/panel/iconfont.js

// 与 Python 版逐模块对齐时保留了若干「Python 侧同样未被调用」的辅助函数
// （caddy 的 Admin API 路由管理、COS 生命周期清理、host_ops 的交互式执行等），
// 用于后续功能扩展与行为对照。端点覆盖由 scripts/diff_routes.py 兜底校验，
// 因此这里统一放行 dead_code，避免几百行移植代码被误删。
#![allow(dead_code)]

mod auth;
mod config;
mod db;
mod docker;
mod error;
mod models;
mod routers;
mod services;
mod state;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::{header, HeaderMap, Method, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde_json::{json, Value};
use tower_http::services::ServeDir;

use crate::config::settings;
use crate::models::Instance;
use crate::services::{cron, instance};
use crate::state::AppState;

#[tokio::main]
async fn main() {
    let s = config::init();
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let pool = match db::init().await {
        Ok(p) => p,
        Err(e) => {
            eprintln!("[agent] 数据库初始化失败：{}", e.detail);
            std::process::exit(1);
        }
    };
    if let Err(e) = db::ensure_agent_user(&pool).await {
        eprintln!("[agent] 占位账号初始化失败：{}", e.detail);
    }
    if let Err(e) = db::ensure_local_admin(&pool).await {
        eprintln!("[agent] 独立面板管理员初始化失败：{}", e.detail);
    }

    let state = AppState::new(pool);

    // 启动命令归一：旧版本把 requirements.txt 的安装拼进了默认启动命令（一长串 if/fi），
    // 现在改由容器入口自动完成（services::instance::entry_cmd_str），启动命令只留一行。
    // 这里把平台自己生成的那条长命令还原；用户手工改过的命令不动。
    let _ = sqlx::query(
        "UPDATE instances SET start_cmd = 'python main.py' \
         WHERE start_cmd LIKE '%pip install -r requirements.txt%' \
           AND start_cmd LIKE '%python main.py' \
           AND image NOT LIKE 'php:%' AND image NOT LIKE 'node:%' AND image NOT LIKE 'golang:%'",
    )
    .execute(&state.db)
    .await;
    // 默认启动命令不再落库：空 = 平台默认（被控按入口文件存在与否自动决定，缺文件进空闲模式
    // 而不是 crash）。把历史上平台写入的默认值清掉；用户改过的命令不动。
    let _ = sqlx::query(
        "UPDATE instances SET start_cmd = '' WHERE start_cmd IN \
         ('python main.py', 'node index.js', 'php -S 0.0.0.0:8000 -t /app', \
          '[ -f go.mod ] || go mod init ppanel-app; go build -o app . && ./app')",
    )
    .execute(&state.db)
    .await;
    // PHP 运行参数（-d disable_functions/upload/memory…）不再拼进启动命令：
    // 改由容器入口按 disabled_funcs 列与节点配置动态注入（services::instance::php_ini_flags）。
    // 把存量命令里的 -d 段剥掉；剥完与默认命令等价的清空，用户自定义端口等其余内容保留。
    {
        use sqlx::Row;
        let rows = sqlx::query(
            "SELECT id, start_cmd FROM instances \
             WHERE (image LIKE 'php:%' OR image LIKE 'ppanel-php:%') AND start_cmd LIKE '%-d %'",
        )
        .fetch_all(&state.db)
        .await
        .unwrap_or_default();
        for row in rows {
            let id: i64 = row.get("id");
            let cmd: String = row.get("start_cmd");
            let clean = services::instance::strip_php_flags(&cmd);
            if clean == cmd {
                continue;
            }
            let final_cmd = if clean == "php -S 0.0.0.0:8000 -t /app" { "" } else { &clean };
            let _ = sqlx::query("UPDATE instances SET start_cmd = ? WHERE id = ?")
                .bind(final_cmd)
                .bind(id)
                .execute(&state.db)
                .await;
        }
    }

    if s.node_token.is_empty() {
        eprintln!("[agent] 警告：未设置 NODE_TOKEN，所有请求将被拒绝（401）");
    } else {
        eprintln!("[agent] 节点鉴权已启用，监听 {}:{}", s.agent_host, s.agent_port);
    }
    if s.open_api_key.is_empty() {
        eprintln!("[agent] 开通接口未启用（未配置 OPEN_API_KEY）");
    } else {
        eprintln!("[agent] 开通接口 /open/* 已启用（X-API-Key）");
    }

    tokio::spawn(metrics_loop(state.clone()));
    // 宿主机资源采样 + 阈值告警（每分钟；数据供节点面板 24h/7d 曲线与告警设置）
    tokio::spawn(services::host_metrics::run_loop(state.clone()));
    tokio::spawn(cron::run_loop(state.clone()));
    tokio::spawn(services::db_backup::run_loop(state.clone()));

    let app = build_router(state.clone());

    let addr = format!("{}:{}", s.agent_host, s.agent_port);
    let listener = match tokio::net::TcpListener::bind(&addr).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[agent] 端口监听失败 {addr}：{e}");
            std::process::exit(1);
        }
    };
    eprintln!("[agent] 已就绪：http://{addr}");
    if let Err(e) = axum::serve(listener, app).await {
        eprintln!("[agent] 服务异常退出：{e}");
    }
}

// =====================================================================
// 路由装配
// =====================================================================

fn build_router(state: AppState) -> Router {
    // 节点管理：/agent/health、实例、文件、Docker、防火墙、安全防护、备份、升级
    let agent = Router::new()
        .merge(routers::node_admin::routes())
        .merge(routers::node_security::routes())
        .merge(routers::node_system::routes())
        .merge(routers::node_ops::routes())
        .merge(routers::ws::ws_routes());

    // 独立面板：先挂扩展路由（panel_ops 里的多段路径优先于 panel 的 /instance/{action} 通配）
    let panel = Router::new()
        .merge(routers::panel_ops::routes())
        .merge(routers::panel::routes());

    let mut app = Router::new()
        .nest("/agent", agent)
        // 本地管理 API 挂顶层（/panel-admin/*），与 /panel/* 平级、不进 /agent 前缀
        .merge(routers::panel_admin::routes())
        .merge(routers::node_ops::open_routes())
        .merge(panel)
        .merge(routers::ws::panel_ws_routes())
        .route("/agent/ping", get(ping))
        .route("/health", get(health));

    // 独立面板静态资源（单文件 UI + Monaco + xterm + iconfont）
    let ui = panel_ui_dir();
    app = app.route("/panel", get(panel_page));
    let vs = ui.join("vs");
    let vendor = ui.join("vendor");
    if vs.is_dir() {
        app = app.nest_service("/panel/vs", ServeDir::new(vs));
    }
    if vendor.is_dir() {
        app = app.nest_service("/panel/vendor", ServeDir::new(vendor));
    }
    if ui.join("iconfont.js").is_file() {
        app = app.route("/panel/iconfont.js", get(panel_iconfont));
    }

    app
        // 上传大文件：单文件上限由业务层（UPLOAD_LIMIT_MB）继续校验
        .layer(axum::extract::DefaultBodyLimit::max(512 * 1024 * 1024))
        .layer(middleware::from_fn_with_state(state.clone(), domain_proxy))
        .with_state(state)
}

async fn ping() -> Json<Value> {
    Json(json!({"ok": true, "service": "ppanel-agent"}))
}

async fn health() -> Json<Value> {
    Json(json!({"ok": true}))
}

// ---------- 独立面板单文件 UI ----------

/// 面板 UI 目录：环境变量 PPANEL_UI_DIR 优先，其次按 cwd / 可执行文件位置向上找 app/panel_ui。
fn panel_ui_dir() -> std::path::PathBuf {
    if let Ok(p) = std::env::var("PPANEL_UI_DIR") {
        let p = std::path::PathBuf::from(p);
        if p.is_dir() {
            return p;
        }
    }
    let mut starts: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(d) = std::env::current_dir() {
        starts.push(d);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(p) = exe.parent() {
            starts.push(p.to_path_buf());
        }
    }
    for start in &starts {
        let mut d = Some(start.clone());
        while let Some(cur) = d {
            let cand = cur.join("app").join("panel_ui");
            if cand.is_dir() {
                return cand;
            }
            d = cur.parent().map(|p| p.to_path_buf());
        }
    }
    starts
        .first()
        .map(|p| p.join("app").join("panel_ui"))
        .unwrap_or_else(|| std::path::PathBuf::from("app/panel_ui"))
}

async fn panel_page() -> Response {
    let file = panel_ui_dir().join("index.html");
    match tokio::fs::read(&file).await {
        Ok(bytes) => Response::builder()
            .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
            // no-store：页面更新后浏览器立即生效，避免旧缓存
            .header(header::CACHE_CONTROL, "no-store")
            .body(Body::from(bytes))
            .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response()),
        Err(_) => (
            StatusCode::NOT_FOUND,
            Json(json!({"detail": format!("面板未部署：缺少 {}", file.display())})),
        )
            .into_response(),
    }
}

async fn panel_iconfont() -> Response {
    let file = panel_ui_dir().join("iconfont.js");
    match tokio::fs::read(&file).await {
        Ok(bytes) => Response::builder()
            .header(header::CONTENT_TYPE, "application/javascript; charset=utf-8")
            .body(Body::from(bytes))
            .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response()),
        Err(_) => StatusCode::NOT_FOUND.into_response(),
    }
}

// =====================================================================
// 域名反代：绑定了域名的实例，按 Host 转发到实例端口
// =====================================================================

async fn domain_map(state: &AppState) -> HashMap<String, i64> {
    if let Some(map) = state.domains.fresh().await {
        return map;
    }
    let rows: Vec<(String, i64)> = sqlx::query_as(
        "SELECT domain, ext_port FROM instances \
         WHERE domain IS NOT NULL AND domain != '' AND ext_port > 0",
    )
    .fetch_all(&state.db)
    .await
    .unwrap_or_default();
    let map: HashMap<String, i64> = rows
        .into_iter()
        .map(|(d, p)| (d.to_lowercase(), p))
        .collect();
    state.domains.store(map.clone()).await;
    map
}

async fn domain_proxy(State(state): State<AppState>, req: Request, next: Next) -> Response {
    let host = req
        .headers()
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .split(':')
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase();

    // 命中已绑定域名才转发；IP/localhost/面板路径不受影响
    if !host.is_empty() && host.contains('.') && !host.parse::<std::net::IpAddr>().is_ok() {
        if let Some(port) = domain_map(&state).await.get(&host).copied() {
            return proxy_to(req, port).await;
        }
    }
    next.run(req).await
}

async fn proxy_to(req: Request, port: i64) -> Response {
    let method = req.method().clone();
    let path_q = req
        .uri()
        .path_and_query()
        .map(|p| p.as_str().to_string())
        .unwrap_or_else(|| "/".to_string());
    let headers = req.headers().clone();
    let body = match axum::body::to_bytes(req.into_body(), 64 * 1024 * 1024).await {
        Ok(b) => b,
        Err(_) => return (StatusCode::BAD_REQUEST, Json(json!({"detail": "请求体读取失败"}))).into_response(),
    };

    let url = format!("http://127.0.0.1:{port}{path_q}");
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(60))
        .build()
    {
        Ok(c) => c,
        Err(_) => return (StatusCode::BAD_GATEWAY, Json(json!({"detail": "实例服务不可达"}))).into_response(),
    };

    let method_rs = match reqwest::Method::from_bytes(method.as_str().as_bytes()) {
        Ok(m) => m,
        Err(_) => return (StatusCode::BAD_REQUEST, Json(json!({"detail": "方法不支持"}))).into_response(),
    };
    let mut rb = client.request(method_rs, &url).body(body.to_vec());
    for (k, v) in headers.iter() {
        let name = k.as_str().to_lowercase();
        if matches!(name.as_str(), "host" | "content-length" | "connection" | "accept-encoding") {
            continue;
        }
        if let Ok(s) = v.to_str() {
            rb = rb.header(k.as_str(), s);
        }
    }

    match rb.send().await {
        Ok(resp) => {
            let status = resp.status();
            let mut out_headers = HeaderMap::new();
            for (k, v) in resp.headers().iter() {
                let name = k.as_str().to_lowercase();
                if matches!(
                    name.as_str(),
                    "content-length" | "transfer-encoding" | "content-encoding" | "connection"
                ) {
                    continue;
                }
                out_headers.insert(k.clone(), v.clone());
            }
            let bytes = resp.bytes().await.unwrap_or_default();
            (status, out_headers, bytes).into_response()
        }
        Err(_) => (
            StatusCode::BAD_GATEWAY,
            Json(json!({"detail": "实例服务不可达"})),
        )
            .into_response(),
    }
}

// =====================================================================
// 后台任务：60s 用量采样 / 状态同步 / 商城 webhook / 孤儿容器对账
// =====================================================================

type SentMap = Arc<Mutex<HashMap<String, f64>>>;

async fn metrics_loop(state: AppState) {
    let sent: SentMap = Arc::new(Mutex::new(HashMap::new()));
    loop {
        tokio::time::sleep(Duration::from_secs(60)).await;
        if let Err(e) = instance::sample_all_metrics(&state).await {
            tracing::debug!("用量采样失败：{}", e.detail);
        }
        if let Err(e) = watch_once(&state, &sent).await {
            tracing::debug!("状态巡检失败：{}", e.detail);
        }
        let _ = instance::reconcile_orphans(&state).await;
    }
}

/// 状态同步 + 崩溃通知 + 到期提醒（内存去重，重启后最多重发一次）。
async fn watch_once(state: &AppState, sent: &SentMap) -> error::AppResult<()> {
    let rows: Vec<Instance> = sqlx::query_as("SELECT * FROM instances")
        .fetch_all(&state.db)
        .await?;
    let now = models::utcnow();
    let ts = now.and_utc().timestamp() as f64;

    for mut inst in rows {
        instance::sync_status(state, &mut inst).await;
        let _ = instance::persist_status(&state.db, &inst).await;

        // 崩溃：running → exited，至少间隔 1h 才重复提醒
        if inst.status == "exited" {
            let key = format!("exited:{}", inst.id);
            if should_send(sent, &key, ts, 3600.0) {
                webhook(json!({
                    "event": "instance_exited",
                    "iid": inst.id,
                    "name": inst.name,
                    "owner_ref": inst.owner_ref,
                    "ts": models::dt_iso(&now),
                }))
                .await;
            }
        }

        // 到期提醒：≤3天 / ≤1天 / 已过期 三档，各档每天最多发一次
        let Some(expire) = inst.expire_at else { continue };
        let left = (expire - now).num_seconds() as f64 / 86400.0;
        let warn_days = settings().webhook_warn_days as f64;
        let tier = if left <= 0.0 {
            "expired"
        } else if left <= 1.0 {
            "d1"
        } else if left <= warn_days {
            "d3"
        } else {
            ""
        };
        if tier.is_empty() {
            continue;
        }
        let key = format!("expire:{}:{}", inst.id, tier);
        if should_send(sent, &key, ts, 86400.0) {
            webhook(json!({
                "event": "instance_expiring",
                "iid": inst.id,
                "name": inst.name,
                "owner_ref": inst.owner_ref,
                "tier": tier,
                "expire_at": models::dt_iso(&expire),
                "days_left": (left * 10.0).round() / 10.0,
                "ts": models::dt_iso(&now),
            }))
            .await;
        }
    }
    Ok(())
}

fn should_send(sent: &SentMap, key: &str, now: f64, window: f64) -> bool {
    let mut g = sent.lock().unwrap();
    let last = g.get(key).copied().unwrap_or(0.0);
    if now - last > window {
        g.insert(key.to_string(), now);
        true
    } else {
        false
    }
}

async fn webhook(event: Value) {
    let url = settings().webhook_url.clone();
    if url.is_empty() {
        return;
    }
    let client = match reqwest::Client::builder().timeout(Duration::from_secs(5)).build() {
        Ok(c) => c,
        Err(_) => return,
    };
    if let Err(e) = client.post(&url).json(&event).send().await {
        // 通知失败不影响主流程
        tracing::debug!("webhook 发送失败：{e}");
    }
}

// 供 Method 类型显式引用（保持与 domain_proxy 里对方法判断的语义一致）
#[allow(dead_code)]
fn _method_unused(_m: Method) {}
