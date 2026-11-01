//! 实例服务：容器创建/启停/删除、端口分配、依赖安装任务、状态同步、用量采样。

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, OnceLock};

use bollard::container::{
    Config, CreateContainerOptions, LogsOptions, RemoveContainerOptions, StatsOptions,
    StopContainerOptions,
};
use bollard::exec::{CreateExecOptions, StartExecOptions, StartExecResults};
use bollard::image::CreateImageOptions;
use bollard::models::{HostConfig, PortBinding, PortMap, RestartPolicy, RestartPolicyNameEnum};
use bollard::network::CreateNetworkOptions;
use futures_util::StreamExt;
use serde_json::Value;
use sqlx::SqlitePool;
use tokio::sync::mpsc;

use crate::config::settings;
use crate::error::{AppError, AppResult};
use crate::models::{Instance, utcnow};
use crate::state::{AppState, ExecJob};
use crate::services::file;

pub fn container_prefix() -> String {
    settings().container_prefix.clone()
}

pub fn container_name(instance_id: i64) -> String {
    format!("{}{}", container_prefix(), instance_id)
}

pub fn host_dir_for(instance_id: i64) -> String {
    format!("{}/{}", settings().data_root.trim_end_matches('/'), instance_id)
}

pub fn volume_src_for(inst: &Instance) -> String {
    let roots = &settings().docker_volume_root;
    if !roots.is_empty() {
        format!("{}/{}", roots.trim_end_matches('/'), inst.id)
    } else {
        inst.host_dir.clone()
    }
}

pub fn is_php(image: &str) -> bool {
    image.starts_with("php:") || image.starts_with("ppanel-php:")
}

pub fn runtime_kind(image: &str) -> &'static str {
    if image.starts_with("node:") {
        "node"
    } else if image.starts_with("golang:") {
        "go"
    } else if is_php(image) {
        "php"
    } else {
        "python"
    }
}

pub fn default_start_cmd(image: &str, _mem_limit_mb: Option<i64>) -> String {
    match runtime_kind(image) {
        "node" => "node index.js".to_string(),
        "go" => "[ -f go.mod ] || go mod init ppanel-app; go build -o app . && ./app".to_string(),
        // PHP 默认命令保持一行：禁用函数/上传/内存等运行参数由 entry 组装时动态注入
        // （见 php_ini_flags），不进用户可见/可编辑的启动命令——PHP 用户通常只需要
        // 「上传代码就能跑」，不该看到一长串 -d 参数，改规格也不用重写命令。
        "php" => "php -S 0.0.0.0:8000 -t /app".to_string(),
        // Python 只留一行。requirements.txt 的安装由容器入口自动完成（见 python_bootstrap），
        // 不再塞进用户可见/可编辑的启动命令里。
        _ => "python main.py".to_string(),
    }
}

/// 启动前的依赖自举脚本（Python）：
/// - 有 requirements.txt 才装；
/// - 用 sha256 盖戳在 /tmp（容器内），依赖没变就跳过，重启不再等 pip；
/// - 装失败只告警不阻断，应用照常启动（与原命令语义一致）。
/// 盖戳放 /tmp 而不是 /app：容器重建后盖戳随之消失，依赖会被重新安装，不会出现"戳还在但包没了"。
fn python_bootstrap() -> &'static str {
    r#"if [ -f requirements.txt ]; then
  __h=$(sha256sum requirements.txt 2>/dev/null | cut -d' ' -f1)
  if [ -n "$__h" ] && [ "$__h" = "$(cat /tmp/.ppanel_deps 2>/dev/null)" ]; then
    echo '[依赖] requirements.txt 未变化，跳过安装'
  else
    echo '[依赖] 检测到 requirements.txt，安装中…'
    if pip install -r requirements.txt --no-input --disable-pip-version-check; then
      [ -n "$__h" ] && printf '%s' "$__h" > /tmp/.ppanel_deps
      echo '[依赖] 依赖安装完成'
    else
      echo '[依赖] 部分依赖安装失败，仍尝试启动（可在「日志」页查看 pip 输出）'
    fi
  fi
fi
"#
}

/// 端口桥（Python / PHP 镜像，后台运行）：
/// 用户应用把端口写死成 8010/3000/5000 等而没按约定监听 INNER_APP_PORT 时，
/// 自动探测实际监听端口并在约定端口上建一条转发，外部访问仍然可通。
/// - 应用自行监听约定端口 → 桥直接退出（零开销，行为与从前一致）；
/// - 桥运行中应用又自己绑定了约定端口（竞态）→ 桥退出让位，容器 crash 后 unless-stopped 重启收敛；
/// - 600 秒没探测到任何应用端口（应用没起来）→ 桥退出。
fn python_port_bridge() -> &'static str {
    r#"cat > /tmp/.ppanel_bridge.py <<'PPEOF'
import asyncio, os, time

APP = int(os.environ.get("INNER_APP_PORT", "8000"))
SKIP = {APP, 22, 5678, 9229}          # 约定端口 + ssh + debugpy + node inspector
DEADLINE = 600

def listen_ports():
    ports = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(f).read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            p = line.split()
            if len(p) > 3 and p[3] == "0A":        # LISTEN
                try:
                    ports.add(int(p[1].split(":")[1], 16))
                except (ValueError, IndexError):
                    pass
    return ports

def own_socket_inodes():
    """bridge 自己持有的 socket inode（绑定后排除自己，避免把 8000 上的自己误判为应用）"""
    inodes = set()
    try:
        for fd in os.listdir("/proc/self/fd"):
            try:
                t = os.readlink(f"/proc/self/fd/{fd}")
                if t.startswith("socket:["):
                    inodes.add(t[8:-1])
            except OSError:
                pass
    except OSError:
        pass
    return inodes

def hex_to_ip(hex_ip):
    """/proc/net/tcp 的地址是小端 hex：v4 长 8 位；tcp6 中 IPv4-mapped 取末 8 位"""
    try:
        if len(hex_ip) == 8:
            b = bytes.fromhex(hex_ip)[::-1]
        elif len(hex_ip) == 32:
            b = bytes.fromhex(hex_ip[-8:])[::-1]
        else:
            return None
        return ".".join(str(x) for x in b)
    except ValueError:
        return None

def foreign_listen_ports():
    """应用监听的端口：排除 bridge 自身的 socket + 回环地址上的监听。
    回环必须排除：docker 内嵌 DNS 在 127.0.0.11:<随机端口> 常驻 LISTEN
    （tcp 与 tcp6 都可能出现），是稳定端口，会被 3 次复核放行导致误转发。"""
    own = own_socket_inodes()
    ports = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(f).read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            p = line.split()
            if len(p) > 9 and p[3] == "0A" and p[9] not in own:   # LISTEN 且不是自己的
                ip = hex_to_ip(p[1].split(":")[0])
                if ip and ip.startswith("127."):
                    continue
                try:
                    ports.add(int(p[1].split(":")[1], 16))
                except (ValueError, IndexError):
                    pass
    return ports

async def pipe(r, w):
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass

async def handle(rd, wr, target):
    try:
        ur, uw = await asyncio.open_connection("127.0.0.1", target)
        await asyncio.gather(pipe(rd, uw), pipe(ur, wr))
    except Exception:
        try:
            wr.close()
        except Exception:
            pass

async def main():
    target = None
    t0 = time.time()
    while time.time() - t0 < DEADLINE:
        ports = foreign_listen_ports()
        if APP in ports:
            return                                   # 应用按约定监听，桥不需要
        cand = sorted(p for p in ports if p not in SKIP and p > 1024)
        if cand:
            # 复核：端口需连续 3 次探测都存在（间隔 1s），过滤瞬时 ephemeral 端口，
            # 避免 crash 循环/短命进程的随机端口被误当成应用端口
            stable = cand[0]
            confirmed = stable in foreign_listen_ports()
            for _ in range(2):
                await asyncio.sleep(1)
                if stable not in foreign_listen_ports():
                    confirmed = False
                    break
            if confirmed:
                target = stable
                print(f"[端口桥] 应用监听在 {target}（非约定端口 {APP}），已自动转发 {APP}->{target}，外部访问可正常使用；"
                      f"建议启动命令改用 --port $INNER_APP_PORT 省去桥接层", flush=True)
                break
        await asyncio.sleep(1)
    else:
        print("[端口桥] 未探测到应用监听端口，桥退出", flush=True)
        return

    async def recheck(server):
        while True:
            await asyncio.sleep(2)
            if APP in foreign_listen_ports():
                print("[端口桥] 应用已自行监听约定端口，桥退出让位", flush=True)
                server.close()
                return

    server = await asyncio.start_server(lambda r, w: handle(r, w, target), "0.0.0.0", APP)
    asyncio.ensure_future(recheck(server))
    async with server:
        await server.serve_forever()

asyncio.run(main())
PPEOF
python /tmp/.ppanel_bridge.py &
"#
}

/// Node 端口桥：node 镜像天然有 node 解释器，用纯 JS 实现同语义的桥
/// （/proc/net/tcp 探测 + 3 次复核 + net 双向转发 + 让位复查）。
fn node_port_bridge() -> &'static str {
    r#"cat > /tmp/.ppanel_bridge.js <<'NDEOF'
const fs = require("fs");
const net = require("net");

const APP = parseInt(process.env.INNER_APP_PORT || "8000", 10);
const SKIP = new Set([APP, 22, 5678, 9229]);  // 约定端口 + ssh + debugpy + node inspector
const DEADLINE = 600000;

function ownInodes() {
  const s = new Set();
  try {
    for (const fd of fs.readdirSync("/proc/self/fd")) {
      try {
        const t = fs.readlinkSync("/proc/self/fd/" + fd);
        const m = /^socket:\[(\d+)\]$/.exec(t);
        if (m) s.add(m[1]);
      } catch (e) {}
    }
  } catch (e) {}
  return s;
}

function hexToIp(h) {
  let b;
  if (h.length === 8) b = Buffer.from(h, "hex").reverse();
  else if (h.length === 32) b = Buffer.from(h.slice(-8), "hex").reverse();
  else return null;
  return Array.from(b).join(".");
}

function foreignPorts() {
  const own = ownInodes();
  const ports = new Set();
  for (const f of ["/proc/net/tcp", "/proc/net/tcp6"]) {
    let lines;
    try { lines = fs.readFileSync(f, "utf8").split("\n").slice(1); } catch (e) { continue; }
    for (const line of lines) {
      const p = line.trim().split(/\s+/);
      if (p.length > 9 && p[3] === "0A" && !own.has(p[9])) {
        const ip = hexToIp(p[1].split(":")[0]);
        if (ip && ip.startsWith("127.")) continue;  // docker 内嵌 DNS 127.0.0.11 排除
        const port = parseInt(p[1].split(":")[1], 16);
        if (port) ports.add(port);
      }
    }
  }
  return ports;
}

function pipe(a, b) {
  a.on("data", (d) => { try { b.write(d); } catch (e) {} });
  a.on("error", () => { try { b.destroy(); } catch (e) {} });
  a.on("close", () => { try { b.end(); } catch (e) {} });
  b.on("error", () => { try { a.destroy(); } catch (e) {} });
}

let target = null;
let started = false;   // 互斥：poll 每 1s 都会触发一轮复核，防止 startBridge 被调两次
const t0 = Date.now();

const poll = setInterval(() => {
  const ports = foreignPorts();
  if (ports.has(APP)) {                          // 应用按约定监听，桥不需要
    console.log("[端口桥] 应用已监听约定端口，桥退出");
    process.exit(0);
  }
  if (Date.now() - t0 > DEADLINE) {
    console.log("[端口桥] 未探测到应用监听端口，桥退出");
    process.exit(0);
  }
  const cand = [...ports].filter((p) => !SKIP.has(p) && p > 1024).sort((a, b) => a - b);
  if (!cand.length) return;
  const stable = cand[0];
  // 3 次复核（间隔 1s），过滤瞬时 ephemeral 端口
  let confirmed = ports.has(stable);
  let round = 0;
  const check = setInterval(() => {
    round++;
    if (!foreignPorts().has(stable)) confirmed = false;
    if (round >= 2) {
      clearInterval(check);
      if (!confirmed || started) return;
      started = true;
      target = stable;
      clearInterval(poll);
      startBridge();
    }
  }, 1000);
}, 1000);

function startBridge() {
  const server = net.createServer((client) => {
    const up = net.connect(target, "127.0.0.1");
    pipe(client, up);
    pipe(up, client);
  });
  server.on("error", (e) => {
    console.log("[端口桥] 绑定失败：" + e.message);
    process.exit(0);
  });
  server.listen(APP, "0.0.0.0", () => {
    console.log("[端口桥] 应用监听在 " + target + "（非约定端口 " + APP + "），已自动转发 " + APP + "->" + target +
      "，外部访问可正常使用；建议启动命令改用 --port $" + "INNER_APP_PORT 省去桥接层");
  });
  setInterval(() => {
    if (foreignPorts().has(APP)) {
      console.log("[端口桥] 应用已自行监听约定端口，桥退出让位");
      server.close(() => process.exit(0));
      process.exit(0);
    }
  }, 2000);
}
NDEOF
node /tmp/.ppanel_bridge.js >/tmp/.ppanel_bridge.log 2>&1 &
"#
}

/// PHP 端口桥：php 镜像没有 python3，用纯 PHP 实现同语义的桥
/// （/proc/net/tcp 探测 + 3 次复核 + stream_select 双向转发 + 让位复查）。
fn php_port_bridge() -> &'static str {    r#"cat > /tmp/.ppanel_bridge.php <<'PPEOF'
<?php
error_reporting(0);
$APP = (int)(getenv("INNER_APP_PORT") ?: "8000");
$SKIP = [$APP, 22, 5678, 9229];
$DEADLINE = 600;

function own_inodes(): array {
    $s = [];
    $fds = @scandir("/proc/self/fd");
    if ($fds) foreach ($fds as $f) {
        $t = @readlink("/proc/self/fd/$f");
        if (is_string($t) && preg_match('/^socket:\[(\d+)\]$/', $t, $m)) $s[$m[1]] = 1;
    }
    return $s;
}

// /proc/net/tcp 的地址是小端 hex：v4 长 8 位；tcp6 中 IPv4-mapped 取末 8 位
function hex_to_ip(string $h): ?string {
    $n = strlen($h);
    if ($n === 8) $hex = $h; elseif ($n === 32) $hex = substr($h, -8); else return null;
    $b = @pack("H*", $hex);
    if ($b === false || strlen($b) !== 4) return null;
    return implode(".", array_reverse(unpack("C4", $b)));
}

// 应用监听的端口：排除桥自身 socket + 回环（docker 内嵌 DNS 在 127.0.0.11 常驻 LISTEN）
function foreign_listen_ports(): array {
    $own = own_inodes();
    $ports = [];
    foreach (["/proc/net/tcp", "/proc/net/tcp6"] as $f) {
        $lines = @file($f, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
        if (!$lines) continue;
        foreach (array_slice($lines, 1) as $line) {
            $p = preg_split('/\s+/', trim($line));
            if (count($p) < 10 || $p[3] !== "0A") continue;   // LISTEN
            if (isset($own[$p[9]])) continue;                 // 自己的 socket
            $ip = hex_to_ip(explode(":", $p[1])[0]);
            if ($ip === null || strncmp($ip, "127.", 4) === 0) continue;
            $port = (int)hexdec(substr($p[1], strrpos($p[1], ":") + 1));
            if ($port > 0) $ports[$port] = 1;
        }
    }
    return array_keys($ports);
}

function close_pair(array &$map, $sock): void {
    $peer = $map[(int)$sock] ?? null;
    if (is_resource($peer)) { @fclose($peer); unset($map[(int)$peer]); }
    @fclose($sock); unset($map[(int)$sock]);
}

// ---- 探测阶段：连续 3 次（间隔 1s）确认才认，过滤瞬时 ephemeral 端口 ----
$target = 0;
$t0 = time();
while (time() - $t0 < $DEADLINE) {
    $ports = foreign_listen_ports();
    if (in_array($APP, $ports, true)) exit(0);            // 应用按约定监听，桥不需要
    $cand = array_values(array_filter($ports, function ($p) use ($SKIP) {
        return !in_array($p, $SKIP, true) && $p > 1024;
    }));
    sort($cand);
    if ($cand) {
        $stable = $cand[0];
        $confirmed = in_array($stable, foreign_listen_ports(), true);
        for ($i = 0; $i < 2; $i++) {
            sleep(1);
            if (!in_array($stable, foreign_listen_ports(), true)) { $confirmed = false; break; }
        }
        if ($confirmed) { $target = $stable; break; }
    }
    sleep(1);
}
if (!$target) { echo "[端口桥] 未探测到应用监听端口，桥退出\n"; exit(0); }
echo "[端口桥] 应用监听在 $target（非约定端口 $APP），已自动转发 $APP->$target，外部访问可正常使用\n";

// ---- 转发阶段：stream_select 双向复制，每 2s 复查让位 ----
$server = @stream_socket_server("tcp://0.0.0.0:$APP", $ec, $em);
if (!$server) { echo "[端口桥] 绑定 $APP 失败\n"; exit(1); }
stream_set_blocking($server, false);
$map = [];   // (int)$sock => 对端 resource
$lastProbe = time();
while (true) {
    $read = [$server];
    foreach ($map as $res) $read[] = $res;
    $write = null; $except = null;
    if (@stream_select($read, $write, $except, 1) === false) continue;
    if (in_array($server, $read, true)) {
        while ($cn = @stream_socket_accept($server, 0)) {
            $up = @stream_socket_client("tcp://127.0.0.1:$target", $e2, $m2, 3);
            if ($up) {
                stream_set_blocking($cn, false);
                stream_set_blocking($up, false);
                $map[(int)$cn] = $up;
                $map[(int)$up] = $cn;
            } else {
                @fclose($cn);
            }
        }
    }
    foreach ($read as $sock) {
        if ($sock === $server) continue;
        $data = @fread($sock, 65536);
        if ($data === false || $data === "") { close_pair($map, $sock); continue; }
        $peer = $map[(int)$sock] ?? null;
        if (is_resource($peer)) @fwrite($peer, $data);
    }
    if (time() - $lastProbe >= 2) {
        $lastProbe = time();
        if (in_array($APP, foreign_listen_ports(), true)) {
            echo "[端口桥] 应用已自行监听约定端口，桥退出让位\n";
            exit(0);
        }
    }
}
PPEOF
php /tmp/.ppanel_bridge.php >/tmp/.ppanel_bridge.log 2>&1 &
"#
}

/// 把用户看到的短命令包装成容器真正执行的命令：
/// 入口先跑依赖自举，再执行用户的启动命令。启动命令本身保持干净。
/// 用户命令前加 exec：应用进程直接替换 sh 成为 tini 的子进程，
/// docker stop 的 TERM 能直达应用（优雅关闭、毫秒级停机）——
/// 否则 sh 等前台命令结束才处理信号，TERM 传不下去，每次都要等超时被 SIGKILL。
pub fn entry_cmd_str(image: &str, start_cmd: &str, php_flags: &str) -> String {
    match runtime_kind(image) {
        "python" => format!("{}{}exec {}", python_bootstrap(), python_port_bridge(), start_cmd),
        // node 与 python/php 同语义：应用没监听约定端口时自动桥接转发
        "node" => format!("{}exec {}", node_port_bridge(), start_cmd),
        "php" => format!("{}exec {}", php_port_bridge(), inject_php_flags(start_cmd, php_flags)),
        _ => format!("exec {}", start_cmd),
    }
}

/// 把 PHP 运行参数（-d …）注入启动命令：命令以 php 开头 → 插到 php 后面；
/// 其余命令（自定义脚本等）不动——那些场景参数由用户自己管。
fn inject_php_flags(cmd: &str, flags: &str) -> String {
    if flags.is_empty() {
        return cmd.to_string();
    }
    let mut parts: Vec<String> = cmd.split_whitespace().map(|s| s.to_string()).collect();
    if parts.first().map(|s| s.as_str()) == Some("php") {
        parts.insert(1, flags.to_string());
    } else {
        return cmd.to_string();
    }
    parts.join(" ")
}

/// PHP 容器入口注入的运行参数：禁用函数（实例自选，空 = 节点默认）、上传/POST/内存限制。
/// 这些属于「平台运行参数」，与启动命令彻底分离——用户看到/编辑的命令保持一行干净命令，
/// 节点改安全策略、实例升降配（内存变化）都不需要动用户的命令。
pub fn php_ini_flags(inst: &Instance) -> String {
    let s = settings();
    let raw = inst.disabled_funcs.trim();
    let dis: Vec<String> = if raw.is_empty() {
        default_disabled_funcs()
    } else {
        raw.split(',').map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).collect()
    };
    let mut flags = String::new();
    if !dis.is_empty() {
        flags.push_str(&format!("-d disable_functions={}", dis.join(",")));
    }
    let upload = s.upload_limit_mb;
    flags.push_str(&format!(" -d upload_max_filesize={upload}M"));
    flags.push_str(&format!(" -d post_max_size={}M", upload + 16));
    if inst.mem_limit > 0 {
        flags.push_str(&format!(" -d memory_limit={}M", inst.mem_limit));
    }
    flags.trim().to_string()
}

pub fn entry_filename(image: &str) -> &'static str {
    match runtime_kind(image) {
        "php" => "index.php",
        "node" => "index.js",
        "go" => "main.go",
        _ => "main.py",
    }
}

pub async fn allowed_images(state: &AppState) -> Vec<String> {
    let mut images = settings().all_images();
    images.extend(built_images(state).await);
    images
}

pub async fn built_images(state: &AppState) -> Vec<String> {
    let Some(docker) = state.docker.try_get().await else {
        return vec![];
    };
    match docker.list_images::<String>(None).await {
        Ok(list) => {
            let mut tags: Vec<String> = list
                .into_iter()
                .flat_map(|i| i.repo_tags)
                .filter(|t| t.starts_with("ppanel-php:") && t.ends_with("-full"))
                .collect();
            tags.sort();
            tags
        }
        Err(_) => vec![],
    }
}

pub async fn ensure_network(state: &AppState) -> AppResult<()> {
    let docker = state.docker.get().await?;
    let name = &settings().docker_network;
    if docker.inspect_network::<String>(name, None).await.is_ok() {
        return Ok(());
    }
    let opts = CreateNetworkOptions {
        name: name.clone(),
        driver: "bridge".to_string(),
        ..Default::default()
    };
    let _ = docker.create_network(opts).await;
    Ok(())
}

fn port_free(port: u16) -> bool {
    std::net::TcpListener::bind(("0.0.0.0", port)).is_ok()
}

pub async fn alloc_port(pool: &SqlitePool) -> AppResult<u16> {
    use rand::Rng;
    let used: Vec<i64> = sqlx::query_scalar("SELECT ext_port FROM instances")
        .fetch_all(pool)
        .await?;
    let used: HashSet<i64> = used.into_iter().collect();
    let s = settings();
    let total = (s.port_end as i64 - s.port_start as i64 + 1).max(1);
    let offset = rand::thread_rng().gen_range(0..total);
    for i in 0..total {
        let port = s.port_start as i64 + (offset + i) % total;
        if used.contains(&port) {
            continue;
        }
        if port_free(port as u16) {
            return Ok(port as u16);
        }
    }
    Err(AppError::unavailable("外部端口已耗尽，无可用端口"))
}

pub async fn ensure_image(state: &AppState, image: &str) -> AppResult<()> {
    let docker = state.docker.get().await?;
    if docker.inspect_image(image).await.is_ok() {
        return Ok(());
    }
    let opts = CreateImageOptions {
        from_image: image.to_string(),
        ..Default::default()
    };
    let mut stream = docker.create_image(Some(opts), None, None);
    let mut err: Option<String> = None;
    while let Some(ev) = stream.next().await {
        match ev {
            Ok(_) => {}
            Err(e) => {
                err = Some(e.to_string());
                break;
            }
        }
    }
    match err {
        Some(e) => Err(AppError::bad_gateway(format!("拉取镜像失败：{e}"))),
        None => Ok(()),
    }
}

const DEFAULT_MAIN_PY: &str = r#"from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = "PPanel instance is running. Edit /app/main.py and restart.\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):
        pass


print("PPanel instance starting on 0.0.0.0:8000 ...")
HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
"#;

const DEFAULT_INDEX_PHP: &str = r#"<?php
header("Content-Type: text/plain; charset=utf-8");
echo "PPanel PHP instance is running. Edit /app/index.php and restart.\n";
echo "PHP version: " . PHP_VERSION . "\n";
"#;

const DEFAULT_INDEX_JS: &str = r#"// PPanel Node 实例示例入口：开箱即跑，改完代码点重启生效
const http = require("http");

http.createServer((req, res) => {
  res.writeHead(200, { "Content-Type": "text/plain; charset=utf-8" });
  res.end(`PPanel Node instance is running. Edit /app/index.js and restart.\nNode version: ${process.version}\n`);
}).listen(8000, "0.0.0.0", () => console.log("PPanel instance starting on 0.0.0.0:8000 ..."));
"#;

const DEFAULT_MAIN_GO: &str = r#"// PPanel Go 实例示例入口：零第三方依赖，开箱即跑。
// 默认命令 go build -o app . && ./app——首次启动编译约 1-3 分钟（受 CPU 限额影响），之后重启秒级。
package main

import (
	"fmt"
	"net/http"
	"runtime"
)

func main() {
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "PPanel Go instance is running. Edit /app/main.go and restart.\nGo version: %s\n", runtime.Version())
	})
	fmt.Println("PPanel instance starting on 0.0.0.0:8000 ...")
	http.ListenAndServe("0.0.0.0:8000", nil)
}
"#;

pub fn ensure_entry_file(host_dir: &str, image: &str) -> AppResult<()> {
    let name = entry_filename(image);
    let target = std::path::Path::new(host_dir).join(name);
    if target.exists() {
        return Ok(());
    }
    let content = match runtime_kind(image) {
        "php" => DEFAULT_INDEX_PHP,
        "node" => DEFAULT_INDEX_JS,
        "go" => DEFAULT_MAIN_GO,
        _ => DEFAULT_MAIN_PY,
    };
    file::write_text(host_dir, &format!("/{name}"), content)
}

/// 一键重置：实例恢复到刚创建的样子。
/// 停容器 → 清空数据目录（保留目录本身与规格/端口/令牌）→ 重写默认入口文件
/// → 重建容器 → 原来在运行则拉起并诊断。规格、启动命令、面板令牌不受影响。
pub async fn reset_instance(state: &AppState, inst: &mut Instance) -> AppResult<serde_json::Value> {
    let was_running = inst.status == "running";
    if was_running {
        let _ = stop_instance(state, inst).await;
    }
    // 清空数据目录内容（保留目录本身；逐项删除，失败不中断——重置是尽力而为的清理）
    let dir = std::path::Path::new(&inst.host_dir);
    if let Ok(entries) = std::fs::read_dir(dir) {
        for e in entries.flatten() {
            let p = e.path();
            if p.is_dir() {
                let _ = std::fs::remove_dir_all(&p);
            } else {
                let _ = std::fs::remove_file(&p);
            }
        }
    }
    ensure_entry_file(&inst.host_dir, &inst.image)?;
    recreate_container(state, inst).await?;
    if was_running {
        start_instance(state, inst).await?;
        diagnose_startup(state, inst).await?;
    }
    persist_status(&state.db, inst).await?;
    Ok(serde_json::json!({ "status": inst.status, "reset": true }))
}

fn entry_cmd(inst: &Instance) -> Vec<String> {
    // 用户自定义了启动命令就执行它：入口可以是任何东西
    // （python main.py / python -m uvicorn app.main:app --port 8000 / gunicorn …）。
    // 注意：与 default_start_cmd 相同的值视为「平台默认」而非自定义（历史上默认值曾落库），
    // 仍走入口文件判断——main.py 被删掉/改包结构后能自动收敛到空闲模式，而不是找不到文件 crash。
    let cmd = inst.start_cmd.trim();
    let flags = php_ini_flags(inst);
    if !cmd.is_empty() && cmd != default_start_cmd(&inst.image, Some(inst.mem_limit)) {
        return vec![
            "/bin/sh".into(),
            "-c".into(),
            entry_cmd_str(&inst.image, cmd, &flags),
        ];
    }
    let entry = std::path::Path::new(&inst.host_dir).join(entry_filename(&inst.image));
    if entry.exists() {
        let cmd = default_start_cmd(&inst.image, Some(inst.mem_limit));
        return vec![
            "/bin/sh".into(),
            "-c".into(),
            entry_cmd_str(&inst.image, &cmd, &flags),
        ];
    }
    vec![
        "/bin/sh".into(),
        "-c".into(),
        format!(
            "echo '[空闲模式] 未检测到 {} 且未设置启动命令：环境已就绪，上传代码后在「启动命令」填写入口（如 python main.py 或 python -m uvicorn app.main:app --port 8000）后点重启即可运行' && sleep infinity",
            entry_filename(&inst.image)
        ),
    ]
}

pub async fn create_container(state: &AppState, inst: &Instance) -> AppResult<String> {
    let docker = state.docker.get().await?;
    ensure_network(state).await?;
    let s = settings();

    let mut exposed = HashMap::new();
    exposed.insert(format!("{}/tcp", s.inner_app_port), HashMap::new());
    let mut port_bindings: PortMap = HashMap::new();
    port_bindings.insert(
        format!("{}/tcp", s.inner_app_port),
        Some(vec![PortBinding {
            host_ip: None,
            host_port: Some(inst.ext_port.to_string()),
        }]),
    );

    // PHP 扩展持久化：conf.d 挂到宿主持久目录，扩展安装后的 ini 重建不丢
    // （.so 本体由安装流程归档到 /app/.phpext，ini 内写绝对路径，见 panel.rs ext 归档步骤）
    let mut binds = vec![format!("{}:/app:rw", volume_src_for(inst))];
    if runtime_kind(&inst.image) == "php" {
        let confd = format!("{}/.phpconf", volume_src_for(inst));
        let _ = std::fs::create_dir_all(&confd);
        binds.push(format!("{}:/usr/local/etc/php/conf.d:rw", confd));
    }

    let mut host_config = HostConfig {
        binds: Some(binds),
        port_bindings: Some(port_bindings),
        nano_cpus: Some((inst.cpu_limit * 1e9) as i64),
        memory: Some(inst.mem_limit * 1024 * 1024),
        memory_swap: Some(inst.mem_limit * 1024 * 1024),
        pids_limit: Some(s.pids_limit),
        restart_policy: Some(RestartPolicy {
            name: Some(RestartPolicyNameEnum::UNLESS_STOPPED),
            maximum_retry_count: None,
        }),
        privileged: Some(false),
        network_mode: Some(s.docker_network.clone()),
        ..Default::default()
    };
    // 多租户加固：禁提权 + PID1 用 tini（信号正确转发，stop 更快更干净）
    // + 去掉卖容器用不到的危险 capability（NET_RAW 可伪造流量，MKNOD 可造设备）。
    // 不 drop 常规 cap、不开只读 rootfs：会破坏 pip/npm 安装，得不偿失。
    if s.container_hardening {
        host_config.security_opt = Some(vec!["no-new-privileges:true".to_string()]);
        host_config.init = Some(true);
        host_config.cap_drop = Some(vec![
            "NET_RAW".to_string(),
            "MKNOD".to_string(),
            "AUDIT_WRITE".to_string(),
            "SETPCAP".to_string(),
        ]);
    }

    let config = Config {
        image: Some(inst.image.clone()),
        cmd: Some(entry_cmd(inst)),
        working_dir: Some("/app".to_string()),
        env: Some(vec![
            "PYTHONUNBUFFERED=1".to_string(),
            // 应用监听端口约定：反代把外部流量打到这个容器内端口，启动命令里可用引用。
            // PORT 是业界惯例（Heroku/Cloud Run），大量框架/脚手架读它自动选端口；
            // INNER_APP_PORT 是平台原名，两者同值。
            format!("INNER_APP_PORT={}", s.inner_app_port),
            format!("PORT={}", s.inner_app_port),
            // PHP 内置服务器默认单进程：一个慢请求会阻塞全站。官方支持
            // PHP_CLI_SERVER_WORKERS 多进程预派生，固定 4 个 worker 实测
            // 3 个并发 2s 请求各自 2.0s 完成；对 python -S 无影响，无害。
            "PHP_CLI_SERVER_WORKERS=4".to_string(),
        ]),
        exposed_ports: Some(exposed),
        tty: Some(false),
        open_stdin: Some(false),
        host_config: Some(host_config),
        ..Default::default()
    };

    let name = container_name(inst.id);
    let opts = CreateContainerOptions {
        name: name.clone(),
        platform: None,
    };
    match docker.create_container(Some(opts.clone()), config.clone()).await {
        Ok(res) => Ok(res.id),
        Err(e) => {
            let msg = e.to_string();
            if msg.contains("already in use") || msg.contains("Conflict") {
                // 孤儿容器残留：移除后重建，用户文件保留
                let _ = docker
                    .remove_container(&name, Some(RemoveContainerOptions { force: true, ..Default::default() }))
                    .await;
                docker
                    .create_container(Some(opts), config)
                    .await
                    .map(|r| r.id)
                    .map_err(|e2| AppError::bad_gateway(format!("创建容器失败：{e2}")))
            } else {
                Err(AppError::bad_gateway(format!("创建容器失败：{e}")))
            }
        }
    }
}

pub async fn recreate_container(state: &AppState, inst: &Instance) -> AppResult<String> {
    let docker = state.docker.get().await?;
    let name = container_name(inst.id);
    if let Ok(info) = docker.inspect_container(&name, None).await {
        let running = info
            .state
            .as_ref()
            .and_then(|st| st.running)
            .unwrap_or(false);
        // 所有调用方（升级/重置/改启动命令/装扩展）都要求容器处于停止态再重建；
        // 运行中（含 crash 后的 restarting）自动温和停止，而不是报错打断流程
        if running {
            let _ = docker
                .stop_container(&name, Some(StopContainerOptions { t: 3 }))
                .await;
        }
        let _ = docker
            .remove_container(&name, Some(RemoveContainerOptions { force: true, ..Default::default() }))
            .await;
    }
    create_container(state, inst).await
}

pub async fn get_container(state: &AppState, inst: &Instance) -> AppResult<Value> {
    let docker = state.docker.get().await?;
    let name = container_name(inst.id);
    match docker.inspect_container(&name, None).await {
        Ok(v) => Ok(serde_json::to_value(v).unwrap_or(Value::Null)),
        Err(bollard::errors::Error::DockerResponseServerError { status_code: 404, .. }) => {
            Err(AppError::not_found("容器不存在，请尝试重新启动实例以自动重建"))
        }
        Err(e) => Err(AppError::bad_gateway(format!("Docker 调用失败：{e}"))),
    }
}

/// 用 Docker 真实状态校正 DB status；返回容器本次启动时间。
pub async fn sync_status(state: &AppState, inst: &mut Instance) -> Option<String> {
    let docker = state.docker.try_get().await?;
    let name = container_name(inst.id);
    let info = docker.inspect_container(&name, None).await.ok()?;
    let state_json = info.state.as_ref()?;
    let running = state_json.running.unwrap_or(false);
    // Docker 在崩溃重启循环里 Running=true、Status=restarting：服务其实完全不可用。
    // 必须单独成一种状态，否则面板与主控都会当"运行中"（绿灯、不告警）。
    let restarting = state_json.restarting.unwrap_or(false)
        || state_json
            .status
            .as_ref()
            .map(|s| format!("{s:?}").to_lowercase().contains("restarting"))
            .unwrap_or(false);
    let new_status = if restarting {
        "restarting"
    } else if running {
        "running"
    } else {
        "exited"
    };
    let new_id = info.id.clone().unwrap_or_default();
    let mut changed = false;
    if inst.status != new_status {
        inst.status = new_status.to_string();
        changed = true;
    }
    if inst.container_id.as_deref().unwrap_or("") != new_id && !new_id.is_empty() {
        inst.container_id = Some(new_id);
        changed = true;
    }
    if changed {
        let _ = sqlx::query("UPDATE instances SET status = ?, container_id = ? WHERE id = ?")
            .bind(&inst.status)
            .bind(inst.container_id.as_deref().unwrap_or(""))
            .bind(inst.id)
            .execute(&state.db)
            .await;
    }
    if new_status != "running" {
        return None;
    }
    let started = serde_json::to_value(&info)
        .ok()
        .and_then(|v| v["State"]["StartedAt"].as_str().map(|s| s.to_string()))
        .filter(|s| !s.is_empty());
    started
}

pub async fn container_stats(state: &AppState, inst: &Instance) -> Value {
    let mut empty = serde_json::json!({
        "running": false,
        "cpu_percent": 0.0,
        "mem_usage_mb": 0.0,
        "mem_limit_mb": inst.mem_limit,
        "mem_percent": 0.0,
        "net_rx_mb": 0.0,
        "net_tx_mb": 0.0,
        "traffic_gb": inst.traffic_gb,
        "traffic_used_mb": crate::models::round1(inst.traffic_used_mb),
        "disk_used_mb": serde_json::Value::Null,
        "rootfs_used_mb": 0.0,
    });
    empty["disk_used_mb"] = match dir_size_mb(&inst.host_dir) {
        Some(v) => serde_json::json!(v),
        None => Value::Null,
    };

    let Some(docker) = state.docker.try_get().await else {
        return empty;
    };
    let name = container_name(inst.id);
    let Ok(info) = docker.inspect_container(&name, None).await else {
        return empty;
    };
    let running = info.state.as_ref().and_then(|s| s.running).unwrap_or(false);
    let restarting = info
        .state
        .as_ref()
        .map(|s| {
            s.restarting.unwrap_or(false)
                || s.status
                    .as_ref()
                    .map(|v| format!("{v:?}").to_lowercase().contains("restarting"))
                    .unwrap_or(false)
        })
        .unwrap_or(false);
    // 崩溃重启循环中不取 stats（取到的是崩溃前的残值，会误导成"仍在跑"）
    if !running || restarting {
        return empty;
    }
    let mut stream = docker.stats(
        &name,
        Some(StatsOptions { stream: false, one_shot: true }),
    );
    let Some(Ok(stats)) = stream.next().await else {
        return empty;
    };
    let Ok(v) = serde_json::to_value(&stats) else {
        return empty;
    };

    let cpu_usage = v["cpu_stats"]["cpu_usage"]["total_usage"].as_u64().unwrap_or(0) as i64;
    let pre_cpu = v["precpu_stats"]["cpu_usage"]["total_usage"].as_u64().unwrap_or(0) as i64;
    let sys = v["cpu_stats"]["system_cpu_usage"].as_u64().unwrap_or(0) as i64;
    let pre_sys = v["precpu_stats"]["system_cpu_usage"].as_u64().unwrap_or(0) as i64;
    let online = v["cpu_stats"]["online_cpus"]
        .as_u64()
        .or_else(|| v["cpu_stats"]["cpu_usage"]["percpu_usage"].as_array().map(|a| a.len() as u64))
        .unwrap_or(1) as f64;
    let cpu_delta = cpu_usage - pre_cpu;
    let sys_delta = sys - pre_sys;
    let cpu_percent = if sys_delta > 0 {
        cpu_delta as f64 / sys_delta as f64 * online * 100.0
    } else {
        0.0
    };

    let usage = v["memory_stats"]["usage"].as_u64().unwrap_or(0) as i64;
    let cache = v["memory_stats"]["stats"]["cache"]
        .as_u64()
        .or_else(|| v["memory_stats"]["stats"]["total_inactive_file"].as_u64())
        .unwrap_or(0) as i64;
    let mem_used = (usage - cache).max(0);
    let limit = v["memory_stats"]["limit"]
        .as_u64()
        .unwrap_or(inst.mem_limit as u64 * 1024 * 1024) as i64;

    let (mut rx, mut tx) = (0i64, 0i64);
    if let Some(nets) = v["networks"].as_object() {
        for n in nets.values() {
            rx += n["rx_bytes"].as_u64().unwrap_or(0) as i64;
            tx += n["tx_bytes"].as_u64().unwrap_or(0) as i64;
        }
    }

    serde_json::json!({
        "running": true,
        "cpu_percent": crate::models::round1(cpu_percent),
        "mem_usage_mb": crate::models::round1(mem_used as f64 / 1024.0 / 1024.0),
        "mem_limit_mb": crate::models::round1(limit as f64 / 1024.0 / 1024.0),
        "mem_percent": if limit > 0 { crate::models::round1(mem_used as f64 / limit as f64 * 100.0) } else { 0.0 },
        "net_rx_mb": crate::models::round2(rx as f64 / 1024.0 / 1024.0),
        "net_tx_mb": crate::models::round2(tx as f64 / 1024.0 / 1024.0),
        "traffic_gb": inst.traffic_gb,
        "traffic_used_mb": crate::models::round1(inst.traffic_used_mb),
        "disk_used_mb": dir_size_mb(&inst.host_dir),
        "rootfs_used_mb": crate::models::round1(rootfs_used_mb(state, inst).await),
    })
}

pub fn dir_size_mb(path: &str) -> Option<f64> {
    if path.is_empty() || !std::path::Path::new(path).is_dir() {
        return None;
    }
    let out = std::process::Command::new("du").arg("-sm").arg(path).output().ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    text.split_whitespace().next()?.parse::<f64>().ok()
}

/// 容器可写层用量（MB）：容器内 `du -sm -x /`（-x 不跨 /app 挂载）减去镜像体积，
/// 10 分钟缓存（du 不便宜，不能每个 stats 请求都跑）。
/// 不走 inspect 的 GraphDriver：新版 Docker API 已不再返回该字段。
static ROOTFS_CACHE: OnceLock<Mutex<HashMap<i64, (f64, std::time::Instant)>>> = OnceLock::new();

async fn rootfs_used_mb(state: &AppState, inst: &Instance) -> f64 {
    let cache = ROOTFS_CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    if let Ok(guard) = cache.lock() {
        if let Some((v, t)) = guard.get(&inst.id) {
            if t.elapsed() < std::time::Duration::from_secs(600) {
                return *v;
            }
        }
    }
    let docker = match state.docker.try_get().await {
        Some(d) => d,
        None => return 0.0,
    };
    let cname = container_name(inst.id);
    let merged = match crate::services::backup::exec_demux(
        &docker,
        &cname,
        vec!["sh".into(), "-c".into(), "du -sm -x / 2>/dev/null".into()],
    )
    .await
    {
        Ok((0, out, _)) => out
            .split_whitespace()
            .next()
            .and_then(|x| x.parse::<f64>().ok())
            .unwrap_or(0.0),
        _ => return 0.0,
    };
    // 减去镜像本身占的体积（du 看到的是镜像+写入的合并视图）
    let image_mb = match docker.inspect_image(&inst.image).await {
        Ok(img) => img.size.unwrap_or(0) as f64 / 1024.0 / 1024.0,
        Err(_) => 0.0,
    };
    let v = (merged - image_mb).max(0.0);
    if let Ok(mut guard) = cache.lock() {
        guard.insert(inst.id, (v, std::time::Instant::now()));
    }
    v
}

pub async fn container_logs(state: &AppState, inst: &Instance, tail: i64) -> AppResult<String> {
    get_container(state, inst).await?;
    let docker = state.docker.get().await?;
    let mut stream = docker.logs(
        &container_name(inst.id),
        Some(LogsOptions::<String> {
            follow: false,
            stdout: true,
            stderr: true,
            since: 0,
            until: 0,
            timestamps: false,
            tail: tail.clamp(1, 5000).to_string(),
        }),
    );
    let mut out = String::new();
    while let Some(item) = stream.next().await {
        match item {
            Ok(bollard::container::LogOutput::StdOut { message })
            | Ok(bollard::container::LogOutput::StdErr { message })
            | Ok(bollard::container::LogOutput::Console { message })
            | Ok(bollard::container::LogOutput::StdIn { message }) => {
                out.push_str(&String::from_utf8_lossy(&message));
            }
            Err(e) => return Err(AppError::bad_gateway(format!("读取日志失败：{e}"))),
        }
    }
    Ok(out)
}

/// 启动后短暂观察：容器未能保持 running 时带回本次启动日志。
pub async fn diagnose_startup(state: &AppState, inst: &Instance) -> AppResult<()> {
    let Some(docker) = state.docker.try_get().await else {
        return Ok(());
    };
    let name = container_name(inst.id);
    let t0 = chrono::Utc::now().timestamp();
    for _ in 0..4 {
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        let Ok(info) = docker.inspect_container(&name, None).await else {
            return Ok(());
        };
        let st = info.state.as_ref();
        let running = st.and_then(|s| s.running).unwrap_or(false);
        if running {
            return Ok(());
        }
        let status = st
            .and_then(|s| s.status.as_ref())
            .map(|s| format!("{s:?}").to_lowercase())
            .unwrap_or_else(|| "unknown".into());
        if status.contains("exited") || status.contains("restarting") {
            let mut stream = docker.logs(
                &name,
                Some(LogsOptions::<String> {
                    follow: false,
                    stdout: true,
                    stderr: true,
                    since: (t0 - 2).max(0),
                    until: 0,
                    timestamps: false,
                    tail: "15".to_string(),
                }),
            );
            let mut text = String::new();
            while let Some(Ok(line)) = stream.next().await {
                match line {
                    bollard::container::LogOutput::StdOut { message }
                    | bollard::container::LogOutput::StdErr { message }
                    | bollard::container::LogOutput::Console { message }
                    | bollard::container::LogOutput::StdIn { message } => {
                        text.push_str(&String::from_utf8_lossy(&message));
                    }
                }
            }
            let tail = if text.trim().is_empty() { "(本次启动无新日志)".to_string() } else { text.trim().to_string() };
            return Err(AppError::bad_request(format!(
                "容器未能保持运行（状态：{status}）。本次启动日志：\n{tail}"
            )));
        }
    }
    Ok(())
}

fn env_ok(info: &Value) -> bool {
    info["Config"]["Env"]
        .as_array()
        .map(|a| a.iter().any(|v| v.as_str() == Some("PYTHONUNBUFFERED=1")))
        .unwrap_or(false)
}

/// 非运行中且入口命令/环境过期时重建容器。
async fn ensure_entry_cmd(state: &AppState, inst: &mut Instance) -> AppResult<()> {
    let docker = state.docker.get().await?;
    let name = container_name(inst.id);
    let info = docker
        .inspect_container(&name, None)
        .await
        .map_err(|e| AppError::bad_gateway(format!("Docker 调用失败：{e}")))?;
    let v = serde_json::to_value(&info).unwrap_or(Value::Null);
    let running = info.state.as_ref().and_then(|s| s.running).unwrap_or(false);
    let cur_cmd: Vec<String> = v["Config"]["Cmd"]
        .as_array()
        .map(|a| a.iter().map(|x| x.as_str().unwrap_or("").to_string()).collect())
        .unwrap_or_default();
    // 加固漂移：升级后被控开启了加固而旧容器没有（或反之），下次启动时重建收敛
    let hardened = v["HostConfig"]["SecurityOpt"]
        .as_array()
        .map(|a| a.iter().any(|x| x.as_str().unwrap_or("").contains("no-new-privileges")))
        .unwrap_or(false);
    let harden_drift = settings().container_hardening != hardened;

    if (cur_cmd != entry_cmd(inst) || !env_ok(&v) || harden_drift) && !running {
        eprintln!("[instance {}] 启动参数与登记不一致，重建容器 {name}", inst.id);
        let _ = docker
            .remove_container(&name, Some(RemoveContainerOptions { force: true, ..Default::default() }))
            .await;
        inst.container_id = Some(create_container(state, inst).await?);
    }
    Ok(())
}

pub async fn start_instance(state: &AppState, inst: &mut Instance) -> AppResult<()> {
    let docker = state.docker.get().await?;
    let name = container_name(inst.id);
    let exists = docker.inspect_container(&name, None).await.is_ok();
    if !exists {
        inst.container_id = Some(create_container(state, inst).await?);
    }
    ensure_entry_cmd(state, inst).await?;

    let info = docker.inspect_container(&name, None).await.ok();
    let restarting = info
        .as_ref()
        .and_then(|i| i.state.as_ref())
        .and_then(|s| s.restarting)
        .unwrap_or(false);
    if restarting {
        let _ = docker
            .stop_container(&name, Some(StopContainerOptions { t: 5 }))
            .await;
    }
    docker
        .start_container(&name, None::<bollard::container::StartContainerOptions<String>>)
        .await
        .map_err(|e| AppError::bad_gateway(format!("启动失败：{e}")))?;
    // 启动后确认容器仍存在：容器被并发清理/秒退并移除时，不能对外回 200（否则面板显示运行中但实际没有）
    if let Err(e) = docker.inspect_container(&name, None).await {
        return Err(AppError::bad_gateway(format!("启动后未找到容器 {name}：{e}")));
    }
    inst.status = "running".to_string();
    Ok(())
}

pub async fn stop_instance(state: &AppState, inst: &mut Instance) -> AppResult<()> {
    get_container(state, inst).await?;
    let docker = state.docker.get().await?;
    let name = container_name(inst.id);
    let already = docker
        .inspect_container(&name, None)
        .await
        .ok()
        .and_then(|i| i.state)
        .map(|s| !s.running.unwrap_or(false))
        .unwrap_or(false);
    // 宽限期 3s：容器内 PID 1 是 sh 时不转发 SIGTERM，要等满宽限期才 SIGKILL；
    // 原来 10s 让每次停止/重启白等十秒，3s 是"给应用落盘"与"别让用户干等"的折中。
    if !already {
        docker
            .stop_container(&name, Some(StopContainerOptions { t: 3 }))
            .await
            .map_err(|e| AppError::bad_gateway(format!("停止失败：{e}")))?;
    }
    inst.status = "exited".to_string();
    Ok(())
}

pub async fn restart_instance(state: &AppState, inst: &mut Instance) -> AppResult<()> {
    get_container(state, inst).await?;
    let docker = state.docker.get().await?;
    docker
        .stop_container(&container_name(inst.id), Some(StopContainerOptions { t: 3 }))
        .await
        .map_err(|e| AppError::bad_gateway(format!("重启失败：{e}")))?;
    ensure_entry_cmd(state, inst).await?;
    docker
        .start_container(&container_name(inst.id), None::<bollard::container::StartContainerOptions<String>>)
        .await
        .map_err(|e| AppError::bad_gateway(format!("重启失败：{e}")))?;
    inst.status = "running".to_string();
    Ok(())
}

pub async fn remove_container(state: &AppState, inst: &Instance) -> AppResult<()> {
    let Some(docker) = state.docker.try_get().await else {
        return Err(AppError::bad_gateway(
            "Docker 不可用，为避免产生孤儿容器已中止删除，请稍后重试",
        ));
    };
    let name = container_name(inst.id);
    if docker.inspect_container(&name, None).await.is_err() {
        return Ok(());
    }
    let _ = docker
        .stop_container(&name, Some(StopContainerOptions { t: 5 }))
        .await;
    docker
        .remove_container(&name, Some(RemoveContainerOptions { force: true, ..Default::default() }))
        .await
        .map_err(|e| AppError::bad_gateway(format!("容器删除失败（登记未删除，可重试）：{e}")))?;
    Ok(())
}

/// 对账兜底：清理已无登记的实例容器（只匹配 {前缀}{纯数字}）。
pub async fn reconcile_orphans(state: &AppState) -> i64 {
    let Some(docker) = state.docker.try_get().await else {
        return 0;
    };
    let prefix = container_prefix();
    // 注意：查询失败必须直接放弃本轮对账。若把失败当成空集，后面会把所有 ppanel-* 容器
    // 都判成「孤儿」并强制删除——一次瞬时数据库错误就能清掉整台节点上正在跑的实例容器。
    let known_ids: Vec<i64> = match sqlx::query_scalar("SELECT id FROM instances")
        .fetch_all(&state.db)
        .await
    {
        Ok(v) => v,
        Err(e) => {
            eprintln!("[reconcile] 跳过本轮孤儿清理（读取实例列表失败：{e}）");
            return 0;
        }
    };
    let known: HashSet<String> = known_ids.into_iter().map(|i| format!("{prefix}{i}")).collect();
    let mut removed = 0;
    let opts = bollard::container::ListContainersOptions::<String> {
        all: true,
        ..Default::default()
    };
    if let Ok(list) = docker.list_containers(Some(opts)).await {
        for c in list {
            let names = c.names.unwrap_or_default();
            for raw in names {
                let name = raw.trim_start_matches('/').to_string();
                let Some(rest) = name.strip_prefix(&prefix) else { continue };
                if rest.is_empty() || !rest.chars().all(|ch| ch.is_ascii_digit()) {
                    continue;
                }
                if known.contains(&name) {
                    continue;
                }
                if docker
                    .remove_container(&name, Some(RemoveContainerOptions { force: true, ..Default::default() }))
                    .await
                    .is_ok()
                {
                    removed += 1;
                    println!("[reconcile] 已清理孤儿容器 {name}（无登记记录，数据目录保留）");
                }
            }
        }
    }
    removed
}

pub async fn runtime_versions(state: &AppState, inst: &Instance) -> AppResult<Value> {
    let docker = state.docker.get().await?;
    let local: HashSet<String> = docker
        .list_images::<String>(None)
        .await
        .map(|l| l.into_iter().flat_map(|i| i.repo_tags).collect())
        .unwrap_or_default();
    let kind = runtime_kind(&inst.image);
    let allowed = allowed_images(state).await;
    let mut versions: Vec<String> = allowed
        .into_iter()
        .filter(|v| local.contains(v) && runtime_kind(v) == kind)
        .collect();
    if !versions.iter().any(|v| v == &inst.image) {
        versions.insert(0, inst.image.clone());
    }
    Ok(serde_json::json!({"image": inst.image, "versions": versions}))
}

pub async fn switch_runtime(state: &AppState, inst: &Instance, img: &str) -> AppResult<Value> {
    let img = img.trim().to_string();
    let allowed = allowed_images(state).await;
    if !allowed.contains(&img) {
        return Err(AppError::bad_request("不支持的镜像版本"));
    }
    if runtime_kind(&img) != runtime_kind(&inst.image) {
        return Err(AppError::bad_request(
            "仅支持同类型运行环境内切换（Python / PHP / Node 各自的版本间）",
        ));
    }
    if img == inst.image {
        return Ok(serde_json::json!({"status": inst.status, "image": img, "detail": "当前已是该版本"}));
    }
    let docker = state.docker.get().await?;
    match docker.inspect_image(&img).await {
        Ok(_) => {}
        Err(bollard::errors::Error::DockerResponseServerError { status_code: 404, .. }) => {
            return Err(AppError::not_found("此版本不支持，请联系管理员。"))
        }
        Err(e) => return Err(AppError::bad_gateway(format!("Docker 调用失败：{e}"))),
    }
    state.jobs.cleanup_finished(inst.id);
    let job = state.jobs.insert(ExecJob::new(
        inst.id,
        vec![],
        format!("runtime-switch → {img}"),
    ));
    let st = state.clone();
    let iid = inst.id;
    let job_id = job.id.clone();
    tokio::spawn(async move {
        runtime_worker(st, job, iid, img).await;
    });
    Ok(serde_json::json!({"status": inst.status, "image": inst.image, "job_id": job_id}))
}

async fn runtime_worker(state: AppState, job: Arc<ExecJob>, iid: i64, img: String) {
    let mut inst = match fetch_instance(&state.db, iid).await {
        Ok(i) => i,
        Err(_) => {
            job.emit("[切换] 实例不存在");
            job.finish(-1);
            return;
        }
    };
    job.emit(format!("[1/4] 目标运行环境：{img}"));
    let was_running = inst.status == "running";
    if was_running {
        job.emit("[2/4] 停止当前容器…");
        let _ = stop_instance(&state, &mut inst).await;
    } else {
        job.emit("[2/4] 实例未在运行，跳过停止");
    }
    let old = inst.image.clone();
    inst.image = img.clone();
    if inst.start_cmd.trim() == default_start_cmd(&old, Some(inst.mem_limit)).trim() {
        // 平台默认命令不落库：置空，entry_cmd 会按新镜像的默认入口/入口文件自动收敛
        inst.start_cmd = String::new();
        job.emit("[提示] 启动命令为平台默认，已随运行环境自动适配");
    }
    job.emit("[3/4] 移除旧容器并按新镜像重建（数据目录与访问端口不变）…");
    let _ = persist_image(&state.db, &inst).await;
    if recreate_container(&state, &inst).await.is_err() {
        inst.image = old;
        let _ = recreate_container(&state, &inst).await;
        let _ = persist_image(&state.db, &inst).await;
        job.emit("[切换] 失败：重建容器失败");
        job.finish(-1);
        return;
    }
    if was_running {
        job.emit("[4/4] 启动实例…");
        let _ = start_instance(&state, &mut inst).await;
        let _ = persist_status(&state.db, &inst).await;
        match diagnose_startup(&state, &inst).await {
            Ok(_) => job.emit(format!("[切换] 完成！服务已恢复运行（{img}）")),
            Err(e) => {
                job.emit(format!("[切换] 容器已切换，但应用启动异常：{}", e.detail));
                job.emit("请到「日志」页查看详情");
                job.finish(1);
                return;
            }
        }
    } else {
        job.emit(format!("[切换] 完成！下次启动将使用 {img}"));
    }
    let _ = log_op(&state.db, inst.id, "runtime", &format!("运行环境切换为 {img}")).await;
    job.emit("[提示] 容器已重建，容器内依赖被重置；Python 实例下次启动会自动重装 requirements.txt（日志可见）");
    job.finish(0);
}

pub async fn fetch_instance(pool: &SqlitePool, id: i64) -> AppResult<Instance> {
    sqlx::query_as::<_, Instance>("SELECT * FROM instances WHERE id = ?")
        .bind(id)
        .fetch_optional(pool)
        .await?
        .ok_or_else(|| AppError::not_found("实例不存在"))
}

pub async fn persist_status(pool: &SqlitePool, inst: &Instance) -> AppResult<()> {
    sqlx::query("UPDATE instances SET status = ?, container_id = ? WHERE id = ?")
        .bind(&inst.status)
        .bind(inst.container_id.as_deref().unwrap_or(""))
        .bind(inst.id)
        .execute(pool)
        .await?;
    Ok(())
}

async fn persist_image(pool: &SqlitePool, inst: &Instance) -> AppResult<()> {
    sqlx::query("UPDATE instances SET image = ?, start_cmd = ? WHERE id = ?")
        .bind(&inst.image)
        .bind(&inst.start_cmd)
        .bind(inst.id)
        .execute(pool)
        .await?;
    Ok(())
}

// ---------- 容器内执行（依赖安装 / pecl 等） ----------

async fn require_running(state: &AppState, inst: &Instance) -> AppResult<()> {
    let docker = state.docker.get().await?;
    let info = docker
        .inspect_container(&container_name(inst.id), None)
        .await
        .map_err(|_| AppError::conflict("容器未运行，请先启动实例"))?;
    if !info.state.as_ref().and_then(|s| s.running).unwrap_or(false) {
        return Err(AppError::conflict("容器未运行，请先启动实例"));
    }
    Ok(())
}

pub async fn start_exec_job(
    state: &AppState,
    inst: &Instance,
    cmd: Vec<String>,
    desc: &str,
) -> AppResult<Arc<ExecJob>> {
    require_running(state, inst).await?;
    state.jobs.cleanup_finished(inst.id);
    let job = state.jobs.insert(ExecJob::new(inst.id, cmd, desc));
    let st = state.clone();
    let j = job.clone();
    tokio::spawn(async move { exec_worker(st, j).await });
    Ok(job)
}

pub async fn start_install(state: &AppState, inst: &Instance, file_name: &str) -> AppResult<Arc<ExecJob>> {
    // 按运行时分派：node=package.json → npm install；go=go.mod → go mod tidy；
    // php 无 composer 走不了文件安装；其余（python）维持 pip install -r。
    match runtime_kind(&inst.image) {
        "node" => {
            start_exec_job(
                state,
                inst,
                vec!["npm".into(), "install".into()],
                "",
            )
            .await
        }
        "go" => {
            start_exec_job(
                state,
                inst,
                vec!["go".into(), "mod".into(), "tidy".into()],
                "",
            )
            .await
        }
        "php" => Err(AppError::bad_request(
            "PHP 环境暂不支持依赖文件安装（无 composer），请使用面板扩展管理或控制台执行",
        )),
        _ => {
            start_exec_job(
                state,
                inst,
                vec!["pip".into(), "install".into(), "-r".into(), file_name.to_string()],
                "",
            )
            .await
        }
    }
}

pub async fn start_pip(state: &AppState, inst: &Instance, args: &[String]) -> AppResult<Arc<ExecJob>> {
    let mut cmd = vec!["pip".to_string()];
    cmd.extend(args.iter().cloned());
    start_exec_job(state, inst, cmd.clone(), &cmd.join(" ")).await
}

async fn exec_worker(state: AppState, job: Arc<ExecJob>) {
    let docker = match state.docker.get().await {
        Ok(d) => d,
        Err(e) => {
            job.emit(format!("[面板] 执行出错：{}", e.detail));
            job.finish(-1);
            return;
        }
    };
    let name = container_name(job.instance_id);
    let Ok(info) = docker.inspect_container(&name, None).await else {
        job.emit("[面板] 容器已停止，安装中止");
        job.finish(-1);
        return;
    };
    if !info.state.as_ref().and_then(|s| s.running).unwrap_or(false) {
        job.emit("[面板] 容器已停止，安装中止");
        job.finish(-1);
        return;
    }
    let exec = docker
        .create_exec(
            &name,
            CreateExecOptions {
                cmd: Some(job.cmd.clone()),
                attach_stdout: Some(true),
                attach_stderr: Some(true),
                working_dir: Some("/app".to_string()),
                ..Default::default()
            },
        )
        .await;
    let exec_id = match exec {
        Ok(e) => e.id,
        Err(e) => {
            job.emit(format!("[面板] 执行出错：{e}"));
            job.finish(-1);
            return;
        }
    };
    let desc = if job.desc.is_empty() {
        format!("pip {}", job.cmd.iter().skip(1).cloned().collect::<Vec<_>>().join(" "))
    } else {
        job.desc.clone()
    };
    job.emit(format!("[面板] 执行：{desc}"));

    let start = docker
        .start_exec(
            &exec_id,
            Some(StartExecOptions {
                detach: false,
                tty: true,
                output_capacity: None,
            }),
        )
        .await;
    match start {
        Ok(StartExecResults::Attached { mut output, .. }) => {
            while let Some(item) = output.next().await {
                if let Ok(line) = item {
                    match line {
                        bollard::container::LogOutput::StdOut { message }
                        | bollard::container::LogOutput::StdErr { message }
                        | bollard::container::LogOutput::Console { message }
                        | bollard::container::LogOutput::StdIn { message } => {
                            job.emit(String::from_utf8_lossy(&message).to_string());
                        }
                    }
                }
            }
        }
        Ok(StartExecResults::Detached) => {
            job.emit("[面板] 任务已后台执行");
        }
        Err(e) => {
            job.emit(format!("[面板] 执行出错：{e}"));
            job.finish(-1);
            return;
        }
    }
    let code = docker
        .inspect_exec(&exec_id)
        .await
        .ok()
        .and_then(|i| i.exit_code)
        .unwrap_or(-1);
    job.emit(format!(
        "[面板] 任务完成，退出码 {code}{}",
        if code == 0 { "（成功）" } else { "（失败）" }
    ));
    job.finish(code);
}

pub async fn exec_capture(
    state: &AppState,
    inst: &Instance,
    cmd: Vec<String>,
    timeout: std::time::Duration,
) -> AppResult<(i64, String)> {
    let docker = state.docker.get().await?;
    let name = container_name(inst.id);
    let exec = docker
        .create_exec(
            &name,
            CreateExecOptions {
                cmd: Some(cmd),
                attach_stdout: Some(true),
                attach_stderr: Some(true),
                working_dir: Some("/app".to_string()),
                ..Default::default()
            },
        )
        .await?;
    let exec_id = exec.id;
    let fut = async {
        let start = docker
            .start_exec(&exec_id, Some(StartExecOptions { detach: false, tty: true, output_capacity: None }))
            .await?;
        let mut buf = String::new();
        if let StartExecResults::Attached { mut output, .. } = start {
            while let Some(item) = output.next().await {
                if let Ok(line) = item {
                    match line {
                        bollard::container::LogOutput::StdOut { message }
                        | bollard::container::LogOutput::StdErr { message }
                        | bollard::container::LogOutput::Console { message }
                        | bollard::container::LogOutput::StdIn { message } => {
                            buf.push_str(&String::from_utf8_lossy(&message));
                        }
                    }
                }
            }
        }
        Ok::<String, bollard::errors::Error>(buf)
    };
    let out = match tokio::time::timeout(timeout, fut).await {
        Err(_) => return Ok((124, "命令超时".to_string())),
        Ok(Err(e)) => return Err(AppError::bad_gateway(format!("执行失败：{e}"))),
        Ok(Ok(v)) => v,
    };
    let code = docker
        .inspect_exec(&exec_id)
        .await
        .ok()
        .and_then(|i| i.exit_code)
        .unwrap_or(-1);
    Ok((code, out))
}

/// 交互式 exec（WebSocket 终端用）：返回 (输出流接收端, 输入发送端)。
pub async fn exec_interactive(
    state: &AppState,
    inst: &Instance,
    cmd: Vec<String>,
) -> AppResult<(mpsc::Receiver<String>, mpsc::Sender<Vec<u8>>)> {
    use bollard::exec::ResizeExecOptions;
    let docker = state.docker.get().await?;
    let exec = docker
        .create_exec(
            &container_name(inst.id),
            CreateExecOptions {
                cmd: Some(cmd),
                attach_stdin: Some(true),
                attach_stdout: Some(true),
                attach_stderr: Some(true),
                tty: Some(true),
                working_dir: Some("/app".to_string()),
                ..Default::default()
            },
        )
        .await?;
    let exec_id = exec.id.clone();
    let start = docker
        .start_exec(
            &exec_id,
            Some(StartExecOptions { detach: false, tty: true, output_capacity: Some(64 * 1024) }),
        )
        .await?;
    let (out_tx, out_rx) = mpsc::channel::<String>(4096);
    let (in_tx, mut in_rx) = mpsc::channel::<Vec<u8>>(256);
    if let StartExecResults::Attached { mut output, mut input } = start {
        let d = docker.clone();
        let eid = exec_id.clone();
        tokio::spawn(async move {
            let d2 = d.clone();
            let eid2 = eid.clone();
            let writer = tokio::spawn(async move {
                use tokio::io::AsyncWriteExt;
                while let Some(data) = in_rx.recv().await {
                    if input.write_all(&data).await.is_err() {
                        break;
                    }
                    let _ = input.flush().await;
                }
                let _ = d2.resize_exec(&eid2, ResizeExecOptions { height: 30, width: 120 }).await;
            });
            while let Some(item) = output.next().await {
                if let Ok(line) = item {
                    match line {
                        bollard::container::LogOutput::StdOut { message }
                        | bollard::container::LogOutput::StdErr { message }
                        | bollard::container::LogOutput::Console { message }
                        | bollard::container::LogOutput::StdIn { message } => {
                            if out_tx.send(String::from_utf8_lossy(&message).to_string()).await.is_err() {
                                break;
                            }
                        }
                    }
                }
            }
            writer.abort();
        });
    }
    Ok((out_rx, in_tx))
}

pub async fn pip_list(state: &AppState, inst: &Instance) -> AppResult<Vec<Value>> {
    require_running(state, inst).await?;
    let (code, out) = exec_capture(
        state,
        inst,
        vec!["pip".into(), "list".into(), "--format=json".into()],
        std::time::Duration::from_secs(60),
    )
    .await?;
    if code != 0 {
        return Err(AppError::internal("获取依赖列表失败"));
    }
    let start = out.find('[').ok_or_else(|| AppError::internal("解析依赖列表失败"))?;
    let value: Value = serde_json::from_str(&out[start..]).map_err(|_| AppError::internal("解析依赖列表失败"))?;
    let arr = value.as_array().cloned().ok_or_else(|| AppError::internal("解析依赖列表失败"))?;
    Ok(arr
        .into_iter()
        .map(|d| serde_json::json!({"name": d["name"], "version": d["version"]}))
        .collect())
}

// ---------- 被控自持：操作记录 / 用量采样 ----------

pub async fn log_op(pool: &SqlitePool, instance_id: i64, action: &str, detail: &str) -> AppResult<()> {
    sqlx::query("INSERT INTO agent_op_logs (instance_id, action, detail, created_at) VALUES (?,?,?,?)")
        .bind(instance_id)
        .bind(action)
        .bind(detail)
        .bind(utcnow())
        .execute(pool)
        .await?;
    Ok(())
}

pub async fn sample_all_metrics(state: &AppState) -> AppResult<()> {
    let rows: Vec<Instance> =
        sqlx::query_as("SELECT * FROM instances WHERE status = 'running'")
            .fetch_all(&state.db)
            .await?;
    for mut inst in rows {
        let s = container_stats(state, &inst).await;
        let cpu = s["cpu_percent"].as_f64().unwrap_or(0.0);
        let mem_used = s["mem_usage_mb"].as_f64().unwrap_or(0.0);
        let mem_limit = s["mem_limit_mb"].as_f64().unwrap_or(inst.mem_limit as f64);
        let rx = s["net_rx_mb"].as_f64().unwrap_or(0.0);
        let tx = s["net_tx_mb"].as_f64().unwrap_or(0.0);
        let _ = sqlx::query(
            "INSERT INTO agent_metrics (instance_id, ts, cpu_percent, mem_used_mb, mem_limit_mb, net_rx_mb, net_tx_mb) \
             VALUES (?,?,?,?,?,?,?)",
        )
        .bind(inst.id)
        .bind(utcnow())
        .bind(cpu)
        .bind(mem_used)
        .bind(mem_limit)
        .bind(rx)
        .bind(tx)
        .execute(&state.db)
        .await;

        // 本期流量累计
        let cur = rx + tx;
        let month = chrono::Utc::now().format("%Y-%m").to_string();
        if inst.traffic_month != month {
            inst.traffic_used_mb = 0.0;
            inst.traffic_month = month;
        } else if cur >= inst.traffic_last_mb {
            inst.traffic_used_mb = crate::models::round2(inst.traffic_used_mb + (cur - inst.traffic_last_mb));
        } else {
            inst.traffic_used_mb = crate::models::round2(inst.traffic_used_mb + cur);
        }
        inst.traffic_last_mb = crate::models::round2(cur);
        let _ = sqlx::query(
            "UPDATE instances SET traffic_used_mb = ?, traffic_last_mb = ?, traffic_month = ? WHERE id = ?",
        )
        .bind(inst.traffic_used_mb)
        .bind(inst.traffic_last_mb)
        .bind(&inst.traffic_month)
        .bind(inst.id)
        .execute(&state.db)
        .await;
    }
    let cutoff = utcnow() - chrono::Duration::hours(24);
    sqlx::query("DELETE FROM agent_metrics WHERE ts < ?")
        .bind(cutoff)
        .execute(&state.db)
        .await
        .ok();
    Ok(())
}

// ---------- PHP 禁用函数（实例级自选，存 disabled_funcs 列，不进启动命令） ----------

/// 节点默认禁用函数列表（PHP_DISABLE_FUNCTIONS）。
pub fn default_disabled_funcs() -> Vec<String> {
    settings()
        .php_disable_functions
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// 实例当前生效的禁用函数：disabled_funcs 列为空 = 节点默认。
pub fn effective_disabled(inst: &Instance) -> Vec<String> {
    let raw = inst.disabled_funcs.trim();
    if raw.is_empty() {
        return default_disabled_funcs();
    }
    raw.split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect()
}

/// 剥掉 PHP 启动命令里的 `-d key=value` 运行参数段（历史数据迁移用）。
/// 老版本把禁用函数/上传/内存参数拼进了启动命令落库，现改由容器入口动态注入。
pub fn strip_php_flags(cmd: &str) -> String {
    let re = regex::Regex::new(r"\s*-d\s+\S+").expect("内置正则编译失败");
    re.replace_all(cmd, "").trim().to_string()
}
