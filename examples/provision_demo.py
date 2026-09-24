"""开通实例演示脚本：第三方商城服务端调用 PPanel 被控 /open/* 的完整示例。

用法：
    uv run python examples/provision_demo.py              # 开通一个演示实例
    uv run python examples/provision_demo.py cleanup 10   # 回收演示实例（iid 换成实际值）
"""
import os
import sys

import httpx

# 生产环境请改为：BASE 指向你的服务器，KEY 从环境变量读取（切勿硬编码提交仓库）
BASE = os.environ.get("PPANEL_BASE", "http://192.168.31.149:9100")
KEY = os.environ.get("PPANEL_KEY", "zhuxiaohuan")
HEADERS = {"X-API-Key": KEY}


def provision() -> None:
    """开通一个 30 天期实例，并打印买家使用的一键登录链接。"""
    with httpx.Client(timeout=120) as c:
        # ---- 第 1 步：开通（真实场景在收到买家付款成功回调之后调用）----
        resp = c.post(f"{BASE}/open/provision", headers=HEADERS, json={
            "name": "demo-buyer-1001",      # 实例名称
            "image": "python:3.9-slim",     # 运行环境（节点 PYTHON_IMAGES 白名单内，建议节点已缓存该镜像）
            "days": 30,                     # 有效期 30 天
            "cpu": 1.0,                     # CPU 上限 1 核
            "mem": 512,                     # 内存上限 512MB
            "owner_ref": "ORDER-1001",      # 你的订单号（对账、Webhook 里都会带）
        })
        if resp.status_code >= 400:
            raise SystemExit(f"[开通失败] HTTP {resp.status_code}: {resp.text}")

        inst = resp.json()
        iid = inst["iid"]
        print(f"[开通成功] iid={iid}")
        print(f"  面板地址   : {inst['panel_url']}")
        print(f"  面板令牌   : {inst['panel_token']}")
        print(f"  外部端口   : {inst['ext_port']}")
        print(f"  到期时间   : {inst['expire_at']}")
        print(f"  买家登录链接: {inst['panel_url']}?t={inst['panel_token']}")
        # ^ 把这个链接发给买家即可，打开即自动进入他的独立面板

        # ---- 第 2 步：查询实例状态与用量（可轮询或按需调用）----
        resp = c.get(f"{BASE}/open/instances/{iid}", headers=HEADERS)
        data = resp.json()
        print(f"[查询] status={data['status']}  mem={data['stats']['mem_usage_mb']}MB")

        # ---- 第 3 步：买家续费 → 续期 7 天（从 max(到期, now) 起顺延）----
        resp = c.post(f"{BASE}/open/instances/{iid}/renew", headers=HEADERS,
                      json={"days": 7})
        resp.raise_for_status()
        print(f"[续期] 新到期时间: {resp.json()['expire_at']}")

        print("\n演示实例已保留，可用以下命令回收：")
        print(f"  uv run python examples/provision_demo.py cleanup {iid}")


def cleanup(iid: int) -> None:
    """回收实例：purge=1 连实例目录一起删除（数据不可恢复）。"""
    with httpx.Client(timeout=60) as c:
        resp = c.delete(f"{BASE}/open/instances/{iid}",
                        headers=HEADERS, params={"purge": 1})
        resp.raise_for_status()
        print(f"[回收完成] {resp.json()}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "cleanup":
        cleanup(int(sys.argv[2]))
    else:
        provision()
