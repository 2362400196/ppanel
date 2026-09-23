"""后端冒烟测试：不依赖 Docker，验证登录/权限/文件路径穿越防护。

运行：uv run python tests/smoke_test.py
"""
import io
import os
import shutil
import sys

# 必须在导入 app 前设置测试环境
TEST_DIR = os.path.join(os.path.dirname(__file__), ".tmp_test")
os.environ["DB_URL"] = f"sqlite:///{TEST_DIR}/test.db"
os.environ["DATA_ROOT"] = os.path.join(TEST_DIR, "inst")
os.environ["PANEL_API_KEY"] = "test-api-key-123"

shutil.rmtree(TEST_DIR, ignore_errors=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Instance  # noqa: E402

PASS = 0


def check(name: str, cond: bool, extra: str = ""):
    global PASS
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {extra}" if extra and not cond else ""))
    if not cond:
        sys.exit(1)
    PASS += 1


with TestClient(app) as client:
    # 1. 健康检查
    check("health", client.get("/api/health").json().get("ok") is True)

    # 2. admin 登录（种子账号）
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    check("admin login", r.status_code == 200 and "token" in r.json())
    admin_token = r.json()["token"]
    ah = {"Authorization": f"Bearer {admin_token}"}

    # 3. 错误密码
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    check("wrong password 401", r.status_code == 401)

    # 4. /api/me
    r = client.get("/api/me", headers=ah)
    check("admin me", r.json().get("role") == "admin")

    # 5. 未登录 401
    check("no token 401", client.get("/api/instances").status_code == 401)

    # 6. admin 创建 user
    r = client.post("/api/admin/users", headers=ah,
                    json={"username": "alice", "password": "alice123", "role": "user"})
    check("create user", r.status_code == 200 and r.json()["role"] == "user")
    r = client.post("/api/admin/users", headers=ah,
                    json={"username": "bob", "password": "bob12345", "role": "user"})
    check("create user2", r.status_code == 200)

    # 7. 普通用户访问管理端被拒
    r = client.post("/api/auth/login", json={"username": "alice", "password": "alice123"})
    alice_token = r.json()["token"]
    auth_h = {"Authorization": f"Bearer {alice_token}"}
    check("user admin api 403", client.get("/api/admin/users", headers=auth_h).status_code == 403)

    # 8. 直接插入两个实例（不依赖 Docker），验证归属与文件防护
    db = SessionLocal()
    inst_a = Instance(user_id=1, name="admin-inst", image="python:3.11-slim",
                      start_cmd="python main.py", ext_port=30001, status="exited",
                      host_dir=os.environ["DATA_ROOT"] + "/1")
    inst_b = Instance(user_id=db.query(__import__("app.models", fromlist=["User"]).User)
                      .filter_by(username="alice").first().id,
                      name="alice-inst", image="python:3.11-slim",
                      start_cmd="python main.py", ext_port=30002, status="exited",
                      host_dir=os.environ["DATA_ROOT"] + "/2")
    inst_c = Instance(user_id=999, name="other-inst", image="python:3.11-slim",
                      start_cmd="python main.py", ext_port=30003, status="exited",
                      host_dir=os.environ["DATA_ROOT"] + "/3")
    db.add_all([inst_a, inst_b, inst_c])
    db.commit()
    id_a, id_b, id_c = inst_a.id, inst_b.id, inst_c.id
    db.close()

    os.makedirs(os.path.join(os.environ["DATA_ROOT"], "2", "sub"), exist_ok=True)
    with open(os.path.join(os.environ["DATA_ROOT"], "2", "main.py"), "w", encoding="utf-8") as f:
        f.write("print('hello')")

    # 9. 实例列表
    r = client.get("/api/instances", headers=auth_h)
    check("user instance list", r.status_code == 200 and len(r.json()) == 1
          and r.json()[0]["id"] == id_b)
    r = client.get("/api/instances?all=1", headers=ah)
    check("admin all instances", r.status_code == 200 and len(r.json()) == 3)

    # 10. 归属校验：alice 访问 admin/他人实例 -> 404
    check("cross access 404", client.get(f"/api/instances/{id_a}", headers=auth_h).status_code == 404)
    check("cross access 404 b", client.get(f"/api/instances/{id_c}", headers=auth_h).status_code == 404)
    check("own access 200", client.get(f"/api/instances/{id_b}", headers=auth_h).status_code == 200)

    # 11. 文件列表
    r = client.get(f"/api/instances/{id_b}/files?path=/", headers=auth_h)
    names = {e["name"] for e in r.json()["entries"]}
    check("file list", r.status_code == 200 and "main.py" in names and "sub" in names)

    # 12. 读取文件
    r = client.get(f"/api/instances/{id_b}/files/content", params={"path": "/main.py"},
                   headers=auth_h)
    check("file read", r.status_code == 200 and "hello" in r.json()["content"])

    # 13. 保存文件
    r = client.post(f"/api/instances/{id_b}/files/save", headers=auth_h,
                    json={"path": "/main.py", "content": "print('edited')"})
    check("file save", r.status_code == 200)
    r = client.get(f"/api/instances/{id_b}/files/content", params={"path": "/main.py"},
                   headers=auth_h)
    check("file saved", "edited" in r.json()["content"])

    # 14. mkdir / rename / delete
    r = client.post(f"/api/instances/{id_b}/files/mkdir", headers=auth_h, json={"path": "/newdir"})
    check("mkdir", r.status_code == 200)
    r = client.post(f"/api/instances/{id_b}/files/rename", headers=auth_h,
                    json={"src": "/newdir", "dst": "/sub2"})
    check("rename", r.status_code == 200)
    r = client.request("DELETE", f"/api/instances/{id_b}/files", params={"path": "/sub2"},
                       headers=auth_h)
    check("delete", r.status_code == 200)

    # 15. 上传
    r = client.put(f"/api/instances/{id_b}/files/upload", params={"path": "/"},
                   headers=auth_h,
                   files={"file": ("hello.txt", io.BytesIO("upload-content".encode()), "text/plain")})
    check("upload", r.status_code == 200 and r.json()["name"] == "hello.txt")
    r = client.get(f"/api/instances/{id_b}/files/content", params={"path": "/hello.txt"},
                   headers=auth_h)
    check("upload content", "upload-content" in r.json()["content"])

    # 16. 路径穿越防护（生死线）
    for evil in ["/../../etc", "/../../../etc/passwd", "/..%2f..%2fetc",
                 "/sub/../../../../etc", "/./../", "\\..\\..\\windows"]:
        r = client.get(f"/api/instances/{id_b}/files/content",
                       params={"path": evil}, headers=auth_h)
        check(f"traversal 403: {evil}", r.status_code in (403, 400, 404) and r.status_code != 200)
    r = client.get(f"/api/instances/{id_b}/files", params={"path": "/../../etc"}, headers=auth_h)
    check("traversal list 403", r.status_code == 403)
    r = client.put(f"/api/instances/{id_b}/files/upload", params={"path": "/../../etc"},
                   headers=auth_h, files={"file": ("x.txt", io.BytesIO(b"x"), "text/plain")})
    check("traversal upload 403", r.status_code == 403)
    r = client.post(f"/api/instances/{id_b}/files/save", headers=auth_h,
                    json={"path": "/../../evil.py", "content": "x"})
    check("traversal save 403", r.status_code == 403)
    r = client.delete(f"/api/instances/{id_b}/files", params={"path": "/.."},
                      headers=auth_h)
    check("traversal delete root 400/403", r.status_code in (400, 403))

    # 17. X-API-Key 预留通道（无归属 admin：默认列表为空，all=1 看全部）
    r = client.get("/api/instances", headers={"X-API-Key": "test-api-key-123"})
    check("api key admin empty", r.status_code == 200 and len(r.json()) == 0,
          f"status={r.status_code} body={r.text[:200]}")
    r = client.get("/api/instances", params={"all": 1}, headers={"X-API-Key": "test-api-key-123"})
    check("api key admin all", r.status_code == 200 and len(r.json()) >= 2,
          f"status={r.status_code} body={r.text[:200]}")
    r = client.get("/api/instances", headers={"X-API-Key": "bad-key"})
    check("api key bad 401", r.status_code == 401)

    # 18. 实例删除（无容器也应成功）
    r = client.delete(f"/api/instances/{id_c}", headers=ah)
    check("delete instance", r.status_code == 200)

print(f"\n全部 {PASS} 项通过")
