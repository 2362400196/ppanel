//! 宿主机操作：daemon.json 读写、dockerd 重启、宿主机命令执行、
//! 防火墙（ufw / firewalld）与 fail2ban（SSH 防爆破）封装。
//! 全部走 `tokio::process::Command`，返回结构化结果；行为与 Python 版 app/services/host_ops.py
//! 及 app/routers/security.py、app/routers/agent.py 的防火墙实现逐行对齐。

use std::process::Stdio;
use std::time::Duration;

use regex::Regex;
use serde_json::{json, Value};

use crate::error::{AppError, AppResult};
use crate::state::{AppState, TaskLog};

pub const DAEMON_JSON: &str = "/etc/docker/daemon.json";
pub const JAIL_CONF: &str = "/etc/fail2ban/jail.d/ppanel-sshd.local";

// ---------- 运行形态 ----------

/// local：面板与 dockerd 同机（Linux）；wsl：面板在 Windows、dockerd 在 WSL（开发态）。
pub fn host_kind() -> &'static str {
    if cfg!(windows) {
        "wsl"
    } else {
        "local"
    }
}

/// INNER_SHELL_MODE：auto（Win→WSL）/ local / wsl。
fn shell_mode() -> String {
    std::env::var("INNER_SHELL_MODE").unwrap_or_else(|_| "auto".to_string())
}

fn use_wsl() -> bool {
    match shell_mode().as_str() {
        "wsl" => true,
        "auto" => cfg!(windows),
        _ => false,
    }
}

/// 在 WSL 内以 root 执行命令（仅开发态使用）；失败返回空串。
async fn wsl_root(cmd: &str, timeout: u64) -> String {
    let mut c = tokio::process::Command::new("wsl");
    c.args(["-u", "root", "-e", "bash", "-c", cmd]);
    c.stdout(Stdio::piped()).stderr(Stdio::piped()).kill_on_drop(true);
    let child = match c.spawn() {
        Ok(x) => x,
        Err(_) => return String::new(),
    };
    match tokio::time::timeout(Duration::from_secs(timeout), child.wait_with_output()).await {
        Ok(Ok(out)) => String::from_utf8_lossy(&out.stdout).to_string(),
        _ => String::new(),
    }
}

// ---------- daemon.json ----------

pub async fn read_daemon_json() -> Value {
    if host_kind() == "wsl" {
        let raw = wsl_root(&format!("cat {DAEMON_JSON} 2>/dev/null"), 30).await;
        if raw.trim().is_empty() {
            return json!({});
        }
        return serde_json::from_str(&raw).unwrap_or_else(|_| json!({}));
    }
    match std::fs::read_to_string(DAEMON_JSON) {
        Ok(raw) => serde_json::from_str(&raw).unwrap_or_else(|_| json!({})),
        Err(_) => json!({}),
    }
}

pub async fn write_daemon_json(data: &Value) -> AppResult<()> {
    let text = serde_json::to_string_pretty(data).unwrap_or_else(|_| "{}".to_string());
    if host_kind() == "wsl" {
        use base64::Engine;
        let b64 = base64::engine::general_purpose::STANDARD.encode(text.as_bytes());
        let out = wsl_root(
            &format!(
                "cp {DAEMON_JSON} {DAEMON_JSON}.bak 2>/dev/null; \
                 mkdir -p /etc/docker && echo {b64} | base64 -d > {DAEMON_JSON} && cat {DAEMON_JSON}"
            ),
            30,
        )
        .await;
        if !out.contains('{') {
            return Err(AppError::internal("写入 WSL /etc/docker/daemon.json 失败"));
        }
        return Ok(());
    }
    if let Some(parent) = std::path::Path::new(DAEMON_JSON).parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let _ = std::fs::copy(DAEMON_JSON, format!("{DAEMON_JSON}.bak"));
    std::fs::write(DAEMON_JSON, text)?;
    Ok(())
}

/// 重启 dockerd 使 daemon.json 生效（面板与 Docker 的连接会短暂中断）。
pub async fn restart_dockerd() {
    if host_kind() == "wsl" {
        wsl_root("systemctl restart docker", 90).await;
        return;
    }
    let (rc, _) = run(&["systemctl", "restart", "docker"], 90).await;
    if rc != 0 {
        let _ = run(&["service", "docker", "restart"], 90).await;
    }
}

// ---------- 通用命令执行 ----------

/// systemd 环境 PATH 可能缺 /usr/sbin，兜底常见 sbin 路径（对应 Python `_resolve`）。
pub fn resolve_bin(binname: &str) -> String {
    if binname.contains('/') || binname.contains('\\') {
        return binname.to_string();
    }
    if let Ok(path) = std::env::var("PATH") {
        for dir in std::env::split_paths(&path) {
            let cand = dir.join(binname);
            if cand.is_file() {
                return cand.to_string_lossy().to_string();
            }
            if cfg!(windows) {
                for ext in ["exe", "bat", "cmd"] {
                    let c = dir.join(format!("{binname}.{ext}"));
                    if c.is_file() {
                        return c.to_string_lossy().to_string();
                    }
                }
            }
        }
    }
    for pre in ["/usr/sbin", "/sbin", "/usr/local/sbin"] {
        let cand = format!("{pre}/{binname}");
        if std::path::Path::new(&cand).exists() {
            return cand;
        }
    }
    binname.to_string()
}

/// 执行命令，返回 (退出码, stdout+stderr 文本)。
/// 与 Python `_run` 一致：找不到命令 → 127/"command not found"；超时 → 124/"命令超时"。
pub async fn run(cmd: &[&str], timeout: u64) -> (i32, String) {
    run_env(cmd, timeout, &[]).await
}

pub async fn run_env(cmd: &[&str], timeout: u64, envs: &[(&str, &str)]) -> (i32, String) {
    if cmd.is_empty() {
        return (127, "command not found".to_string());
    }
    let bin = resolve_bin(cmd[0]);
    let mut c = tokio::process::Command::new(bin);
    c.args(&cmd[1..]);
    for (k, v) in envs {
        c.env(k, v);
    }
    c.stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let child = match c.spawn() {
        Ok(x) => x,
        Err(_) => return (127, "command not found".to_string()),
    };
    match tokio::time::timeout(Duration::from_secs(timeout), child.wait_with_output()).await {
        Err(_) => (124, "命令超时".to_string()),
        Ok(Err(_)) => (127, "command not found".to_string()),
        Ok(Ok(out)) => {
            let code = out.status.code().unwrap_or(-1);
            let mut text = String::from_utf8_lossy(&out.stdout).to_string();
            text.push_str(&String::from_utf8_lossy(&out.stderr));
            (code, text)
        }
    }
}

/// 截断到 n 个字符（Python 切片语义）。
fn truncate(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// 取末尾 n 个字符（Python `[-n:]`），供包管理器报错信息使用。
fn tail_chars(s: &str, n: usize) -> String {
    let total = s.chars().count();
    s.chars().skip(total.saturating_sub(n)).collect()
}

/// 失败详情：`(out or fallback).strip()[:300]`。
fn fail_detail(out: &str, fallback: &str) -> String {
    let base = if out.is_empty() { fallback } else { out };
    truncate(base.trim(), 300)
}

// ---------- 宿主机 shell（终端用） ----------

pub const TERM_TIMEOUT: u64 = 120;

fn shell_quote(v: &str) -> String {
    format!("'{}'", v.replace('\'', "'\\''"))
}

/// 拆掉脚本附加的 @@PWD@@ 标记行，返回 (正文, 新cwd)。
fn parse_pwd(out: &str) -> (String, String) {
    let mut lines: Vec<&str> = out.split('\n').map(|l| l.trim_end_matches('\r')).collect();
    while lines.last().map(|l| l.trim().is_empty()).unwrap_or(false) {
        lines.pop();
    }
    let mut pwd = String::new();
    if let Some(last) = lines.last() {
        if let Some(rest) = last.strip_prefix("@@PWD@@") {
            pwd = rest.trim().to_string();
            lines.pop();
        }
    }
    (lines.join("\n"), pwd)
}

/// 在宿主机（或 WSL）执行一条 shell 命令，返回 (输出, 新cwd)。
pub async fn run_host_command(cmd: &str, cwd: &str) -> AppResult<(String, String)> {
    if cmd.chars().count() > 4000 {
        return Err(AppError::bad_request("命令过长"));
    }
    let quiet_cwd = if cwd.is_empty() {
        "~".to_string()
    } else {
        shell_quote(cwd)
    };
    let script = format!(
        "cd {quiet_cwd} 2>/dev/null || cd ~\n{cmd}\n__ppanel_rc=$?\nprintf \"\\n@@PWD@@%s\\n\" \"$(pwd)\"\nexit $__ppanel_rc"
    );
    let mut c = if use_wsl() {
        let mut c = tokio::process::Command::new("wsl");
        c.args(["-e", "bash", "-c", &script]);
        c
    } else {
        let mut c = tokio::process::Command::new(resolve_bin("bash"));
        c.args(["-c", &script]);
        c
    };
    c.stdout(Stdio::piped()).stderr(Stdio::piped()).kill_on_drop(true);
    let child = c
        .spawn()
        .map_err(|e| AppError::new(503, format!("无法在宿主机创建 shell：{e}")))?;
    match tokio::time::timeout(Duration::from_secs(TERM_TIMEOUT), child.wait_with_output()).await {
        Err(_) => Ok((
            "[终端] 命令执行超时（120s），已终止".to_string(),
            cwd.to_string(),
        )),
        Ok(Err(e)) => Err(AppError::new(503, format!("无法在宿主机创建 shell：{e}"))),
        Ok(Ok(out)) => {
            let mut text = String::from_utf8_lossy(&out.stdout).to_string();
            text.push_str(&String::from_utf8_lossy(&out.stderr));
            let (body, new_cwd) = parse_pwd(&text);
            Ok((body, if new_cwd.is_empty() { cwd.to_string() } else { new_cwd }))
        }
    }
}

// ---------- fail2ban（SSH 防爆破） ----------

/// server 监听的 socket：读 fail2ban.conf 的 socket= 行（宝塔等面板会改到私有路径）。
pub fn f2b_socket() -> String {
    if let Ok(text) = std::fs::read_to_string("/etc/fail2ban/fail2ban.conf") {
        for line in text.lines() {
            let line = line.trim();
            if line.starts_with("socket") && !line.starts_with('#') {
                if let Some((_, v)) = line.split_once('=') {
                    let v = v.trim();
                    if !v.is_empty() {
                        return v.to_string();
                    }
                }
            }
        }
    }
    "/var/run/fail2ban/fail2ban.sock".to_string()
}

pub fn f2b_installed() -> bool {
    let p = resolve_bin("fail2ban-client");
    std::path::Path::new(&p).is_file()
}

pub async fn f2b(args: &[&str], timeout: u64) -> (i32, String) {
    let mut cmd: Vec<String> = vec![
        "fail2ban-client".to_string(),
        "-s".to_string(),
        f2b_socket(),
    ];
    cmd.extend(args.iter().map(|a| a.to_string()));
    let refs: Vec<&str> = cmd.iter().map(|s| s.as_str()).collect();
    run(&refs, timeout).await
}

async fn f2b_version() -> String {
    let (rc, out) = run(&["fail2ban-client", "--version"], 8).await;
    if rc != 0 {
        return String::new();
    }
    out.trim()
        .split('\n')
        .next()
        .unwrap_or("")
        .trim_end_matches('\r')
        .to_string()
}

pub async fn f2b_active() -> bool {
    let (rc, out) = run(&["systemctl", "is-active", "fail2ban"], 8).await;
    rc == 0 && out.trim() == "active"
}

/// fail2ban 安装/运行状态 + sshd jail 封禁情况。
pub async fn fail2ban_status() -> Value {
    let installed = f2b_installed();
    let mut out = json!({
        "installed": installed,
        "active": false,
        "version": "",
        "jails": [],
        "sshd": Value::Null,
        "error": Value::Null,
    });
    if !installed {
        return out;
    }
    out["version"] = json!(f2b_version().await);
    let active = f2b_active().await;
    out["active"] = json!(active);
    if !active {
        out["error"] = json!("fail2ban 服务未运行，可点击「安装/启用」修复");
        return out;
    }
    let (rc, text) = f2b(&["status"], 10).await;
    if rc == 0 {
        let re = Regex::new(r"(?m)Jail list:\s*(\S.*)$").unwrap();
        let jails: Vec<String> = match re.captures(&text) {
            Some(c) => c
                .get(1)
                .map(|m| m.as_str())
                .unwrap_or("")
                .split(',')
                .map(|j| j.trim().to_string())
                .collect(),
            None => Vec::new(),
        };
        out["jails"] = json!(jails);
    }
    out["sshd"] = sshd_status().await;
    out
}

/// 解析 fail2ban-client status sshd 的失败计数与封禁列表。
pub async fn sshd_status() -> Value {
    let (rc, text) = f2b(&["status", "sshd"], 10).await;
    if rc != 0 {
        let lines: Vec<&str> = text
            .trim()
            .split('\n')
            .map(|l| l.trim_end_matches('\r'))
            .filter(|l| !l.trim().is_empty())
            .collect();
        let last = match lines.last() {
            Some(l) => l.to_string(),
            None => "sshd jail 未启用".to_string(),
        };
        return json!({"enabled": false, "error": truncate(&last, 200)});
    }
    let num = |name: &str| -> i64 {
        let re = Regex::new(&format!(r"{name}:\s*(\d+)")).unwrap();
        re.captures(&text)
            .and_then(|c| c.get(1))
            .and_then(|m| m.as_str().parse::<i64>().ok())
            .unwrap_or(0)
    };
    let banned: Vec<String> = {
        let re = Regex::new(r"Banned IP list:\s*(.*)").unwrap();
        match re.captures(&text) {
            Some(c) => c
                .get(1)
                .map(|m| m.as_str())
                .unwrap_or("")
                .split_whitespace()
                .map(|s| s.to_string())
                .collect(),
            None => Vec::new(),
        }
    };
    json!({
        "enabled": true,
        "error": Value::Null,
        "currently_failed": num("Currently failed"),
        "total_failed": num("Total failed"),
        "currently_banned": num("Currently banned"),
        "total_banned": num("Total banned"),
        "banned": banned,
    })
}

/// 包管理器安装 fail2ban。返回 None=成功，否则错误信息。
async fn install_fail2ban(log: &TaskLog, tid: &str) -> Option<String> {
    let apt = resolve_bin("apt-get");
    if std::path::Path::new(&apt).is_file() {
        let mut last_err = String::new();
        for attempt in 1..=2 {
            if !tid.is_empty() {
                log.log(
                    tid,
                    &format!("apt 安装 fail2ban（第 {attempt} 次尝试，最长 5 分钟）…"),
                );
            }
            let (rc, out) = run_env(
                &[apt.as_str(), "install", "-y", "-qq", "fail2ban"],
                300,
                &[("DEBIAN_FRONTEND", "noninteractive")],
            )
            .await;
            if rc == 0 {
                return None;
            }
            last_err = out;
            if attempt == 1 && !tid.is_empty() {
                log.log(tid, "安装失败，apt update 刷新索引后重试…");
            }
            if attempt == 1 {
                let _ = run(&[apt.as_str(), "update", "-qq"], 180).await;
            }
        }
        let tail = tail_chars(last_err.trim(), 300);
        return Some(if tail.is_empty() {
            "apt 安装失败".to_string()
        } else {
            tail
        });
    }
    for mgr in ["dnf", "yum"] {
        let path = resolve_bin(mgr);
        if !std::path::Path::new(&path).is_file() {
            continue;
        }
        if !tid.is_empty() {
            log.log(tid, &format!("{mgr} 安装 epel-release 与 fail2ban…"));
        }
        let _ = run(&[path.as_str(), "install", "-y", "epel-release"], 300).await;
        let (rc, out) = run(&[path.as_str(), "install", "-y", "fail2ban"], 300).await;
        return if rc == 0 {
            None
        } else {
            let tail = tail_chars(out.trim(), 300);
            Some(if tail.is_empty() {
                "安装失败".to_string()
            } else {
                tail
            })
        };
    }
    Some("未识别的包管理器（需要 apt/dnf/yum）".to_string())
}

/// 安装（如缺）fail2ban + 写入 sshd 防爆破策略（失败 5 次封 10 分钟）+ 启用。
pub async fn fail2ban_setup(state: &AppState, tid: &str) -> AppResult<Value> {
    if !tid.is_empty() {
        state.tasks.start(tid, "安装/启用 fail2ban（SSH 防爆破）");
    }
    let res = setup_inner(state, tid).await;
    match &res {
        Ok(_) => {
            if !tid.is_empty() {
                state.tasks.finish(tid, true, "✔ fail2ban 已安装并启用");
            }
        }
        Err(_) => {
            if !tid.is_empty() {
                state.tasks.finish(tid, false, "✘ 设置失败：");
            }
        }
    }
    res
}

async fn setup_inner(state: &AppState, tid: &str) -> AppResult<Value> {
    if !f2b_installed() {
        if !tid.is_empty() {
            state.tasks.log(tid, "未检测到 fail2ban，开始安装…");
        }
        if let Some(err) = install_fail2ban(&state.tasks, tid).await {
            return Err(AppError::bad_gateway(format!("fail2ban 安装失败：{err}")));
        }
    }
    if !tid.is_empty() {
        state
            .tasks
            .log(tid, "写入 sshd 防爆破策略（失败 5 次封 10 分钟）…");
    }
    if let Some(parent) = std::path::Path::new(JAIL_CONF).parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut active = false;
    // 先用 systemd backend（现代发行版无 auth.log 也能取日志）；服务起不来则降级重试一次
    for backend in ["backend = systemd\n", ""] {
        let body = format!(
            "# PPanel SSH 防爆破策略（由节点面板管理）\n[sshd]\nenabled = true\n{backend}maxretry = 5\nfindtime = 10m\nbantime = 10m\n"
        );
        std::fs::write(JAIL_CONF, body)?;
        let _ = run(&["systemctl", "enable", "fail2ban"], 30).await;
        let _ = run(&["systemctl", "restart", "fail2ban"], 30).await;
        // systemd Type=simple 启动即 active，初始化失败要等一会才暴露，不能立刻判定
        tokio::time::sleep(Duration::from_millis(2500)).await;
        if f2b_active().await {
            active = true;
            break;
        }
    }
    if !active {
        return Err(AppError::bad_gateway(
            "fail2ban 已安装但服务启动失败，请检查 journalctl -u fail2ban（sshd jail 日志源不可用）",
        ));
    }
    Ok(fail2ban_status().await)
}

// ---------- 防火墙（ufw / firewalld 自动探测，列表参数防注入） ----------

pub async fn detect_tool() -> AppResult<String> {
    if run(&["ufw", "version"], 15).await.0 == 0 {
        return Ok("ufw".to_string());
    }
    if run(&["firewall-cmd", "--version"], 15).await.0 == 0 {
        return Ok("firewalld".to_string());
    }
    Err(AppError::bad_request(
        "节点未安装防火墙工具（ufw / firewalld），请先安装：apt install -y ufw 或 yum install -y firewalld",
    ))
}

/// 端口或端口段（30000:39999）白名单校验。
pub fn check_port(port: &str) -> AppResult<()> {
    let re = Regex::new(r"\d{1,5}(:\d{1,5})?").unwrap();
    if !re.is_match(port) || re.find(port).map(|m| m.as_str()) != Some(port) {
        return Err(AppError::bad_request("端口格式无效"));
    }
    let parts: Vec<i64> = port
        .split(':')
        .filter_map(|p| p.parse::<i64>().ok())
        .collect();
    if parts.iter().any(|p| *p < 1 || *p > 65535) || (parts.len() == 2 && parts[0] > parts[1]) {
        return Err(AppError::bad_request("端口范围无效"));
    }
    Ok(())
}

pub fn check_proto(proto: &str) -> AppResult<()> {
    if proto != "tcp" && proto != "udp" {
        return Err(AppError::bad_request("协议仅支持 tcp/udp"));
    }
    Ok(())
}

pub fn check_ip(ip: &str) -> AppResult<()> {
    let parts: Vec<&str> = ip.split('.').collect();
    if parts.len() == 4
        && parts
            .iter()
            .all(|p| p.bytes().all(|b| b.is_ascii_digit()) && p.parse::<u16>().map(|n| n <= 255).unwrap_or(false))
    {
        return Ok(());
    }
    if ip.contains(':') {
        let re = Regex::new(r"[0-9A-Fa-f:]{2,45}").unwrap();
        if re.is_match(ip) && re.find(ip).map(|m| m.as_str()) == Some(ip) {
            return Ok(());
        }
    }
    Err(AppError::bad_request("IP 格式无效"))
}

/// v4/v6 镜像规则合并为一条，nums 保留全部编号（删除时一起删）。
fn parse_ufw(out: &str) -> Vec<Value> {
    let pat = Regex::new(
        r"^\[\s*(\d+)\]\s+(\S+)(?:\s+\(v6\))?\s+(ALLOW|DENY|REJECT|LIMIT)(?:\s+IN)?\s+(.+?)\s*$",
    )
    .unwrap();
    let mut order: Vec<Value> = Vec::new();
    for line in out.split('\n') {
        let line = line.trim_end_matches('\r');
        let Some(c) = pat.captures(line) else { continue };
        let num: i64 = c.get(1).map(|m| m.as_str()).unwrap_or("0").parse().unwrap_or(0);
        let port = c.get(2).map(|m| m.as_str()).unwrap_or("").to_string();
        let action = c.get(3).map(|m| m.as_str()).unwrap_or("").to_string();
        let frm = c.get(4).map(|m| m.as_str()).unwrap_or("").trim().to_string();
        if let Some(item) = order.iter_mut().find(|v| {
            v["port"].as_str() == Some(port.as_str()) && v["action"].as_str() == Some(action.as_str())
        }) {
            if let Some(nums) = item["nums"].as_array_mut() {
                nums.push(json!(num));
            }
        } else {
            order.push(json!({"port": port, "action": action, "from": frm, "nums": [num]}));
        }
    }
    order
}

async fn parse_firewalld() -> Vec<Value> {
    let mut rules: Vec<Value> = Vec::new();
    let (rc, out) = run(&["firewall-cmd", "--permanent", "--list-ports"], 15).await;
    if rc == 0 {
        for item in out.split_whitespace() {
            rules.push(json!({"num": 0, "port": item, "action": "ALLOW", "from": "Anywhere"}));
        }
    }
    let (rc, out) = run(&["firewall-cmd", "--permanent", "--list-rich-rules"], 15).await;
    if rc == 0 {
        for line in out.split('\n').map(|l| l.trim().trim_end_matches('\r')) {
            if line.is_empty() {
                continue;
            }
            rules.push(json!({"num": 0, "port": line, "action": "RULE", "from": "-"}));
        }
    }
    rules
}

pub async fn firewall_status() -> Value {
    let mut tool: Option<String> = None;
    if run(&["ufw", "version"], 15).await.0 == 0 {
        tool = Some("ufw".to_string());
    } else if run(&["firewall-cmd", "--version"], 15).await.0 == 0 {
        tool = Some("firewalld".to_string());
    }

    let mut rules: Vec<Value> = Vec::new();
    let mut active = false;
    let mut err: Option<String> = None;
    match tool.as_deref() {
        Some("ufw") => {
            let (rc, out) = run(&["ufw", "status", "numbered"], 15).await;
            active = rc == 0
                && !out.is_empty()
                && out
                    .split('\n')
                    .next()
                    .map(|l| l.contains("active"))
                    .unwrap_or(false);
            if rc == 0 {
                rules = parse_ufw(&out);
            } else {
                let t = out.trim().to_string();
                err = Some(if t.is_empty() { "ufw 执行失败".to_string() } else { t });
            }
        }
        Some("firewalld") => {
            let (rc, out) = run(&["firewall-cmd", "--state"], 15).await;
            active = rc == 0 && out.contains("running");
            if active {
                rules = parse_firewalld().await;
            } else {
                err = Some("firewalld 服务未运行".to_string());
            }
        }
        _ => {}
    }
    json!({"tool": tool, "active": active, "rules": rules, "error": err})
}

pub async fn firewall_allow(port: &str, proto: &str) -> AppResult<Value> {
    check_port(port)?;
    check_proto(proto)?;
    let (rc, out) = if detect_tool().await? == "ufw" {
        run(&["ufw", "allow", &format!("{port}/{proto}")], 15).await
    } else {
        let (rc, out) = run(
            &["firewall-cmd", "--permanent", &format!("--add-port={port}/{proto}")],
            15,
        )
        .await;
        if rc == 0 {
            run(&["firewall-cmd", "--reload"], 15).await
        } else {
            (rc, out)
        }
    };
    if rc != 0 {
        return Err(AppError::bad_gateway(fail_detail(&out, "执行失败")));
    }
    Ok(json!({"ok": true, "detail": format!("已放行 {port}/{proto}")}))
}

pub async fn firewall_deny(port: &str, proto: &str) -> AppResult<Value> {
    check_port(port)?;
    check_proto(proto)?;
    let (rc, out) = if detect_tool().await? == "ufw" {
        run(&["ufw", "deny", &format!("{port}/{proto}")], 15).await
    } else {
        let (rc, out) = run(
            &[
                "firewall-cmd",
                "--permanent",
                &format!("--remove-port={port}/{proto}"),
            ],
            15,
        )
        .await;
        if rc == 0 {
            run(&["firewall-cmd", "--reload"], 15).await
        } else {
            (rc, out)
        }
    };
    if rc != 0 {
        return Err(AppError::bad_gateway(fail_detail(&out, "执行失败")));
    }
    Ok(json!({"ok": true, "detail": format!("已拒绝 {port}/{proto}")}))
}

pub async fn firewall_delete(
    nums: Vec<i64>,
    num: i64,
    port: &str,
    proto: &str,
) -> AppResult<Value> {
    let (rc, out) = if detect_tool().await? == "ufw" {
        let mut list: Vec<i64> = nums.into_iter().filter(|n| *n > 0).collect();
        if num > 0 {
            list.push(num);
        }
        if list.is_empty() {
            return Err(AppError::bad_request("缺少规则号"));
        }
        list.sort_unstable_by(|a, b| b.cmp(a)); // 从大到小删，避免编号偏移
        let mut rc = 0;
        let mut out = String::new();
        for n in list {
            let r = run(&["ufw", "--force", "delete", &n.to_string()], 15).await;
            rc = r.0;
            out = r.1;
            if rc != 0 {
                break;
            }
        }
        (rc, out)
    } else {
        check_port(port)?;
        check_proto(proto)?;
        let (rc, out) = run(
            &[
                "firewall-cmd",
                "--permanent",
                &format!("--remove-port={port}/{proto}"),
            ],
            15,
        )
        .await;
        if rc == 0 {
            run(&["firewall-cmd", "--reload"], 15).await
        } else {
            (rc, out)
        }
    };
    if rc != 0 {
        return Err(AppError::bad_gateway(fail_detail(&out, "执行失败")));
    }
    Ok(json!({"ok": true, "detail": "规则已删除"}))
}

pub async fn firewall_toggle(enable: bool) -> AppResult<Value> {
    let (rc, out) = if detect_tool().await? == "ufw" {
        run(
            &[
                "ufw",
                "--force",
                if enable { "enable" } else { "disable" },
            ],
            15,
        )
        .await
    } else {
        run(
            &[
                "systemctl",
                if enable { "start" } else { "stop" },
                "firewalld",
            ],
            15,
        )
        .await
    };
    if rc != 0 {
        return Err(AppError::bad_gateway(fail_detail(&out, "执行失败")));
    }
    Ok(json!({
        "ok": true,
        "detail": if enable { "防火墙已启用" } else { "防火墙已停用" },
    }))
}
