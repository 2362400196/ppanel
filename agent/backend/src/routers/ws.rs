//! 被控 Agent WebSocket（节点级鉴权 ?node_token=，挂载在 /agent 前缀下 → /agent/ws/...）：
//! 实例日志跟踪 / 依赖安装回显 / 镜像拉取进度 / 宿主机终端。
//!
//! 行为对齐 app/routers/agent_ws.py（agent_main 实际挂载的那份，不是旧面板的 ws.py）：
//! - token 校验失败：Python `ws.close(code=4401)`（未 accept，Starlette 实际返回 HTTP 403），
//!   这里在升级前直接返回 403。
//! - 4xx 业务关闭码沿用 Python：实例/容器不存在 4404、Docker 不可用 4503、镜像参数非法 4400。

use std::io::{Read, Write};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::extract::ws::{CloseFrame, Message, Utf8Bytes, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::Router;
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::sync::mpsc::{self, UnboundedSender};

use crate::auth::decode_user_token;
use crate::config::settings;
use crate::services::host_ops;
use crate::services::instance::{container_name, fetch_instance};
use crate::state::{AppState, ExecJob};

#[derive(Debug, Deserialize, Default)]
struct WsQuery {
    #[serde(default)]
    node_token: Option<String>,
    #[serde(default)]
    tail: Option<String>,
    #[serde(default)]
    job_id: Option<String>,
    #[serde(default)]
    image: Option<String>,
}

/// 与 Python `ws_node_ok` 完全一致：必须配置了 NODE_TOKEN 且 query 参数相等。
fn token_ok(q: &WsQuery) -> bool {
    let token = &settings().node_token;
    !token.is_empty() && q.node_token.as_deref() == Some(token.as_str())
}

/// token 校验失败 → 403（等价 Python 在 accept 前 close(4401)）。
fn forbidden() -> Response {
    StatusCode::FORBIDDEN.into_response()
}

fn text_msg(s: &str) -> Message {
    Message::Text(s.into())
}

fn json_msg(v: &Value) -> Message {
    Message::Text(v.to_string().into())
}

async fn close_code(socket: &mut WebSocket, code: u16) {
    let _ = socket
        .send(Message::Close(Some(CloseFrame {
            code,
            reason: Utf8Bytes::from_static(""),
        })))
        .await;
}

// ---------- 实例日志（先补 tail 历史，再实时 follow） ----------

async fn logs_handler(
    State(state): State<AppState>,
    Path(instance_id): Path<i64>,
    Query(q): Query<WsQuery>,
    ws: WebSocketUpgrade,
) -> Response {
    if !token_ok(&q) {
        return forbidden();
    }
    let tail = q
        .tail
        .as_deref()
        .filter(|s| !s.is_empty())
        .and_then(|s| s.parse::<i64>().ok())
        .unwrap_or(200);
    ws.on_upgrade(move |socket| logs_task(socket, state, instance_id, tail))
}

async fn logs_task(mut socket: WebSocket, state: AppState, instance_id: i64, tail: i64) {
    let inst = match fetch_instance(&state.db, instance_id).await {
        Ok(i) => i,
        Err(_) => {
            let _ = socket.send(text_msg("[agent] 实例不存在")).await;
            close_code(&mut socket, 4404).await;
            return;
        }
    };
    let Some(docker) = state.docker.try_get().await else {
        let _ = socket.send(text_msg("[agent] Docker 服务不可用")).await;
        close_code(&mut socket, 4503).await;
        return;
    };
    let name = container_name(inst.id);
    if docker.inspect_container(&name, None).await.is_err() {
        let _ = socket.send(text_msg("[agent] 容器不存在")).await;
        close_code(&mut socket, 4404).await;
        return;
    }

    // follow=True 时 bollard 先回放 tail 条历史再持续推送，与 Python container.logs 语义一致
    let opts = bollard::container::LogsOptions::<String> {
        follow: true,
        stdout: true,
        stderr: true,
        since: 0,
        until: 0,
        timestamps: false,
        tail: tail.clamp(1, 5000).to_string(),
    };
    let mut stream = docker.logs(&name, Some(opts));
    while let Some(item) = stream.next().await {
        let Ok(out) = item else { break };
        use bollard::container::LogOutput;
        let text = match out {
            LogOutput::StdOut { message }
            | LogOutput::StdErr { message }
            | LogOutput::Console { message }
            | LogOutput::StdIn { message } => String::from_utf8_lossy(&message).to_string(),
        };
        if socket.send(text_msg(&text)).await.is_err() {
            break;
        }
    }
    // 日志流结束（容器停止/日志关闭）：正常关闭（Python 侧由 ASGI 收尾关闭）
    close_code(&mut socket, 1000).await;
}

// ---------- 依赖安装回显（内存任务流） ----------

async fn exec_handler(
    State(state): State<AppState>,
    Path(instance_id): Path<i64>,
    Query(q): Query<WsQuery>,
    ws: WebSocketUpgrade,
) -> Response {
    if !token_ok(&q) {
        return forbidden();
    }
    let job_id = q.job_id.clone().unwrap_or_default();
    ws.on_upgrade(move |socket| exec_task(socket, state, instance_id, job_id))
}

async fn exec_task(mut socket: WebSocket, state: AppState, instance_id: i64, job_id: String) {
    let inst = match fetch_instance(&state.db, instance_id).await {
        Ok(i) => i,
        Err(_) => {
            let _ = socket.send(text_msg("[agent] 实例不存在")).await;
            close_code(&mut socket, 4404).await;
            return;
        }
    };
    let job: Option<Arc<ExecJob>> = state.jobs.get(&job_id);
    let job = match job {
        Some(j) if j.instance_id == inst.id => j,
        _ => {
            let _ = socket.send(text_msg("[agent] 安装任务不存在或已结束")).await;
            close_code(&mut socket, 1000).await;
            return;
        }
    };

    let mut idx = 0usize;
    loop {
        let lines = job.snapshot();
        while idx < lines.len() {
            if socket.send(text_msg(&lines[idx])).await.is_err() {
                return;
            }
            idx += 1;
        }
        if job.is_done() && idx >= job.snapshot().len() {
            let code = match *job.exit_code.lock().unwrap() {
                Some(c) => c.to_string(),
                None => "None".to_string(),
            };
            let _ = socket
                .send(text_msg(&format!("[agent] 任务完成 exit={code}")))
                .await;
            break;
        }
        tokio::time::sleep(Duration::from_millis(300)).await;
    }
    close_code(&mut socket, 1000).await;
}

// ---------- docker pull 进度（与 Python _PullRender 快照格式一致） ----------

fn fmt_size(n: f64) -> String {
    for (unit, div) in [("GB", 1u64 << 30), ("MB", 1u64 << 20), ("KB", 1u64 << 10)] {
        if n >= div as f64 {
            return if unit == "KB" {
                format!("{:.0}KB", n / div as f64)
            } else {
                format!("{:.1}{unit}", n / div as f64)
            };
        }
    }
    format!("{n:.0}B")
}

#[derive(Default)]
struct PullRender {
    done: Vec<String>,
    /// 层 id -> [状态, current, total]（保持插入顺序）
    active: Vec<(String, String, i64, i64)>,
    last: Option<Instant>,
}

impl PullRender {
    fn snapshot(&self) -> String {
        let mut lines = self.done.clone();
        for (lid, st, cur, total) in &self.active {
            if *total != 0 && *cur != 0 {
                let cur = (*cur).min(*total);
                let w: i64 = 26;
                let filled = (cur * w / *total) as usize;
                let bar = if filled < w as usize {
                    let w = w as usize;
                    format!("{}{}{}", "=".repeat(filled), ">", " ".repeat(w - filled - 1))
                } else {
                    "=".repeat(w as usize)
                };
                lines.push(format!(
                    "{lid}: {st}  [{bar}]  {}/{}",
                    fmt_size(cur as f64),
                    fmt_size(*total as f64)
                ));
            } else {
                lines.push(format!("{lid}: {st}"));
            }
        }
        format!("\u{1}{}", lines.join("\n"))
    }

    /// 消费一个 pull 事件；返回要发送的快照（节流后）或 None；出错返回 Err(错误文案)。
    fn feed(&mut self, ev: &bollard::models::CreateImageInfo) -> Result<Option<String>, String> {
        let status = ev.status.as_deref().unwrap_or("").trim().to_string();
        if let Some(detail) = &ev.error_detail {
            let msg = detail
                .message
                .as_deref()
                .map(|m| m.trim().to_string())
                .filter(|m| !m.is_empty())
                .or_else(|| {
                    let s = status.clone();
                    if s.is_empty() {
                        None
                    } else {
                        Some(s)
                    }
                })
                .unwrap_or_else(|| "拉取失败".to_string());
            return Err(msg);
        }
        if status.is_empty() {
            return Ok(None);
        }
        let pid = ev.id.as_deref().unwrap_or("").trim().to_string();
        let now = Instant::now();

        // 头部/汇总行（Pulling from / Digest / Status）→ 固化
        if pid.is_empty() || status.starts_with("Pulling from") || status.starts_with("Digest") || status.starts_with("Status") {
            let line = if !pid.is_empty()
                && !status.starts_with("Digest")
                && !status.starts_with("Status")
            {
                format!("{pid}: {status}")
            } else {
                status.clone()
            };
            self.done.push(line);
            return Ok(Some(self.snapshot()));
        }

        if status == "Pull complete" || status == "Already exists" {
            self.active.retain(|(id, ..)| id != &pid);
            self.done.push(format!("{pid}: {status}"));
            return Ok(Some(self.snapshot())); // 状态变化强制重绘
        }

        let cur = ev.progress_detail.as_ref().and_then(|p| p.current).unwrap_or(0);
        let total = ev.progress_detail.as_ref().and_then(|p| p.total).unwrap_or(0);
        match self.active.iter_mut().find(|(id, ..)| id == &pid) {
            Some(slot) => {
                slot.1 = status;
                if total != 0 {
                    slot.2 = cur;
                    slot.3 = total;
                }
            }
            None => self.active.push((pid, status, 0, 0)),
        }

        if self.last.map(|t| now.duration_since(t).as_secs_f64() >= 0.3).unwrap_or(true) {
            self.last = Some(now);
            return Ok(Some(self.snapshot()));
        }
        Ok(None)
    }

    fn final_snapshot(&self) -> String {
        self.snapshot()
    }
}

async fn pull_handler(
    State(state): State<AppState>,
    Query(q): Query<WsQuery>,
    ws: WebSocketUpgrade,
) -> Response {
    if !token_ok(&q) {
        return forbidden();
    }
    let image = q.image.as_deref().unwrap_or("").trim().to_string();
    if image.is_empty() || image.chars().count() > 200 {
        return forbidden();
    }
    ws.on_upgrade(move |socket| pull_task(socket, state, image))
}

async fn pull_task(mut socket: WebSocket, state: AppState, image: String) {
    // Docker 不可用时 Python 先 accept 再提示关闭
    let docker = match state.docker.try_get().await {
        Some(d) => d,
        None => {
            let _ = socket.send(text_msg("[agent] Docker 服务不可用")).await;
            close_code(&mut socket, 4503).await;
            return;
        }
    };
    if socket
        .send(text_msg(&format!("[agent] 开始拉取镜像：{image}")))
        .await
        .is_err()
    {
        return;
    }
    if socket
        .send(text_msg(
            "[agent] 正在向 Docker 仓库查询镜像（镜像名不存在或网络慢时此步较久，请耐心等待）...",
        ))
        .await
        .is_err()
    {
        return;
    }

    let opts = bollard::image::CreateImageOptions {
        from_image: image.clone(),
        ..Default::default()
    };
    let mut stream = docker.create_image(Some(opts), None, None);
    let mut render = PullRender::default();
    loop {
        let ev = match stream.next().await {
            Some(Ok(e)) => e,
            Some(Err(e)) => {
                let msg = match &e {
                    bollard::errors::Error::DockerResponseServerError {
                        status_code, ..
                    } if *status_code == 404 => format!("[错误] 仓库中不存在镜像 {image}"),
                    bollard::errors::Error::DockerResponseServerError { .. } => {
                        format!("[错误] 拉取失败：{e}")
                    }
                    _ => format!("[错误] {e}"),
                };
                let _ = socket.send(text_msg(&msg)).await;
                return;
            }
            None => break,
        };
        match render.feed(&ev) {
            Ok(Some(msg)) => {
                if socket.send(text_msg(&msg)).await.is_err() {
                    return;
                }
            }
            Ok(None) => {}
            Err(msg) => {
                let _ = socket.send(text_msg(&format!("[错误] {msg}"))).await;
                return;
            }
        }
    }
    let _ = socket.send(text_msg(&render.final_snapshot())).await;
    let _ = socket
        .send(text_msg(&format!("[agent] 拉取完成：{image}")))
        .await;
    close_code(&mut socket, 1000).await;
}

// ---------- docker/term：宿主机 PTY 终端 / SSH 会话 ----------

enum TermEvent {
    Data(String),
    Closed(u64),
}

enum Ctrl {
    Json(Value),
    Close,
}

struct TermSession {
    master: Box<dyn portable_pty::MasterPty + Send>,
    writer: Box<dyn Write + Send>,
    child: Box<dyn portable_pty::Child + Send + Sync>,
}

type SessionHandle = Arc<Mutex<TermSession>>;

/// 打开 pty、启动子进程、起读线程（读线程负责把输出送回 async 侧）。
fn pty_spawn(
    id: u64,
    cmd: portable_pty::CommandBuilder,
    cols: u16,
    rows: u16,
    ev_tx: UnboundedSender<TermEvent>,
    auto_password: Option<String>,
) -> Result<SessionHandle, String> {
    let pair = portable_pty::native_pty_system()
        .openpty(portable_pty::PtySize {
            rows,
            cols,
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|e| e.to_string())?;
    let child = pair.slave.spawn_command(cmd).map_err(|e| e.to_string())?;
    // slave 句柄需在 spawn 后释放，否则读端不会 EOF
    drop(pair.slave);
    let mut reader = pair.master.try_clone_reader().map_err(|e| e.to_string())?;
    let writer = pair.master.take_writer().map_err(|e| e.to_string())?;
    let session: SessionHandle = Arc::new(Mutex::new(TermSession {
        master: pair.master,
        writer,
        child,
    }));

    let s = session.clone();
    std::thread::spawn(move || {
        let mut buf = [0u8; 4096];
        let mut pass_sent = false;
        loop {
            match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    let text = String::from_utf8_lossy(&buf[..n]).to_string();
                    // SSH 首次登录密码提示自动填充（前端通常不传密码场景下无影响）
                    if !pass_sent {
                        if let Some(p) = &auto_password {
                            if text.contains("assword") {
                                if let Ok(mut g) = s.lock() {
                                    let _ = g.writer.write_all(format!("{p}\n").as_bytes());
                                    let _ = g.writer.flush();
                                }
                                pass_sent = true;
                            }
                        }
                    }
                    if ev_tx.send(TermEvent::Data(text)).is_err() {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        let _ = ev_tx.send(TermEvent::Closed(id));
    });
    Ok(session)
}

fn close_session(s: &SessionHandle) {
    if let Ok(mut g) = s.lock() {
        let _ = g.child.kill();
    }
}

fn local_hostname() -> String {
    std::env::var("HOSTNAME")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| std::env::var("COMPUTERNAME").ok())
        .unwrap_or_else(|| "localhost".to_string())
}

fn local_user() -> String {
    std::env::var("USER")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| std::env::var("USERNAME").ok())
        .unwrap_or_else(|| "root".to_string())
}

/// 消息里的 cols/rows：Python `max(20,min(int(x or 120),500))`。
fn dims(v: &Value) -> (u16, u16) {
    let num = |k: &str, d: i64| -> i64 {
        match &v[k] {
            Value::Number(n) => n.as_i64().unwrap_or(d),
            Value::String(s) => s.trim().parse::<i64>().unwrap_or(d),
            _ => d,
        }
    };
    let cols = num("cols", 120).clamp(20, 500) as u16;
    let rows = num("rows", 30).clamp(5, 200) as u16;
    (cols, rows)
}

fn port_of(v: &Value) -> u16 {
    let p = match &v["port"] {
        Value::Number(n) => n.as_i64().unwrap_or(22),
        Value::String(s) => s.trim().parse::<i64>().unwrap_or(22),
        _ => 22,
    };
    p.clamp(1, 65535) as u16
}

async fn term_handler(Query(q): Query<WsQuery>, ws: WebSocketUpgrade) -> Response {
    if !token_ok(&q) {
        return forbidden();
    }
    ws.on_upgrade(term_task)
}

async fn term_task(socket: WebSocket) {
    let (mut sink, mut stream) = socket.split();
    let (ev_tx, mut ev_rx) = mpsc::unbounded_channel::<TermEvent>();
    let (ctrl_tx, mut ctrl_rx) = mpsc::unbounded_channel::<Ctrl>();
    let current: Arc<Mutex<Option<SessionHandle>>> = Arc::new(Mutex::new(None));
    let current_id = Arc::new(AtomicU64::new(0));
    let open_count = Arc::new(AtomicU64::new(1));

    // 输出泵：终端数据 + 控制消息统一由它写 socket
    let pump_id = current_id.clone();
    tokio::spawn(async move {
        loop {
            tokio::select! {
                ev = ev_rx.recv() => {
                    match ev {
                        Some(TermEvent::Data(t)) => {
                            if sink.send(json_msg(&json!({"type": "data", "data": t}))).await.is_err() {
                                return;
                            }
                        }
                        Some(TermEvent::Closed(id)) => {
                            if id == pump_id.load(Ordering::SeqCst) {
                                let _ = sink.send(json_msg(&json!({"type": "closed"}))).await;
                                return;
                            }
                        }
                        None => return,
                    }
                }
                c = ctrl_rx.recv() => {
                    match c {
                        Some(Ctrl::Json(v)) => {
                            if sink.send(json_msg(&v)).await.is_err() {
                                return;
                            }
                        }
                        _ => {
                            let _ = sink.send(Message::Close(Some(CloseFrame {
                                code: 1000,
                                reason: Utf8Bytes::from_static(""),
                            }))).await;
                            return;
                        }
                    }
                }
            }
        }
    });

    // 初始会话：Linux 用本机 PTY shell；Windows 本机终端不可用（与 Python 一致）
    if !cfg!(windows) {
        let hostname = local_hostname();
        let home = std::env::var("HOME").unwrap_or_else(|_| "/root".to_string());
        let shell = std::env::var("SHELL")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .unwrap_or_else(|| "/bin/bash".to_string());
        let mut cmd = portable_pty::CommandBuilder::new(shell);
        cmd.arg("-i");
        cmd.env("TERM", "xterm-256color");
        cmd.env("PS1", format!("\\u@{hostname}:\\w$ "));
        cmd.env("HOME", &home);
        if std::path::Path::new(&home).is_dir() {
            cmd.cwd(&home);
        }
        let sid = open_count.fetch_add(1, Ordering::SeqCst);
        match pty_spawn(sid, cmd, 120, 30, ev_tx.clone(), None) {
            Ok(s) => {
                current_id.store(sid, Ordering::SeqCst);
                *current.lock().unwrap() = Some(s);
                let _ = ctrl_tx.send(Ctrl::Json(
                    json!({"type": "mode", "mode": "local", "label": format!("{}@{hostname}", local_user())}),
                ));
            }
            Err(e) => {
                let _ = ctrl_tx.send(Ctrl::Json(
                    json!({"type": "error", "msg": format!("无法创建终端会话：{e}")}),
                ));
                let _ = ctrl_tx.send(Ctrl::Close);
                return;
            }
        }
    } else {
        let _ = ctrl_tx.send(Ctrl::Json(json!({
            "type": "mode",
            "mode": "none",
            "label": "本机终端仅 Linux 可用，可使用 SSH 登录远程节点",
        })));
    }

    // 前端消息循环
    while let Some(Ok(msg)) = stream.next().await {
        let text = match msg {
            Message::Text(t) => t,
            Message::Close(_) => break,
            _ => continue,
        };
        let v: Value = match serde_json::from_str(text.as_str()) {
            Ok(v) => v,
            Err(_) => continue,
        };
        match v["type"].as_str().unwrap_or("") {
            "resize" => {
                let (cols, rows) = dims(&v);
                let cur = current.lock().unwrap().clone();
                if let Some(s) = cur {
                    if let Ok(g) = s.lock() {
                        let _ = g.master.resize(portable_pty::PtySize {
                            rows,
                            cols,
                            pixel_width: 0,
                            pixel_height: 0,
                        });
                    }
                }
            }
            "input" => {
                let data = v["data"].as_str().unwrap_or("");
                let cur = current.lock().unwrap().clone();
                if let Some(s) = cur {
                    if !data.is_empty() {
                        let mut ok = false;
                        if let Ok(mut g) = s.lock() {
                            if g.writer.write_all(data.as_bytes()).is_ok() {
                                ok = g.writer.flush().is_ok();
                            }
                        }
                        if !ok {
                            break;
                        }
                    }
                }
            }
            "connect" => {
                let host = v["host"].as_str().unwrap_or("").trim().to_string();
                let user = {
                    let u = v["user"].as_str().unwrap_or("root").trim().to_string();
                    if u.is_empty() {
                        "root".to_string()
                    } else {
                        u
                    }
                };
                let password = v["password"].as_str().unwrap_or("").to_string();
                let port = port_of(&v);
                if host.is_empty() {
                    let _ = ctrl_tx.send(Ctrl::Json(
                        json!({"type": "error", "msg": "SSH 主机不能为空"}),
                    ));
                    continue;
                }
                let (cols, rows) = dims(&v);

                // 先做 TCP 可达性探测（12s，同 Python paramiko timeout）
                let reachable = tokio::time::timeout(
                    Duration::from_secs(12),
                    tokio::net::TcpStream::connect((host.as_str(), port)),
                )
                .await;
                match reachable {
                    Ok(Ok(_)) => {}
                    Ok(Err(e)) => {
                        let _ = ctrl_tx.send(Ctrl::Json(json!({
                            "type": "error",
                            "msg": format!("SSH 连接失败：{e}"),
                        })));
                        continue;
                    }
                    Err(_) => {
                        let _ = ctrl_tx.send(Ctrl::Json(json!({
                            "type": "error",
                            "msg": "SSH 连接失败：连接超时",
                        })));
                        continue;
                    }
                }

                let mut cmd = portable_pty::CommandBuilder::new("ssh");
                cmd.arg("-tt");
                cmd.arg("-p");
                cmd.arg(port.to_string());
                cmd.arg("-o");
                cmd.arg("StrictHostKeyChecking=no");
                cmd.arg("-o");
                cmd.arg("UserKnownHostsFile=/dev/null");
                cmd.env("TERM", "xterm-256color");
                cmd.arg(format!("{user}@{host}"));

                let sid = open_count.fetch_add(1, Ordering::SeqCst);
                let auto = if password.is_empty() {
                    None
                } else {
                    Some(password)
                };
                match pty_spawn(sid, cmd, cols, rows, ev_tx.clone(), auto) {
                    Ok(new_sess) => {
                        let old = {
                            let mut g = current.lock().unwrap();
                            g.replace(new_sess)
                        };
                        current_id.store(sid, Ordering::SeqCst);
                        if let Some(old) = old {
                            close_session(&old); // 旧会话关闭，读线程自然结束
                        }
                        let _ = ctrl_tx.send(Ctrl::Json(json!({
                            "type": "mode",
                            "mode": "ssh",
                            "label": format!("{user}@{host}:{port}"),
                        })));
                    }
                    Err(e) => {
                        let _ = ctrl_tx.send(Ctrl::Json(json!({
                            "type": "error",
                            "msg": format!("SSH 连接失败：{e}"),
                        })));
                    }
                }
            }
            _ => {}
        }
    }

    // 收尾：关闭所有会话并给前端发 close 帧
    if let Some(s) = current.lock().unwrap().take() {
        close_session(&s);
    }
    let _ = ctrl_tx.send(Ctrl::Close);
}

pub fn ws_routes() -> Router<AppState> {
    Router::new()
        .route("/ws/instances/{instance_id}/logs", get(logs_handler))
        .route("/ws/instances/{instance_id}/exec", get(exec_handler))
        .route("/ws/docker/pull", get(pull_handler))
        .route("/ws/docker/term", get(term_handler))
        .route("/ws/host/term", get(host_term_handler))
}

// =====================================================================
// 独立面板 WebSocket（/panel/ws/*，?ptoken= 鉴权）
// =====================================================================

#[derive(Debug, Deserialize, Default)]
struct PanelWsQuery {
    #[serde(default)]
    ptoken: Option<String>,
    #[serde(default)]
    tail: Option<String>,
    #[serde(default)]
    job_id: Option<String>,
}

/// 面板令牌 → 实例（令牌吊销或重置即失效）；失败返回 None。
async fn panel_inst_of(state: &AppState, q: &PanelWsQuery) -> Option<crate::models::Instance> {
    let token = q.ptoken.clone().unwrap_or_default();
    let (iid, fp) = crate::auth::decode_panel_session(&token)?;
    let inst = fetch_instance(&state.db, iid).await.ok()?;
    let cur = inst.panel_token.as_deref().unwrap_or("");
    if cur.is_empty() || fp.as_deref().unwrap_or("") != crate::auth::panel_token_fp(cur) {
        return None;
    }
    Some(inst)
}

fn panel_expired(inst: &crate::models::Instance) -> bool {
    match inst.expire_at {
        Some(e) => crate::models::utcnow() >= e,
        None => false,
    }
}

async fn panel_logs_handler(
    State(state): State<AppState>,
    Query(q): Query<PanelWsQuery>,
    ws: WebSocketUpgrade,
) -> Response {
    let Some(inst) = panel_inst_of(&state, &q).await else {
        return forbidden();
    };
    let tail = q
        .tail
        .as_deref()
        .and_then(|s| s.parse::<i64>().ok())
        .unwrap_or(200);
    ws.on_upgrade(move |socket| logs_task(socket, state, inst.id, tail))
}

async fn panel_install_handler(
    State(state): State<AppState>,
    Query(q): Query<PanelWsQuery>,
    ws: WebSocketUpgrade,
) -> Response {
    let Some(inst) = panel_inst_of(&state, &q).await else {
        return forbidden();
    };
    let job_id = q.job_id.clone().unwrap_or_default();
    ws.on_upgrade(move |socket| exec_task(socket, state, inst.id, job_id))
}

async fn panel_term_handler(
    State(state): State<AppState>,
    Query(q): Query<PanelWsQuery>,
    ws: WebSocketUpgrade,
) -> Response {
    let Some(inst) = panel_inst_of(&state, &q).await else {
        return forbidden();
    };
    // 终端属写操作，到期即拒（日志 WS 只读可继续用）
    if panel_expired(&inst) {
        return StatusCode::FORBIDDEN.into_response();
    }
    ws.on_upgrade(move |socket| panel_term_task(socket, state, inst))
}

/// 容器内交互式终端：docker exec PTY 流（xterm.js 前端）。
/// 协议：收 {"type":"input","data"} / {"type":"resize","cols","rows"}；
///       发 {"type":"data","data"} / {"type":"closed"}。
async fn panel_term_task(socket: WebSocket, state: AppState, inst: crate::models::Instance) {
    use bollard::exec::{CreateExecOptions, ResizeExecOptions, StartExecOptions, StartExecResults};

    let (mut sink, mut stream) = socket.split();
    let (msg_tx, mut msg_rx) = mpsc::unbounded_channel::<Message>();
    tokio::spawn(async move {
        while let Some(m) = msg_rx.recv().await {
            if sink.send(m).await.is_err() {
                break;
            }
        }
    });

    let Ok(docker) = state.docker.get().await else {
        let _ = msg_tx.send(json_msg(&json!({"type": "error", "msg": "Docker 服务不可用"})));
        let _ = msg_tx.send(json_msg(&json!({"type": "closed"})));
        return;
    };
    // 提示符随 cd 实时变化；优先 bash：--norc 保留自定义 PS1，-i 启用 readline
    // 行编辑（↑↓ 历史命令、Tab 补全）；Debian 系镜像均自带 bash，
    // Alpine 无 bash 时回落 busybox ash（busybox ash 自带行编辑）。
    let inner = format!(
        "export TERM=xterm-256color PS1='app@{}:$PWD\\$ '; \
         if [ -x /bin/bash ]; then exec /bin/bash --norc -i; else exec /bin/sh; fi",
        inst.name
    );
    let created = docker
        .create_exec(
            &container_name(inst.id),
            CreateExecOptions {
                cmd: Some(vec!["/bin/sh".into(), "-c".into(), inner]),
                attach_stdin: Some(true),
                attach_stdout: Some(true),
                attach_stderr: Some(true),
                tty: Some(true),
                working_dir: Some("/app".to_string()),
                ..Default::default()
            },
        )
        .await;
    let exec_id = match created {
        Ok(e) => e.id,
        Err(e) => {
            let _ = msg_tx.send(json_msg(&json!({"type": "error", "msg": e.to_string()})));
            let _ = msg_tx.send(json_msg(&json!({"type": "closed"})));
            return;
        }
    };
    let started = docker
        .start_exec(
            &exec_id,
            Some(StartExecOptions {
                detach: false,
                tty: true,
                output_capacity: Some(64 * 1024),
            }),
        )
        .await;
    let (mut output, mut input) = match started {
        Ok(StartExecResults::Attached { output, input }) => (output, input),
        Ok(_) => {
            let _ = msg_tx.send(json_msg(&json!({"type": "error", "msg": "容器未运行"})));
            let _ = msg_tx.send(json_msg(&json!({"type": "closed"})));
            return;
        }
        Err(e) => {
            let _ = msg_tx.send(json_msg(&json!({"type": "error", "msg": e.to_string()})));
            let _ = msg_tx.send(json_msg(&json!({"type": "closed"})));
            return;
        }
    };

    // 输出泵：exec 流 → {"type":"data"}；EOF 后发 closed
    let out_tx = msg_tx.clone();
    tokio::spawn(async move {
        while let Some(item) = output.next().await {
            match item {
                Ok(line) => {
                    let text = match line {
                        bollard::container::LogOutput::StdOut { message }
                        | bollard::container::LogOutput::StdErr { message }
                        | bollard::container::LogOutput::Console { message }
                        | bollard::container::LogOutput::StdIn { message } => {
                            String::from_utf8_lossy(&message).to_string()
                        }
                    };
                    if !text.is_empty()
                        && out_tx
                            .send(json_msg(&json!({"type": "data", "data": text})))
                            .is_err()
                    {
                        return;
                    }
                }
                Err(_) => break,
            }
        }
        let _ = out_tx.send(json_msg(&json!({"type": "closed"})));
    });

    // 输入泵 + 尺寸自适应
    let (in_tx, mut in_rx) = mpsc::unbounded_channel::<Vec<u8>>();
    tokio::spawn(async move {
        use tokio::io::AsyncWriteExt;
        while let Some(data) = in_rx.recv().await {
            if input.write_all(&data).await.is_err() {
                break;
            }
            let _ = input.flush().await;
        }
    });

    while let Some(Ok(msg)) = stream.next().await {
        match msg {
            Message::Text(t) => {
                let v: Value = serde_json::from_str(&t).unwrap_or_else(|_| json!({}));
                match v.get("type").and_then(|x| x.as_str()) {
                    Some("input") => {
                        let data = v.get("data").and_then(|x| x.as_str()).unwrap_or("");
                        let _ = in_tx.send(data.as_bytes().to_vec());
                    }
                    Some("resize") => {
                        let (cols, rows) = dims(&v);
                        let _ = docker
                            .resize_exec(
                                &exec_id,
                                ResizeExecOptions { height: rows, width: cols },
                            )
                            .await;
                    }
                    _ => {}
                }
            }
            Message::Close(_) => break,
            _ => {}
        }
    }
}

// ---------- 宿主机终端（行模式，仅 admin） ----------

#[derive(Debug, Deserialize, Default)]
struct HostTermQuery {
    #[serde(default)]
    token: String,
}

/// 宿主机名：优先环境变量（容器内由运行时注入），退化到 /etc/hostname，最后兜底。
fn host_name() -> String {
    if let Ok(h) = std::env::var("HOSTNAME") {
        if !h.trim().is_empty() {
            return h;
        }
    }
    if let Ok(h) = std::env::var("COMPUTERNAME") {
        if !h.trim().is_empty() {
            return h;
        }
    }
    std::fs::read_to_string("/etc/hostname")
        .map(|s| s.trim().to_string())
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "docker-host".to_string())
}

/// `?token=<JWT>` 鉴权并校验 admin 角色；失败返回应使用的 WS 关闭码。
async fn host_term_auth(state: &AppState, token: &str) -> Result<String, u16> {
    let uid = decode_user_token(token).ok_or(4401u16)?;
    let row: Option<(String, String)> =
        sqlx::query_as("SELECT username, role FROM users WHERE id = ?")
            .bind(uid)
            .fetch_optional(&state.db)
            .await
            .ok()
            .flatten();
    let (username, role) = row.ok_or(4401u16)?;
    if role != "admin" {
        return Err(4403);
    }
    Ok(username)
}

async fn host_term_handler(
    State(state): State<AppState>,
    Query(q): Query<HostTermQuery>,
    ws: WebSocketUpgrade,
) -> Response {
    match host_term_auth(&state, &q.token).await {
        Ok(user) => {
            let host = host_name();
            ws.on_upgrade(move |s| host_term_task(s, user, host))
        }
        Err(code) => ws.on_upgrade(move |mut s| async move { close_code(&mut s, code).await }),
    }
}

/// 收 `{cmd}`，回 `{type:"out", out, cwd, prompt}`（与 Python 版逐字段一致）。
async fn host_term_task(mut socket: WebSocket, username: String, host: String) {
    let hello = json!({
        "type": "hello",
        "user": username,
        "host": host,
        "cwd": "~",
    });
    if socket.send(json_msg(&hello)).await.is_err() {
        return;
    }

    let mut cwd = String::new();
    while let Some(msg) = socket.recv().await {
        let text = match msg {
            Ok(Message::Text(t)) => t.to_string(),
            Ok(Message::Close(_)) => break,
            Ok(_) => continue,
            Err(_) => break,
        };
        let v: Value = match serde_json::from_str(&text) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let cmd = v
            .get("cmd")
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if cmd.is_empty() {
            continue;
        }
        let (out, next_cwd) = match host_ops::run_host_command(&cmd, &cwd).await {
            Ok(pair) => pair,
            Err(e) => (format!("[终端] 执行出错：{}", e.detail), cwd.clone()),
        };
        cwd = next_cwd;
        let prompt = format!(
            "{}@{}:{}$",
            username,
            host,
            if cwd.is_empty() { "~" } else { cwd.as_str() }
        );
        let payload = json!({
            "type": "out",
            "out": out,
            "cwd": cwd,
            "prompt": prompt,
        });
        if socket.send(json_msg(&payload)).await.is_err() {
            break;
        }
    }
}

/// 独立面板 WS 路由（挂在根下，路径自带 /panel 前缀）。
pub fn panel_ws_routes() -> Router<AppState> {
    Router::new()
        .route("/panel/ws/logs", get(panel_logs_handler))
        .route("/panel/ws/install", get(panel_install_handler))
        .route("/panel/ws/term", get(panel_term_handler))
}
