"""实例服务：容器创建/启停/删除、端口分配、依赖安装任务、状态同步。"""
import json
import os
import queue
import random
import socket
import subprocess
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

import docker
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.docker_client import get_docker, try_get_docker
from app.models import AgentMetric, AgentOpLog, Instance
from app.services import file_service as fs

CONTAINER_PREFIX = "ppanel-"

# 依赖安装任务注册表：job_id -> ExecJob（内存态，重启面板即清空）
EXEC_JOBS: dict[str, "ExecJob"] = {}


def container_prefix() -> str:
    """本进程管理的容器名前缀：被控可通过 CONTAINER_PREFIX 环境变量覆盖（如 pagent-）。"""
    import os
    return os.environ.get("CONTAINER_PREFIX", "ppanel-")


def container_name(instance_id: int) -> str:
    # 被控进程可通过 CONTAINER_PREFIX 环境变量换前缀（如 pagent-），
    # 避免与同宿主机上旧版面板的容器撞名
    return f"{container_prefix()}{instance_id}"


def host_dir_for(instance_id: int) -> str:
    return os.path.join(settings.data_root, str(instance_id))


def volume_src_for(inst: Instance) -> str:
    """容器挂载源路径：跨系统部署时可与面板本机路径不同根。"""
    if settings.docker_volume_root:
        return f"{settings.docker_volume_root.rstrip('/')}/{inst.id}"
    return inst.host_dir


def ensure_network(client: docker.DockerClient) -> None:
    """安全红线 3：所有实例容器挂在独立 network 内，与默认 bridge 隔离。"""
    try:
        client.networks.get(settings.docker_network)
    except docker.errors.NotFound:
        try:
            client.networks.create(settings.docker_network, driver="bridge")
        except docker.errors.APIError:
            pass  # 并发创建等场景，忽略


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def alloc_port(db: Session) -> int:
    """在 PORT_START-PORT_END 内随机起点扫描空闲端口，避开已分配的。"""
    used = {row[0] for row in db.query(Instance.ext_port).all()}
    total = settings.port_end - settings.port_start + 1
    offset = random.randrange(total)
    for i in range(total):
        port = settings.port_start + (offset + i) % total
        if port in used:
            continue
        if _port_free(port):
            return port
    raise HTTPException(status_code=503, detail="外部端口已耗尽，无可用端口")


def ensure_image(client: docker.DockerClient, image: str) -> None:
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        try:
            client.images.pull(image)
        except docker.errors.APIError as e:
            raise HTTPException(status_code=502, detail=f"拉取镜像失败：{e}")


def is_php(image: str) -> bool:
    """按镜像前缀判断运行时类型（php:* 官方与 ppanel-php:* 增强镜像均为 PHP）。"""
    img = image or ""
    return img.startswith("php:") or img.startswith("ppanel-php:")


def runtime_kind(image: str) -> str:
    """运行时类型：php / node / go / python（缺省）。同类型内才允许互相切换。"""
    img = image or ""
    if img.startswith("node:"):
        return "node"
    if img.startswith("golang:"):
        return "go"
    if is_php(img):
        return "php"
    return "python"


def built_images(client: docker.DockerClient | None = None) -> list[str]:
    """节点本地已构建的增强镜像（ppanel-php:8.x-full），构建完成即自动入白名单。"""
    client = client or try_get_docker()
    if client is None:
        return []
    try:
        tags = [t for img in client.images.list() for t in (img.tags or [])]
    except docker.errors.APIError:
        return []
    return sorted(t for t in tags if t.startswith("ppanel-php:") and t.endswith("-full"))


def allowed_images(client: docker.DockerClient | None = None) -> list[str]:
    """可用运行环境 = 静态白名单（python/php 官方镜像）+ 节点本地已构建的增强镜像。"""
    return settings.all_images + built_images(client)


def default_start_cmd(image: str, mem_limit_mb: int | None = None) -> str:
    """新实例默认启动命令：PHP 用内置服务器，-d 注入禁用函数/上传上限/内存限额
    （烧进容器入口，重建不丢）；Node 跑 index.js；Go 先 build 再跑（产物持久）；
    Python 先自动装 requirements.txt（有则装，幂等；pip 读容器 /etc/pip.conf 的
    加速源），个别包装失败不阻塞启动，由应用 import 时直观报错。"""
    if runtime_kind(image) == "node":
        return "node index.js"
    if runtime_kind(image) == "go":
        # 无 go.mod 自动 init（零依赖示例可跑）；build 产物 /app/app 持久，重启增量编译秒级
        return "[ -f go.mod ] || go mod init ppanel-app; go build -o app . && ./app"
    if not is_php(image):
        return ("if [ -f requirements.txt ]; then echo '[依赖] 检测到 requirements.txt，安装中...'; "
                "pip install -r requirements.txt --no-input --disable-pip-version-check "
                "|| echo '[依赖] 部分包安装失败，仍尝试启动'; fi; python main.py")
    parts = ["php"]
    if settings.php_disable_functions.strip():
        parts.append(f"-d disable_functions={settings.php_disable_functions.strip()}")
    upload = settings.upload_limit_mb
    parts.append(f"-d upload_max_filesize={upload}M")
    parts.append(f"-d post_max_size={upload + 16}M")
    if mem_limit_mb:
        parts.append(f"-d memory_limit={int(mem_limit_mb)}M")
    parts.append("-S 0.0.0.0:8000 -t /app")
    return " ".join(parts)


def entry_filename(image: str) -> str:
    """入口文件名：php 镜像找 index.php，node 找 index.js，go 找 main.go，其余找 main.py。"""
    kind = runtime_kind(image)
    if kind == "php":
        return "index.php"
    if kind == "node":
        return "index.js"
    if kind == "go":
        return "main.go"
    return "main.py"


_DISABLE_RE = None


def parse_disabled_from_cmd(start_cmd: str) -> list[str] | None:
    """从启动命令解析 disable_functions：None=命令里没有该段；[]=空段（全开放）；否则为禁用列表。"""
    global _DISABLE_RE
    if _DISABLE_RE is None:
        import re as _re
        _DISABLE_RE = _re.compile(r"-d\s+disable_functions=(\S*)")
    m = _DISABLE_RE.search(start_cmd or "")
    if m is None:
        return None
    seg = m.group(1).strip()
    return [f for f in seg.split(",") if f]


def apply_disabled_to_cmd(start_cmd: str, funcs: list[str]) -> str:
    """把禁用列表写回 PHP 启动命令的 -d disable_functions 段（无段则插入 php 之后）。"""
    seg = ",".join(funcs)
    global _DISABLE_RE
    if _DISABLE_RE is None:
        import re as _re
        _DISABLE_RE = _re.compile(r"-d\s+disable_functions=(\S*)")
    if _DISABLE_RE.search(start_cmd or ""):
        return _DISABLE_RE.sub(lambda _: f"-d disable_functions={seg}", start_cmd, count=1)
    parts = (start_cmd or "").split()
    if parts and parts[0] == "php":
        parts.insert(1, f"-d disable_functions={seg}")
    else:
        parts.append(f"-d disable_functions={seg}")
    return " ".join(parts)


# 新实例的默认入口文件：常驻 HTTP 服务，保证“创建→启动→访问端口”开箱即通
_DEFAULT_MAIN_PY = '''from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = "PPanel instance is running. Edit /app/main.py and restart.\\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):
        pass


print("PPanel instance starting on 0.0.0.0:8000 ...")
HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
'''

_DEFAULT_INDEX_PHP = '''<?php
header("Content-Type: text/plain; charset=utf-8");
echo "PPanel PHP instance is running. Edit /app/index.php and restart.\\n";
echo "PHP version: " . PHP_VERSION . "\\n";
'''

_DEFAULT_INDEX_JS = '''// PPanel Node 实例示例入口：开箱即跑，改完代码点重启生效
const http = require("http");

http.createServer((req, res) => {
  res.writeHead(200, { "Content-Type": "text/plain; charset=utf-8" });
  res.end(`PPanel Node instance is running. Edit /app/index.js and restart.\\nNode version: ${process.version}\\n`);
}).listen(8000, "0.0.0.0", () => console.log("PPanel instance starting on 0.0.0.0:8000 ..."));
'''

_DEFAULT_MAIN_GO = '''// PPanel Go 实例示例入口：零第三方依赖，开箱即跑。
// 默认命令 go build -o app . && ./app——首次启动编译约 1-3 分钟（受 CPU 限额影响），之后重启秒级。
// 需要第三方库时：面板「依赖」页一键同步（go mod tidy），或本地上传带 go.mod 的项目。
package main

import (
	"fmt"
	"net/http"
	"runtime"
)

func main() {
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "PPanel Go instance is running. Edit /app/main.go and restart.\\nGo version: %s\\n", runtime.Version())
	})
	fmt.Println("PPanel instance starting on 0.0.0.0:8000 ...")
	http.ListenAndServe("0.0.0.0:8000", nil)
}
'''


def ensure_entry_file(host_dir: str, image: str) -> None:
    """创建实例时写入开箱即跑的示例入口（已存在则跳过）。"""
    name = entry_filename(image)
    if not os.path.exists(os.path.join(host_dir, name)):
        kind = runtime_kind(image)
        if kind == "php":
            content = _DEFAULT_INDEX_PHP
        elif kind == "node":
            content = _DEFAULT_INDEX_JS
        elif kind == "go":
            content = _DEFAULT_MAIN_GO
        else:
            content = _DEFAULT_MAIN_PY
        fs.write_text(host_dir, f"/{name}", content)


def _entry_cmd(inst: Instance) -> list[str]:
    """入口命令：有入口文件跑用户命令（PHP 兜底时注入禁用函数等安全参数）；没有则空闲模式常驻。"""
    if os.path.exists(os.path.join(inst.host_dir, entry_filename(inst.image))):
        return ["/bin/sh", "-c", inst.start_cmd or default_start_cmd(inst.image, inst.mem_limit)]
    return ["/bin/sh", "-c",
            f"echo '[空闲模式] 未检测到 {entry_filename(inst.image)}：环境已就绪，上传代码后点重启即可运行' && sleep infinity"]


def create_container(inst: Instance) -> str:
    """安全红线 2：CPU/内存/memswap/pids 全部硬限制，绝不 privileged。"""
    client = get_docker()
    ensure_network(client)

    def _do_create():
        return client.containers.create(
            image=inst.image,
            name=container_name(inst.id),
            command=_entry_cmd(inst),
            working_dir="/app",
            volumes={volume_src_for(inst): {"bind": "/app", "mode": "rw"}},
            ports={f"{settings.inner_app_port}/tcp": inst.ext_port},
            nano_cpus=int(inst.cpu_limit * 1e9),
            mem_limit=inst.mem_limit * 1024 * 1024,
            memswap_limit=inst.mem_limit * 1024 * 1024,  # swap=mem，杜绝内存超额
            pids_limit=settings.pids_limit,
            restart_policy={"Name": "unless-stopped"},
            privileged=False,
            network=settings.docker_network,
            environment={"PYTHONUNBUFFERED": "1"},  # 用户代码 print 实时进容器日志
            detach=True,
            stdin_open=False,
            tty=False,
        )

    try:
        return _do_create().id
    except docker.errors.APIError as e:
        msg = str(e)
        if "already in use" in msg or "Conflict" in msg:
            # 容器名冲突 = 被控登记丢失后的孤儿残留（面板重装 / 记录被清）。
            # 数据目录在宿主机按 id 保留，移除孤儿容器重建后用户文件不丢。
            try:
                client.containers.get(container_name(inst.id)).remove(force=True)
                return _do_create().id
            except docker.errors.APIError as e2:
                raise HTTPException(status_code=502, detail=f"创建容器失败：{e2}")
        raise HTTPException(status_code=502, detail=f"创建容器失败：{e}")


def recreate_container(inst: Instance) -> None:
    """启动命令变更后重建容器（保留目录与端口）。"""
    client = get_docker()
    try:
        c = client.containers.get(container_name(inst.id))
        if c.status == "running":
            raise HTTPException(status_code=409, detail="实例运行中，请先停止再修改启动命令")
        c.remove(force=True)
    except docker.errors.NotFound:
        pass
    inst.container_id = create_container(inst)


def get_container(inst: Instance) -> docker.models.containers.Container:
    client = get_docker()
    try:
        return client.containers.get(container_name(inst.id))
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="容器不存在，请尝试重新启动实例以自动重建")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")


def sync_status(inst: Instance) -> str | None:
    """用 Docker 真实状态校正 DB 里的 status（Docker 不可用时保持原值）。

    返回容器本次启动时间（running 时 ISO 字符串，Docker 重启自动更新；
    未运行/容器不存在返回 None）。调用方原本不使用返回值，兼容安全。"""
    client = try_get_docker()
    if client is None:
        return None
    try:
        c = client.containers.get(container_name(inst.id))
    except docker.errors.NotFound:
        return None
    except docker.errors.APIError:
        return None
    new_status = "running" if c.status == "running" else "exited"
    if inst.status != new_status:
        inst.status = new_status
    if inst.container_id != c.id:
        inst.container_id = c.id
    if c.status != "running":
        return None
    return c.attrs.get("State", {}).get("StartedAt") or None


def container_stats(inst: Instance) -> dict:
    """单次采样 CPU/内存占用（docker stats no-stream 语义）。"""
    empty = {"running": False, "cpu_percent": 0.0, "mem_usage_mb": 0.0,
             "mem_limit_mb": inst.mem_limit, "mem_percent": 0.0,
             "net_rx_mb": 0.0, "net_tx_mb": 0.0,
             "traffic_gb": getattr(inst, "traffic_gb", None),
             "traffic_used_mb": round(getattr(inst, "traffic_used_mb", 0.0) or 0.0, 1),
             "disk_used_mb": dir_size_mb(inst.host_dir)}
    client = try_get_docker()
    if client is None:
        return empty
    try:
        c = client.containers.get(container_name(inst.id))
    except docker.errors.NotFound:
        return empty
    except docker.errors.APIError:
        return empty
    if c.status != "running":
        return empty
    try:
        s = c.stats(stream=False)
    except docker.errors.APIError:
        return empty

    cpu = s.get("cpu_stats", {})
    pre = s.get("precpu_stats", {})
    cpu_delta = cpu.get("cpu_total_usage", 0) - pre.get("cpu_total_usage", 0)
    sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    online = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage", []) or []) or 1
    cpu_percent = (cpu_delta / sys_delta * online * 100) if sys_delta > 0 else 0.0

    mem = s.get("memory_stats", {})
    usage = max(mem.get("usage", 0) - mem.get("stats", {}).get("cache", 0), 0)
    limit = mem.get("limit") or inst.mem_limit * 1024 * 1024
    mem_mb = round(usage / 1024 / 1024, 1)
    limit_mb = round(limit / 1024 / 1024, 1)
    net = s.get("networks") or {}
    rx = sum((v.get("rx_bytes") or 0) for v in net.values())
    tx = sum((v.get("tx_bytes") or 0) for v in net.values())
    return {
        "running": True,
        "cpu_percent": round(cpu_percent, 1),
        "mem_usage_mb": mem_mb,
        "mem_limit_mb": limit_mb,
        "mem_percent": round(usage / limit * 100, 1) if limit else 0.0,
        "net_rx_mb": round(rx / 1024 / 1024, 2),
        "net_tx_mb": round(tx / 1024 / 1024, 2),
        # 流量限额与本期累计（MB，自然月重置；gb 空=不限）
        "traffic_gb": getattr(inst, "traffic_gb", None),
        "traffic_used_mb": round(getattr(inst, "traffic_used_mb", 0.0) or 0.0, 1),
        "disk_used_mb": dir_size_mb(inst.host_dir),
    }


def dir_size_mb(path: str) -> float | None:
    """宿主机目录大小（du -sm，5s 超时）；不可用时返回 None。"""
    if not path or not os.path.isdir(path):
        return None
    try:
        r = subprocess.run(["du", "-sm", path], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0:
            return float(r.stdout.split()[0])
    except Exception:  # noqa: BLE001
        pass
    return None


def container_logs(inst: Instance, tail: int = 200) -> str:
    c = get_container(inst)
    try:
        raw = c.logs(tail=max(1, min(tail, 5000)), stdout=True, stderr=True, timestamps=False)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"读取日志失败：{e}")
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return b"".join(raw).decode("utf-8", "replace")


def diagnose_startup(inst: Instance, wait: float = 2.0, interval: float = 0.5) -> None:
    """启动后短暂观察：容器若在观察期内未能保持 running，则直接把原因带回给用户，
    避免“点了启动却看不到任何反应”。缺文件、语法错误等会立刻暴露。"""
    import time as _t

    client = try_get_docker()
    if client is None:
        return
    t0 = int(_t.time())
    end = t0 + wait
    c = None
    while _t.time() < end:
        _t.sleep(interval)
        try:
            c = client.containers.get(container_name(inst.id))
        except docker.errors.NotFound:
            return
        except docker.errors.APIError:
            return
        if c.status == "running":
            return
    if c is not None and c.status in ("exited", "restarting"):
        try:
            # 只取本次启动之后的新日志，避免历史错误刷屏
            raw = c.logs(stdout=True, stderr=True, since=max(t0 - 2, 0), tail=15)
            text = (raw.decode("utf-8", "replace") if isinstance(raw, bytes)
                    else b"".join(raw).decode("utf-8", "replace"))
            tail = text.strip() or "(本次启动无新日志)"
        except Exception:  # noqa: BLE001
            tail = "(日志读取失败)"
        raise HTTPException(
            status_code=400,
            detail=f"容器未能保持运行（状态：{c.status}）。本次启动日志：\n{tail}",
        )


def _env_ok(c: docker.models.containers.Container) -> bool:
    return "PYTHONUNBUFFERED=1" in (c.attrs.get("Config", {}).get("Env") or [])


def _ensure_entry_cmd(client: docker.DockerClient, inst: Instance,
                      c: docker.models.containers.Container):
    """入口命令/环境随目录内容变化：非运行中时按需重建容器（目录/端口/配置保留）。"""
    stale = (c.attrs.get("Config", {}).get("Cmd") != _entry_cmd(inst) or not _env_ok(c))
    if stale and c.status != "running":
        c.remove(force=True)
        inst.container_id = create_container(inst)
        c = client.containers.get(container_name(inst.id))
    return c


def start_instance(inst: Instance) -> None:
    client = get_docker()
    name = container_name(inst.id)
    try:
        c = client.containers.get(name)
    except docker.errors.NotFound:
        # 容器被手动删除但目录还在：按当前配置自动重建
        inst.container_id = create_container(inst)
        c = client.containers.get(name)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
    c = _ensure_entry_cmd(client, inst, c)
    try:
        if c.status == "restarting":
            # 失败退避循环中：先停再起，打断 backoff 立即重跑（否则新上传的代码要等退避窗口）
            c.stop(timeout=5)
        c.start()
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"启动失败：{e}")
    inst.status = "running"


def stop_instance(inst: Instance) -> None:
    c = get_container(inst)
    try:
        c.stop(timeout=10)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"停止失败：{e}")
    inst.status = "exited"


def restart_instance(inst: Instance) -> None:
    """重启 = 停止后按当前目录内容重建入口（main.py 增删/代码更新都生效）再启动。"""
    client = get_docker()
    c = get_container(inst)
    try:
        c.stop(timeout=10)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"重启失败：{e}")
    c = _ensure_entry_cmd(client, inst, client.containers.get(container_name(inst.id)))
    try:
        c.start()
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"重启失败：{e}")
    inst.status = "running"


def runtime_versions(inst: Instance) -> dict:
    """运行环境卡：当前镜像 + 节点本地已拉取的同类型版本（未拉取的不展示，用户无感不可选）。
    同环境内切换：Python 实例只见 Python 版本，PHP/Node 实例同理（PHP 含 ppanel-php 增强版）。"""
    client = get_docker()
    local = {t for img in client.images.list() for t in (img.tags or [])}
    kind = runtime_kind(inst.image)
    versions = [v for v in allowed_images(client) if v in local and runtime_kind(v) == kind]
    if inst.image not in versions:
        versions.insert(0, inst.image)  # 当前版本始终展示（历史镜像可能不在白名单内）
    return {"image": inst.image, "versions": versions}


def switch_runtime(inst: Instance, img: str, db: Session) -> dict:
    """面板切换运行环境（异步任务）：过程逐行写入 ExecJob，经 /panel/ws/install 实时回显。
    仅限同类型环境内切换（Python/PHP/Node 各自的版本间）；本地无镜像直接拒绝，绝不自动拉取。"""
    img = (img or "").strip()
    if img not in allowed_images():
        raise HTTPException(status_code=400, detail="不支持的镜像版本")
    if runtime_kind(img) != runtime_kind(inst.image):
        raise HTTPException(status_code=400, detail="仅支持同类型运行环境内切换（Python / PHP / Node 各自的版本间）")
    if img == inst.image:
        return {"status": inst.status, "image": img, "detail": "当前已是该版本"}

    client = get_docker()
    try:
        client.images.get(img)
    except docker.errors.ImageNotFound:
        raise HTTPException(status_code=404, detail="此版本不支持，请联系管理员。")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")

    # 清理同实例已完成的历史任务，保持注册表精简
    for jid in [j for j, job in EXEC_JOBS.items() if job.instance_id == inst.id and job.done]:
        EXEC_JOBS.pop(jid, None)

    job = ExecJob(inst.id, f"runtime-switch → {img}", cmd=None)  # cmd 占位，worker 自行执行
    EXEC_JOBS[job.id] = job
    threading.Thread(target=_runtime_worker, args=(job, inst.id, img), daemon=True).start()
    return {"status": inst.status, "image": inst.image, "job_id": job.id}


def _runtime_worker(job: "ExecJob", instance_id: int, img: str) -> None:
    """后台切换序列：停→删→建→启（目录/端口/资源配额不变），每步 emit 到日志流。"""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        inst = db.get(Instance, instance_id)
        if inst is None:
            job.emit("[切换] 实例不存在")
            job.exit_code = -1
            return
        job.emit(f"[1/4] 目标运行环境：{img}")
        was_running = inst.status == "running"
        if was_running:
            job.emit("[2/4] 停止当前容器…")
            stop_instance(inst)
        else:
            job.emit("[2/4] 实例未在运行，跳过停止")
        old = inst.image
        inst.image = img
        # 旧 start_cmd 只是创建时的默认命令（非用户自定义）时，跟随运行时切换，
        # 避免 "python main.py" 跑进 PHP 容器（或反向）导致崩溃循环
        if inst.start_cmd and inst.start_cmd.strip() == default_start_cmd(old, inst.mem_limit):
            inst.start_cmd = default_start_cmd(img, inst.mem_limit)
            job.emit(f"[提示] 启动命令已随运行环境切换为：{inst.start_cmd}")
        job.emit("[3/4] 移除旧容器并按新镜像重建（数据目录与访问端口不变）…")
        try:
            recreate_container(inst)
        except HTTPException as e:
            inst.image = old  # 重建失败尽力恢复旧环境
            try:
                recreate_container(inst)
            except HTTPException:
                pass
            db.commit()
            job.emit(f"[切换] 失败：{e.detail}")
            job.exit_code = -1
            return
        db.commit()
        if was_running:
            job.emit("[4/4] 启动实例…")
            start_instance(inst)
            db.commit()
            try:
                diagnose_startup(inst)
                job.emit(f"[切换] 完成！服务已恢复运行（{img}）")
            except HTTPException as e:
                job.emit(f"[切换] 容器已切换，但应用启动异常：{e.detail}")
                job.emit("请到「日志」页查看详情")
                job.exit_code = 1
                return
        else:
            job.emit(f"[切换] 完成！下次启动将使用 {img}")
        job.exit_code = 0
        log_op(db, inst.id, "runtime", f"Python 环境切换为 {img}")
        db.commit()
        job.emit("[提示] 容器内依赖已重置，请到「依赖」页重装 requirements.txt")
    except Exception as e:  # noqa: BLE001
        job.emit(f"[切换] 异常：{e}")
        job.exit_code = -1
    finally:
        job.done = True
        db.close()


def remove_container(inst: Instance) -> None:
    """删除容器。失败必须抛错阻止上层删登记，否则 docker 故障期间删实例会留下孤儿容器。"""
    client = try_get_docker()
    if client is None:
        raise HTTPException(status_code=502, detail="Docker 不可用，为避免产生孤儿容器已中止删除，请稍后重试")
    name = container_name(inst.id)
    try:
        c = client.containers.get(name)
        try:
            c.stop(timeout=5)
        except docker.errors.APIError:
            pass  # 停不掉也要尝试删除
        client.containers.get(name).remove(force=True)
    except docker.errors.NotFound:
        return  # 容器已不存在（管理员手动删过）→ 视为回收成功
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"容器删除失败（登记未删除，可重试）：{e}")


def reconcile_orphans() -> int:
    """对账兜底：清理 docker 中已无登记的实例容器（孤儿，如重装面板/登记丢失/异常残留）。
    只匹配 {前缀}{纯数字} 命名，服务容器（mysql/caddy/恢复镜像等）不受影响；数据目录保留。"""
    client = try_get_docker()
    if client is None:
        return 0
    import re
    from app.database import SessionLocal

    prefix = container_prefix()
    pat = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    db = SessionLocal()
    try:
        known = {f"{prefix}{i}" for (i,) in db.query(Instance.id).all()}
    finally:
        db.close()
    removed = 0
    try:
        for c in client.containers.list(all=True, ignore_removed=True):
            if not pat.match(c.name or "") or c.name in known:
                continue
            try:
                c.remove(force=True)
                removed += 1
                print(f"[reconcile] 已清理孤儿容器 {c.name}（无登记记录，数据目录保留）")
            except docker.errors.APIError:
                pass
    except docker.errors.APIError:
        pass
    return removed


# ---------- 依赖安装 ----------

class ExecJob:
    def __init__(self, instance_id: int, file: str, cmd: list | None = None, desc: str = ""):
        self.id = uuid.uuid4().hex
        self.instance_id = instance_id
        self.file = file
        self.desc = desc
        self.cmd = cmd or ["pip", "install", "-r", file]
        self.lines: deque[str] = deque(maxlen=8000)
        self.done = False
        self.exit_code: int | None = None

    def emit(self, text: str) -> None:
        self.lines.append(text)


def _pip_container(inst: Instance):
    """依赖操作前置：容器存在且运行中。"""
    client = get_docker()
    try:
        c = client.containers.get(container_name(inst.id))
    except docker.errors.NotFound:
        raise HTTPException(status_code=409, detail="容器未运行，请先启动实例")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
    if c.status != "running":
        raise HTTPException(status_code=409, detail="容器未运行，请先启动实例")
    return c


def start_install(inst: Instance, file: str) -> ExecJob:
    """容器必须处于运行状态才能安装依赖。"""
    c = _pip_container(inst)

    # 清理同实例已完成的历史任务，保持注册表精简
    for jid in [j for j, job in EXEC_JOBS.items() if job.instance_id == inst.id and job.done]:
        EXEC_JOBS.pop(jid, None)

    job = ExecJob(inst.id, file)
    EXEC_JOBS[job.id] = job
    threading.Thread(target=_exec_worker, args=(job,), daemon=True).start()
    return job


def start_pip(inst: Instance, args: list) -> ExecJob:
    """手动 pip 操作（安装指定包 / 卸载），实时输出走 ExecJob。"""
    _pip_container(inst)
    for jid in [j for j, job in EXEC_JOBS.items() if job.instance_id == inst.id and job.done]:
        EXEC_JOBS.pop(jid, None)
    job = ExecJob(inst.id, " ".join(args), cmd=["pip", *args])
    EXEC_JOBS[job.id] = job
    threading.Thread(target=_exec_worker, args=(job,), daemon=True).start()
    return job


def has_running_job(instance_id: int) -> bool:
    """同实例同时只允许一个执行任务（防 pip/pecl 并发互踩）。"""
    return any(j.instance_id == instance_id and not j.done for j in EXEC_JOBS.values())


def start_exec(inst: Instance, desc: str, cmd: list) -> ExecJob:
    """通用容器内命令任务（如 pecl 安装扩展），实时输出走 ExecJob。"""
    _pip_container(inst)
    for jid in [j for j, job in EXEC_JOBS.items() if job.instance_id == inst.id and job.done]:
        EXEC_JOBS.pop(jid, None)
    job = ExecJob(inst.id, desc, cmd=cmd, desc=desc)
    EXEC_JOBS[job.id] = job
    threading.Thread(target=_exec_worker, args=(job,), daemon=True).start()
    return job


def pip_list(inst: Instance) -> list:
    """已安装依赖列表（pip list --format=json）。"""
    c = _pip_container(inst)
    client = get_docker()
    exec_id = client.api.exec_create(
        c.id, ["pip", "list", "--format=json"], workdir="/app")["Id"]
    out = client.api.exec_start(exec_id)
    inspect = client.api.exec_inspect(exec_id)
    if inspect.get("ExitCode") != 0:
        raise HTTPException(status_code=500, detail="获取依赖列表失败")
    raw = out.decode("utf-8", "replace")
    # demux 未开时 stderr（如 pip [notice]）会混入 stdout，用 raw_decode 取第一个合法 JSON 值
    start = raw.find("[")
    data = None
    if start != -1:
        try:
            data, _ = json.JSONDecoder().raw_decode(raw[start:])
        except ValueError:
            data = None
    if not isinstance(data, list):
        raise HTTPException(status_code=500, detail="解析依赖列表失败")
    return [{"name": d.get("name", ""), "version": d.get("version", "")} for d in data]


def _exec_worker(job: "ExecJob") -> None:
    try:
        client = get_docker()
        c = client.containers.get(container_name(job.instance_id))
        if c.status != "running":
            job.emit("[面板] 容器已停止，安装中止")
            job.exit_code = -1
            return
        exec_id = client.api.exec_create(
            c.id, job.cmd, workdir="/app"
        )["Id"]
        job.emit(f"[面板] 执行：{job.desc or 'pip ' + ' '.join(job.cmd[1:])}")
        stream = client.api.exec_start(exec_id, stream=True, demux=True)
        for out, err in stream:
            if out:
                job.emit(out.decode("utf-8", "replace"))
            if err:
                job.emit(err.decode("utf-8", "replace"))
        inspect = client.api.exec_inspect(exec_id)
        job.exit_code = inspect.get("ExitCode")
        job.emit(f"[面板] 任务完成，退出码 {job.exit_code}"
                 + ("（成功）" if job.exit_code == 0 else "（失败）"))
    except HTTPException as e:
        job.emit(f"[面板] 执行出错：{e.detail}")
        job.exit_code = -1
    except Exception as e:  # noqa: BLE001
        job.emit(f"[面板] 执行出错：{e}")
        job.exit_code = -1
    finally:
        job.done = True


# ---------- 被控自持：操作记录 / 用量采样（供独立面板，脱离主控可用） ----------

def log_op(db: Session, instance_id: int, action: str, detail: str = "") -> None:
    db.add(AgentOpLog(instance_id=instance_id, action=action, detail=detail))


def _accrue_traffic(inst: Instance, s: dict) -> None:
    """按采样增量累计本期流量（MB）。

    - cur 为容器生命周期累计（rx+tx）；容器重启会清零，按重启后读数续计
    - 跨自然月：本期流量清零，当前读数作基线（不计入新月份，保守不虚增）
    """
    cur = (s.get("net_rx_mb", 0.0) or 0.0) + (s.get("net_tx_mb", 0.0) or 0.0)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    last = inst.traffic_last_mb or 0.0
    if inst.traffic_month != month:
        inst.traffic_used_mb = 0.0
        inst.traffic_month = month
    elif cur >= last:
        inst.traffic_used_mb = round((inst.traffic_used_mb or 0.0) + (cur - last), 2)
    else:  # 容器重启：计数器清零，重启后的读数即新增量
        inst.traffic_used_mb = round((inst.traffic_used_mb or 0.0) + cur, 2)
    inst.traffic_last_mb = round(cur, 2)


def sample_all_metrics(db: Session) -> None:
    """采样所有运行中实例的 CPU/内存/网络，存本地库；保留最近 24 小时。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for inst in db.query(Instance).filter(Instance.status == "running").all():
        try:
            s = container_stats(inst)
            db.add(AgentMetric(
                instance_id=inst.id,
                cpu_percent=s.get("cpu_percent", 0.0),
                mem_used_mb=s.get("mem_usage_mb", 0.0),
                mem_limit_mb=s.get("mem_limit_mb", inst.mem_limit),
                net_rx_mb=s.get("net_rx_mb", 0.0),
                net_tx_mb=s.get("net_tx_mb", 0.0),
            ))
            _accrue_traffic(inst, s)  # 同步累计本期流量（60s 粒度）
        except Exception:  # noqa: BLE001 单实例失败不影响其他
            pass
    db.commit()
    db.query(AgentMetric).filter(
        AgentMetric.ts < now - timedelta(hours=24)).delete()
    db.commit()
