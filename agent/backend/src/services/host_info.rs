//! 宿主机系统信息采集（只读为主）：系统概览、进程、systemd 服务、监听端口、
//! 系统日志、登录用户。数据全部来自 `/proc`、`/etc` 与标准命令，不修改宿主状态。
//!
//! 设计约定：
//! - 非 Linux（开发态 Windows）下依赖 WSL 的命令会自然降级为空结果，不 panic；
//! - 所有命令经 `host_ops::run` 执行（统一超时、统一 binary 解析）；
//! - 返回字段一经确定即视为契约，供主控后台「节点运维」页面消费。

use std::collections::HashMap;
use std::time::Duration;

use serde_json::{json, Value};

use crate::error::{AppError, AppResult};
use crate::models::round1;
use crate::services::host_ops;

// ---------- 基础读取 ----------

fn read_file(path: &str) -> String {
    std::fs::read_to_string(path).unwrap_or_default()
}

/// 按空白切成 n 段，最后一段保留剩余内容（用于 ps 的 args 列）。
fn split_n(line: &str, n: usize) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let mut rest = line.trim_start();
    while out.len() + 1 < n {
        match rest.find(char::is_whitespace) {
            Some(i) => {
                out.push(rest[..i].to_string());
                rest = rest[i..].trim_start();
                if rest.is_empty() {
                    break;
                }
            }
            None => break,
        }
    }
    if !rest.is_empty() {
        out.push(rest.to_string());
    }
    out
}

/// 服务名 / 单元名校验：只允许 systemd 单元名合法字符，避免把任意串传给命令。
fn valid_unit(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_' | '@' | ':'))
}

fn meminfo() -> HashMap<String, u64> {
    let mut m = HashMap::new();
    for line in read_file("/proc/meminfo").lines() {
        let Some((k, v)) = line.split_once(':') else {
            continue;
        };
        let Some(first) = v.split_whitespace().next() else {
            continue;
        };
        if let Ok(n) = first.parse::<u64>() {
            m.insert(k.trim().to_string(), n * 1024);
        }
    }
    m
}

fn cpu_sample() -> Option<f64> {
    fn snap() -> Option<(u64, u64)> {
        let text = read_file("/proc/stat");
        let first = text.lines().next()?;
        let mut it = first.split_whitespace();
        if it.next()? != "cpu" {
            return None;
        }
        let nums: Vec<u64> = it.filter_map(|x| x.parse::<u64>().ok()).collect();
        if nums.len() < 4 {
            return None;
        }
        // idle + iowait 视为空闲
        let idle = nums[3] + nums.get(4).copied().unwrap_or(0);
        let total: u64 = nums.iter().sum();
        Some((idle, total))
    }
    let a = snap()?;
    std::thread::sleep(std::time::Duration::from_millis(150));
    let b = snap()?;
    let dt = b.1.saturating_sub(a.1);
    if dt == 0 {
        return None;
    }
    let di = b.0.saturating_sub(a.0);
    Some(round1((dt - di) as f64 / dt as f64 * 100.0))
}

fn cpu_model() -> String {
    for line in read_file("/proc/cpuinfo").lines() {
        if let Some((k, v)) = line.split_once(':') {
            if k.trim() == "model name" {
                return v.trim().to_string();
            }
        }
    }
    String::new()
}

fn os_pretty() -> String {
    for line in read_file("/etc/os-release").lines() {
        if let Some(v) = line.strip_prefix("PRETTY_NAME=") {
            return v.trim().trim_matches('"').to_string();
        }
    }
    String::new()
}

fn uptime_secs() -> u64 {
    read_file("/proc/uptime")
        .split_whitespace()
        .next()
        .and_then(|s| s.parse::<f64>().ok())
        .map(|f| f as u64)
        .unwrap_or(0)
}

fn fmt_duration(secs: u64) -> String {
    let d = secs / 86400;
    let h = (secs % 86400) / 3600;
    let m = (secs % 3600) / 60;
    if d > 0 {
        format!("{d} 天 {h} 小时 {m} 分")
    } else if h > 0 {
        format!("{h} 小时 {m} 分")
    } else {
        format!("{m} 分")
    }
}

// ---------- 系统概览 ----------

pub async fn system() -> Value {
    let (cpu_pct, cores) = tokio::task::spawn_blocking(|| {
        let pct = cpu_sample();
        let cores = std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(0);
        (pct, cores)
    })
    .await
    .unwrap_or((None, 0));

    let info = meminfo();
    let mt = info.get("MemTotal").copied().unwrap_or(0);
    let ma = info.get("MemAvailable").copied().unwrap_or(0);
    let mem = if mt > 0 {
        json!({
            "total": mt,
            "used": mt.saturating_sub(ma),
            "available": ma,
            "free": info.get("MemFree").copied().unwrap_or(0),
            "buffers": info.get("Buffers").copied().unwrap_or(0),
            "cached": info.get("Cached").copied().unwrap_or(0),
            "percent": round1((mt - ma) as f64 / mt as f64 * 100.0),
        })
    } else {
        Value::Null
    };
    let st = info.get("SwapTotal").copied().unwrap_or(0);
    let sf = info.get("SwapFree").copied().unwrap_or(0);
    let swap = json!({
        "total": st,
        "used": st.saturating_sub(sf),
        "percent": if st > 0 { round1((st - sf) as f64 / st as f64 * 100.0) } else { 0.0 },
    });

    let load_txt = read_file("/proc/loadavg");
    let lp: Vec<&str> = load_txt.split_whitespace().collect();
    let load = json!({
        "1": lp.first().and_then(|s| s.parse::<f64>().ok()),
        "5": lp.get(1).and_then(|s| s.parse::<f64>().ok()),
        "15": lp.get(2).and_then(|s| s.parse::<f64>().ok()),
        "running": lp.get(3).map(|s| s.split('/').next().unwrap_or("").to_string()),
        "total_procs": lp.get(3).and_then(|s| s.split('/').nth(1)).map(|s| s.to_string()),
    });

    let hostname = read_file("/proc/sys/kernel/hostname").trim().to_string();
    let (_, kernel) = host_ops::run(&["uname", "-r"], 5).await;
    let (_, arch) = host_ops::run(&["uname", "-m"], 5).await;

    let disks = {
        let (code, out) = host_ops::run(&["df", "-PT", "-k"], 10).await;
        if code != 0 {
            Vec::new()
        } else {
            out.lines()
                .skip(1)
                .filter_map(|line| {
                    let f = split_n(line, 7);
                    if f.len() < 7 {
                        return None;
                    }
                    let fs_type = f[1].clone();
                    if matches!(
                        fs_type.as_str(),
                        "tmpfs" | "devtmpfs" | "squashfs" | "proc" | "sysfs" | "overlay" | "none"
                    ) {
                        return None;
                    }
                    let size: u64 = f[2].parse().ok()?;
                    let used: u64 = f[3].parse().ok()?;
                    let avail: u64 = f[4].parse().ok()?;
                    let total = size * 1024;
                    let pct = if total > 0 {
                        round1(used as f64 * 1024.0 / total as f64 * 100.0)
                    } else {
                        0.0
                    };
                    Some(json!({
                        "device": f[0],
                        "fstype": fs_type,
                        "mount": f[6],
                        "total": total,
                        "used": used * 1024,
                        "available": avail * 1024,
                        "percent": pct,
                    }))
                })
                .collect::<Vec<Value>>()
        }
    };

    let net = read_file("/proc/net/dev")
        .lines()
        .skip(2)
        .filter_map(|line| {
            let (iface, rest) = line.split_once(':')?;
            let f: Vec<&str> = rest.split_whitespace().collect();
            if f.len() < 16 {
                return None;
            }
            let name = iface.trim();
            // 过滤回环与容器虚拟网卡：运维面板只关心真实链路
            if name == "lo"
                || name.starts_with("veth")
                || name.starts_with("br-")
                || name.starts_with("docker")
                || name.starts_with("virbr")
            {
                return None;
            }
            Some(json!({
                "iface": name,
                "rx_bytes": f[0].parse::<u64>().unwrap_or(0),
                "rx_packets": f[1].parse::<u64>().unwrap_or(0),
                "rx_errors": f[2].parse::<u64>().unwrap_or(0),
                "tx_bytes": f[8].parse::<u64>().unwrap_or(0),
                "tx_packets": f[9].parse::<u64>().unwrap_or(0),
                "tx_errors": f[10].parse::<u64>().unwrap_or(0),
            }))
        })
        .collect::<Vec<Value>>();

    let up = uptime_secs();
    json!({
        "hostname": hostname,
        "os": os_pretty(),
        "kernel": kernel.trim(),
        "arch": arch.trim(),
        "uptime": up,
        "uptime_text": fmt_duration(up),
        "cpu": {"model": cpu_model(), "cores": cores, "percent": cpu_pct},
        "mem": mem,
        "swap": swap,
        "load": load,
        "disks": disks,
        "net": net,
        "time": now_text(),
    })
}

fn now_text() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("{secs}")
}

// ---------- 进程 ----------

pub async fn processes(sort: &str, limit: usize) -> Value {
    let sort_key = if sort == "mem" { "-pmem" } else { "-pcpu" };
    let (code, out) = host_ops::run(
        &[
            "ps",
            "-eo",
            "pid,ppid,user:24,pcpu,pmem,rss,etime,stat,args",
            "--sort",
            sort_key,
            "--no-headers",
        ],
        15,
    )
    .await;
    if code != 0 {
        return json!({"items": [], "total": 0, "error": out.trim()});
    }
    let mut total = 0usize;
    let mut items: Vec<Value> = Vec::new();
    for line in out.lines() {
        let f = split_n(line, 9);
        if f.len() < 9 {
            continue;
        }
        total += 1;
        if items.len() >= limit {
            continue;
        }
        items.push(json!({
            "pid": f[0].parse::<i64>().unwrap_or(0),
            "ppid": f[1].parse::<i64>().unwrap_or(0),
            "user": f[2],
            "cpu": f[3].parse::<f64>().unwrap_or(0.0),
            "mem": f[4].parse::<f64>().unwrap_or(0.0),
            "rss": f[5].parse::<u64>().unwrap_or(0) * 1024,
            "etime": f[6],
            "state": f[7],
            "cmd": f[8],
        }));
    }
    json!({"items": items, "total": total, "sort": if sort == "mem" {"mem"} else {"cpu"}})
}

// ---------- systemd 服务 ----------

pub async fn services() -> Value {
    let (code, out) = host_ops::run(
        &[
            "systemctl",
            "list-units",
            "--type=service",
            "--all",
            "--no-legend",
            "--no-pager",
            "--plain",
        ],
        15,
    )
    .await;
    if code != 0 {
        return json!({"items": [], "counts": {}, "error": out.trim()});
    }

    // 单元文件启用状态：enabled / disabled / static
    let mut enabled: HashMap<String, String> = HashMap::new();
    let (c2, out2) = host_ops::run(
        &[
            "systemctl",
            "list-unit-files",
            "--type=service",
            "--no-legend",
            "--no-pager",
        ],
        15,
    )
    .await;
    if c2 == 0 {
        for line in out2.lines() {
            let f = split_n(line, 3);
            if f.len() >= 2 {
                enabled.insert(f[0].trim_end_matches('*').to_string(), f[1].clone());
            }
        }
    }

    let (_, sys_state) = host_ops::run(&["systemctl", "is-system-running"], 8).await;

    let mut items: Vec<Value> = Vec::new();
    let (mut running, mut failed, mut inactive) = (0, 0, 0);
    for line in out.lines() {
        let line = line.trim_start_matches(|c: char| c == '●' || c.is_whitespace());
        let f = split_n(line, 5);
        if f.len() < 4 {
            continue;
        }
        let active = f[2].clone();
        let sub = f[3].clone();
        // 计数看 SUB：ACTIVE 恒为 active/inactive，running/exited/failed 在 SUB 里
        if active == "failed" || sub == "failed" {
            failed += 1;
        } else if sub == "running" {
            running += 1;
        } else {
            inactive += 1;
        }
        items.push(json!({
            "unit": f[0],
            "load": f[1],
            "active": active,
            "sub": sub,
            "description": f.get(4).cloned().unwrap_or_default(),
            "enabled": enabled.get(&f[0]).cloned().unwrap_or_else(|| "-".to_string()),
        }));
    }
    // failed 排最前，其次运行中，其余保持 systemd 原始顺序
    items.sort_by_key(|v| {
        let a = v.get("active").and_then(|x| x.as_str()).unwrap_or("");
        let s = v.get("sub").and_then(|x| x.as_str()).unwrap_or("");
        if a == "failed" || s == "failed" {
            0
        } else if s == "running" {
            1
        } else {
            2
        }
    });

    json!({
        "items": items,
        "counts": {"total": items.len(), "running": running, "failed": failed, "inactive": inactive},
        "system_state": sys_state.trim(),
    })
}

pub async fn service_action(unit: &str, action: &str) -> Result<Value, String> {
    if !valid_unit(unit) {
        return Err("服务名无效".into());
    }
    let act = match action {
        "start" | "stop" | "restart" | "reload" => action,
        "enable" | "disable" => action,
        _ => return Err("不支持的操作".into()),
    };
    let (code, out) = host_ops::run(&["systemctl", act, unit], 60).await;
    if code != 0 {
        return Err(format!("systemctl {act} {unit} 失败：{}", out.trim()));
    }
    let (_, active) = host_ops::run(&["systemctl", "is-active", unit], 8).await;
    let (_, enabled) = host_ops::run(&["systemctl", "is-enabled", unit], 8).await;
    Ok(json!({
        "unit": unit,
        "action": act,
        "ok": true,
        "active": active.trim(),
        "enabled": enabled.trim(),
    }))
}

// ---------- 监听端口 ----------

pub async fn ports() -> Value {
    let (code, out) = host_ops::run(&["ss", "-H", "-lntup"], 12).await;
    let (code, out) = if code != 0 {
        // 无 root 权限时进程信息拿不到，退化到只有 socket
        host_ops::run(&["ss", "-H", "-lnt"], 12).await
    } else {
        (code, out)
    };
    if code != 0 {
        return json!({"items": [], "error": out.trim()});
    }
    let mut items: Vec<Value> = Vec::new();
    for line in out.lines() {
        let f: Vec<&str> = line.split_whitespace().collect();
        if f.len() < 5 {
            continue;
        }
        let proto = f[0];
        let local = f[4];
        let (addr, port) = match local.rsplit_once(':') {
            // 去掉 IPv6 的作用域后缀（127.0.0.53%lo → 127.0.0.53）
            Some((a, p)) => (
                a.trim_matches(|c| c == '[' || c == ']')
                    .split('%')
                    .next()
                    .unwrap_or(a),
                p,
            ),
            None => continue,
        };
        let port_num: u32 = port.parse().unwrap_or(0);
        if port_num == 0 {
            continue;
        }
        let (proc_name, pid) = parse_ss_users(&f[5..].join(" "));
        items.push(json!({
            "proto": proto,
            "addr": addr,
            "port": port_num,
            "process": proc_name,
            "pid": pid,
        }));
    }
    items.sort_by_key(|v| v.get("port").and_then(|x| x.as_u64()).unwrap_or(0));
    json!({"items": items, "total": items.len()})
}

/// 从 `users:(("sshd",pid=456,fd=3))` 中取出进程名与 pid。
fn parse_ss_users(s: &str) -> (String, i64) {
    let name = s
        .find("\"")
        .and_then(|i| {
            let rest = &s[i + 1..];
            rest.find('"').map(|j| rest[..j].to_string())
        })
        .unwrap_or_default();
    let pid = s
        .find("pid=")
        .and_then(|i| {
            let rest = &s[i + 4..];
            let end = rest.find(|c: char| !c.is_ascii_digit()).unwrap_or(rest.len());
            rest[..end].parse::<i64>().ok()
        })
        .unwrap_or(0);
    (name, pid)
}

// ---------- 端口进程终结 ----------

/// 保护名单：这些进程不允许被终结（防止自断 / 断 SSH / 断 Docker / 断 agent）。
const PROTECTED_PROCS: &[&str] = &[
    "systemd", "init", "sshd", "dockerd", "containerd", "ppanel-agent",
];

/// 列出某端口的监听进程（复用 ss 解析），返回 (进程列表, 是否存在无进程信息的监听)。
async fn port_listeners(port: u16, proto: &str) -> (Vec<(String, i64)>, bool) {
    let (code, out) = host_ops::run(&["ss", "-H", "-lnp"], 12).await;
    let (code, out) = if code != 0 {
        host_ops::run(&["ss", "-H", "-ln"], 12).await
    } else {
        (code, out)
    };
    let mut targets: Vec<(String, i64)> = Vec::new();
    let mut saw_socket = false;
    if code != 0 {
        return (targets, saw_socket);
    }
    let suffix = format!(":{port}");
    for line in out.lines() {
        let f: Vec<&str> = line.split_whitespace().collect();
        if f.len() < 5 || !f[0].starts_with(proto) {
            continue;
        }
        if !f[4].ends_with(&suffix) {
            continue;
        }
        saw_socket = true;
        let (name, pid) = parse_ss_users(&f[5..].join(" "));
        if pid > 0 && !targets.iter().any(|(_, p)| *p == pid) {
            targets.push((name, pid));
        }
    }
    (targets, saw_socket)
}

/// 终结监听指定端口的进程：先 SIGTERM，1.5s 后仍存活再 SIGKILL。
/// 系统关键进程（sshd/dockerd/systemd/agent 自身等）拒绝终结。
pub async fn port_kill(port: u16, proto: &str) -> AppResult<Value> {
    if !(1..=65535).contains(&port) {
        return Err(AppError::bad_request("端口无效"));
    }
    let proto = match proto.to_lowercase().as_str() {
        "tcp" | "" => "tcp",
        "udp" => "udp",
        _ => return Err(AppError::bad_request("协议仅支持 tcp/udp")),
    };
    let (targets, saw_socket) = port_listeners(port, proto).await;
    if targets.is_empty() {
        return Err(AppError::bad_request(if saw_socket {
            format!(
                "端口 {port} 有监听但拿不到进程信息（agent 需要 root 权限，或进程属于其它用户）"
            )
        } else {
            format!("端口 {port} 当前没有 {proto} 监听进程")
        }));
    }

    let me = std::process::id() as i64;
    let mut killed: Vec<Value> = Vec::new();
    let mut skipped: Vec<Value> = Vec::new();
    let mut failed: Vec<Value> = Vec::new();
    for (name, pid) in targets {
        if pid == me || PROTECTED_PROCS.contains(&name.as_str()) {
            skipped.push(json!({"pid": pid, "process": name, "reason": "系统关键进程，禁止终结"}));
            continue;
        }
        let _ = host_ops::run(&["kill", &pid.to_string()], 8).await;
        tokio::time::sleep(Duration::from_millis(1500)).await;
        if std::path::Path::new(&format!("/proc/{pid}")).exists() {
            let _ = host_ops::run(&["kill", "-9", &pid.to_string()], 8).await;
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
        let gone = !std::path::Path::new(&format!("/proc/{pid}")).exists();
        let item = json!({"pid": pid, "process": name});
        if gone {
            killed.push(item);
        } else {
            failed.push(item);
        }
    }
    let ok = !killed.is_empty() && failed.is_empty();
    Ok(json!({
        "ok": ok,
        "port": port,
        "proto": proto,
        "killed": killed,
        "skipped": skipped,
        "failed": failed,
    }))
}

// ---------- 内存清理 ----------

/// 当前内存快照（MB 为单位原始 kB 值直接透出）。
fn mem_snapshot() -> Value {
    let info = meminfo();
    let mt = info.get("MemTotal").copied().unwrap_or(0);
    let ma = info.get("MemAvailable").copied().unwrap_or(0);
    let st = info.get("SwapTotal").copied().unwrap_or(0);
    let sf = info.get("SwapFree").copied().unwrap_or(0);
    json!({
        "total": mt,
        "used": mt.saturating_sub(ma),
        "available": ma,
        "free": info.get("MemFree").copied().unwrap_or(0),
        "buffers": info.get("Buffers").copied().unwrap_or(0),
        "cached": info.get("Cached").copied().unwrap_or(0),
        "percent": if mt > 0 { round1(mt.saturating_sub(ma) as f64 / mt as f64 * 100.0) } else { 0.0 },
        "swap_used": st.saturating_sub(sf),
        "swap_total": st,
    })
}

/// 内存清理：sync + drop_caches（释放页缓存 / dentries / inodes）。
/// 不杀任何进程、不碰 swap，对运行中的服务无影响。
pub async fn mem_cleanup() -> Value {
    let before = mem_snapshot();
    let _ = host_ops::run(&["sync"], 30).await;
    let direct = std::fs::write("/proc/sys/vm/drop_caches", b"3").is_ok();
    let mode = if direct {
        "proc"
    } else {
        let (rc, _) = host_ops::run(&["sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"], 15).await;
        if rc == 0 { "shell" } else { "" }
    };
    tokio::time::sleep(Duration::from_millis(800)).await;
    let after = mem_snapshot();
    let freed = after["available"].as_u64().unwrap_or(0)
        .saturating_sub(before["available"].as_u64().unwrap_or(0));
    json!({
        "ok": !mode.is_empty(),
        "mode": mode,
        "error": if mode.is_empty() { "drop_caches 写入失败（agent 需要 root 权限）" } else { "" },
        "before": before,
        "after": after,
        "freed": freed,
    })
}

// ---------- 系统日志 ----------

pub async fn journal(unit: Option<&str>, lines: usize) -> Value {
    let n = lines.clamp(10, 500).to_string();
    let mut args: Vec<&str> = vec!["journalctl", "-n", &n, "--no-pager", "-o", "short-iso"];
    if let Some(u) = unit {
        if !u.is_empty() {
            if !valid_unit(u) {
                return json!({"lines": [], "error": "服务名无效"});
            }
            args.push("-u");
            args.push(u);
        }
    }
    let (code, out) = host_ops::run(&args, 20).await;
    let body: Vec<String> = out
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| l.to_string())
        .collect();
    json!({
        "unit": unit.unwrap_or(""),
        "lines": body,
        "count": body.len(),
        "error": if code != 0 { out.trim() } else { "" },
    })
}

// ---------- 登录用户 ----------

pub async fn users() -> Value {
    let mut online: Vec<Value> = Vec::new();
    let (code, out) = host_ops::run(&["who"], 8).await;
    if code == 0 {
        for line in out.lines() {
            let f = split_n(line, 5);
            if f.len() < 2 {
                continue;
            }
            online.push(json!({
                "user": f[0],
                "tty": f[1],
                "since": f.get(2).cloned().unwrap_or_default(),
                "from": f.get(4).cloned().unwrap_or_default(),
            }));
        }
    }
    let mut system: Vec<Value> = Vec::new();
    for line in read_file("/etc/passwd").lines() {
        let f: Vec<&str> = line.split(':').collect();
        if f.len() < 7 {
            continue;
        }
        let uid: i64 = f[2].parse().unwrap_or(-1);
        let shell = f[6].to_string();
        let loginable = !shell.ends_with("nologin") && !shell.ends_with("false") && !shell.is_empty();
        if uid >= 1000 || uid == 0 {
            system.push(json!({
                "user": f[0],
                "uid": uid,
                "gid": f[3].parse::<i64>().unwrap_or(-1),
                "home": f[5],
                "shell": shell,
                "loginable": loginable,
            }));
        }
    }
    json!({"online": online, "accounts": system})
}

// ---------- SSH 登录审计 ----------

pub async fn ssh_logins(limit: usize) -> Value {
    let n = limit.clamp(10, 300).to_string();
    let (_, out) = host_ops::run(
        &[
            "journalctl",
            "-u",
            "ssh",
            "-u",
            "sshd",
            "-n",
            &n,
            "--no-pager",
            "-o",
            "short-iso",
            "--grep",
            "Accepted|Failed|Invalid user",
        ],
        20,
    )
    .await;
    let mut accepted = 0;
    let mut failed = 0;
    let mut items: Vec<Value> = Vec::new();
    for line in out.lines() {
        let is_ok = line.contains("Accepted");
        let is_fail = line.contains("Failed") || line.contains("Invalid user");
        if !is_ok && !is_fail {
            continue;
        }
        if is_ok {
            accepted += 1;
        } else {
            failed += 1;
        }
        // 截取 Accepted/Failed 之后的片段作为摘要
        let idx = line.find("Accepted").or_else(|| line.find("Failed")).unwrap_or(0);
        items.push(json!({
            "ok": is_ok,
            "line": line[idx..].to_string(),
        }));
    }
    json!({"items": items, "accepted": accepted, "failed": failed})
}
