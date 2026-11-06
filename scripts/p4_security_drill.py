#!/usr/bin/env python3
"""P4-M8 越权渗透演练：用两个隔离测试项目，走完整 HTTP 链路打越权。

做法说明（重要）：macOS 防火墙会静默丢弃外部入站，从局域网 IP 连本机
服务会直接断连，因此这里**不假装能用真实网卡地址**。取而代之的是起两个
真实 HTTP 服务：

* 回环服务（普通 Handler）——验证"本机免令牌"没有被破坏；
* 远端服务（``RemoteSimHandler``）——在 ``setup()`` 里把
  ``client_address`` 改成 203.0.113.7（TEST-NET-3，非回环），
  其余全部走真实代码路径：令牌解析、主体映射、Policy、路由。

这样验证的正是生产里会跑的那条链路，唯一差别是 TCP 来源地址。

用法::

    .venv/bin/python scripts/p4_security_drill.py [--port 0] [--json 输出路径]

结论以 PASS/FAIL 逐条打印；任一 FAIL 退出码非 0。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
import subprocess
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

#: 本机可能挂着 HTTP 代理，直连内网必须绕开，否则请求会被代理返回 502。
_OPENER = build_opener(ProxyHandler({}))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

TOKEN = "drill-token-0123456789abcdef"


REMOTE_ADDR = "203.0.113.7"  # TEST-NET-3，非回环


def make_remote_handler(dash_module):
    """把来源伪装成非回环地址的 Handler，其余行为与线上一致。"""

    class RemoteSimHandler(dash_module.Handler):
        def setup(self) -> None:
            super().setup()
            self.client_address = (REMOTE_ADDR, 54321)

    return RemoteSimHandler


def lan_ip() -> str:
    """取本机局域网 IPv4；取不到就退回 127.0.0.1（那样会跳过非回环用例）。

    优先用 ``ipconfig getifaddr``，因为它给的是真实网卡地址；UDP 探测在挂了
    代理/隧道时会拿到 198.18.x.x 这类虚拟地址，连接会失败。
    """
    for iface in ("en0", "en1", "en2"):
        try:
            out = subprocess.run(
                ["ipconfig", "getifaddr", iface],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            if out and not out.startswith("198.18."):
                return out
        except (OSError, subprocess.SubprocessError):
            continue
    return "127.0.0.1"


def bootstrap(tmp: Path) -> None:
    os.environ["DDP_AGENT_PLATFORM_DB"] = str(tmp / "platform.db")
    os.environ["DDP_AGENT_PLATFORM_REPO"] = str(tmp / "repo")
    os.environ["DDP_AGENT_PLATFORM_PROJECTS_ROOT"] = str(tmp / "projects")
    token_file = tmp / "token"
    token_file.write_text(TOKEN, encoding="utf-8")
    os.environ["DDP_AGENT_PLATFORM_TOKEN_FILE"] = str(token_file)

    config = tmp / "projects.json"
    config.write_text(json.dumps({"projects": []}), encoding="utf-8")
    os.environ["DDP_AGENT_PLATFORM_CONFIG"] = str(config)

    (tmp / "repo" / "ai-team").mkdir(parents=True, exist_ok=True)
    (tmp / "repo" / "ai-team" / "TASK_BOARD.md").write_text("# Board\n", encoding="utf-8")
    (tmp / "projects").mkdir(exist_ok=True)


def request(base: str, path: str, method: str = "GET",
            body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(base + path, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:200]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="p4-drill-"))
    bootstrap(tmp)

    import agents_dashboard as dash
    from agent_platform import service, store
    from agent_platform.p4 import killswitch

    monkeypatch_repo = tmp / "repo"
    dash.REPO = monkeypatch_repo
    dash.PROJECTS_ROOT = tmp / "projects"
    dash.STATUS_DIR = monkeypatch_repo / "ai-team" / "status"
    dash.SLUG_PREFIXES = ()

    service.bootstrap(repo_root=monkeypatch_repo)
    conn = store.connect()
    for pid in ("sec-a", "sec-b"):
        conn.execute(
            "INSERT OR IGNORE INTO agent_projects (id, code, name, repo_root,"
            " created_at, updated_at) VALUES (?, ?, ?, ?,"
            " '2026-11-06T00:00:00+00:00', '2026-11-06T00:00:00+00:00')",
            (pid, pid, pid.upper(), str(monkeypatch_repo)),
        )
    conn.commit()
    conn.close()

    loop_server = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
    loop_port = loop_server.server_address[1]
    threading.Thread(target=loop_server.serve_forever, daemon=True).start()

    remote_server = dash.ThreadingHTTPServer(
        ("127.0.0.1", 0), make_remote_handler(dash)
    )
    remote_port = remote_server.server_address[1]
    threading.Thread(target=remote_server.serve_forever, daemon=True).start()

    base = f"http://127.0.0.1:{remote_port}"     # 远端视角
    loop_base = f"http://127.0.0.1:{loop_port}"  # 本机视角
    host = REMOTE_ADDR
    remote_like = True
    time.sleep(0.3)

    results: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append({"case": name, "pass": ok, "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} —— {detail}")

    # 1. 无令牌
    code, data = request(base, "/api/projects")
    check("无令牌访问 API", code == 401 and remote_like or code == 401,
          f"HTTP {code} {data.get('error')}（来源 {host}）")

    # 2. 错误令牌
    code, data = request(base, "/api/projects", token="wrong-token")
    check("错误令牌", code == 401, f"HTTP {code} {data.get('error')}")

    # 3. 正确令牌但无项目成员
    code, data = request(base, "/api/projects/sec-a/tasks", token=TOKEN)
    check("远端主体无成员关系", code == 403,
          f"HTTP {code} {data.get('detail', {}).get('code')}")

    # 4. 给远端主体授予 sec-a 的 viewer，再访问 sec-b
    conn = store.connect()
    conn.execute(
        "INSERT OR IGNORE INTO project_members (id, project_id, principal_id, role, created_at)"
        " VALUES ('m-sec-a', 'sec-a', 'remote-token', 'viewer', '2026-11-06T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    code_a, _ = request(base, "/api/projects/sec-a/tasks", token=TOKEN)
    code_b, data_b = request(base, "/api/projects/sec-b/tasks", token=TOKEN)
    check("授权项目可读", code_a == 200, f"HTTP {code_a}")
    check("未授权项目被拒", code_b == 403,
          f"HTTP {code_b} {data_b.get('detail', {}).get('code')}")

    # 5. viewer 不能写
    code, data = request(base, "/api/projects/sec-a/tasks", "POST",
                         {"title": "越权写入", "task_key": "hack-1"}, token=TOKEN)
    check("viewer 不能写入", code == 403,
          f"HTTP {code} {data.get('detail', {}).get('code')}")

    # 6. 跨项目知识检索
    code, data = request(base, "/api/projects/sec-b/knowledge/search?q=secret", token=TOKEN)
    check("跨项目知识检索被拒", code == 403,
          f"HTTP {code} {data.get('detail', {}).get('code')}")

    # 7. 挂到 sec-a 但用 sec-b 的 project_id 提交越权
    code, data = request(base, "/api/projects/sec-b/knowledge/sources", "POST",
                         {"title": "越权来源"}, token=TOKEN)
    check("越权创建资源被拒", code == 403,
          f"HTTP {code} {data.get('detail', {}).get('code')}")

    # 8. 平台所有者（回环）不受影响
    code, _ = request(loop_base, "/api/projects/sec-a/tasks")
    check("回环仍免令牌可用", code == 200, f"HTTP {code}")

    # 9. Kill Switch 兜底
    conn = store.connect()
    sw = killswitch.trigger(conn, scope="global", reason="演练", triggered_by="drill")
    conn.close()
    time.sleep(2.2)  # 等进程内缓存过期
    code, data = request(base, "/api/projects/sec-a/tasks", token=TOKEN)
    check("Kill Switch 全局拦截", code == 503 and data.get("error") == "kill_switch",
          f"HTTP {code} {data.get('error')}")
    conn = store.connect()
    killswitch.release(conn, sw["id"], released_by="drill")
    conn.close()
    time.sleep(2.2)
    code, _ = request(base, "/api/projects/sec-a/tasks", token=TOKEN)
    check("解除后恢复", code == 200, f"HTTP {code}")

    for server in (loop_server, remote_server):
        server.shutdown()
        server.server_close()

    failed = [r for r in results if not r["pass"]]
    print(f"\n合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    print(f"演练数据目录：{tmp}（临时库，用完即弃）")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "host": host, "remote_like_loopback": remote_like,
            "cases": results, "failed": len(failed),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
