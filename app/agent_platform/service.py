"""项目作用域服务层：CRUD、模板初始化、聚合统计、隔离校验、回填编排。

不依赖 HTTP；``scripts/agents_dashboard.py`` 只做展示/兼容层。
"""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import sources, store, templates

DEFAULT_PROJECT_ID = "ddp"
DEFAULT_PROJECT_CODE = "ddp"
DEFAULT_PROJECT_NAME = "DDP 具身智能数据闭环平台"
DEFAULT_PROJECT_DESCRIPTION = "机器人数据采集、标注与模型闭环平台"
PROJECT_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,23}$")
PROJECT_STATUSES = frozenset({"active", "paused", "risk", "archived"})
ACTIVE_AGENT_STATUSES = ("working", "running")
IDLE_AGENT_STATUSES = ("idle", "available")
PAGE_SIZE_MAX = 100


class PlatformError(Exception):
    """稳定错误码；HTTP 层映射为 ``{ok:false,error:code}`` + status。"""

    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


# ---------------------------------------------------------------------------
# 默认值 / 行转换
# ---------------------------------------------------------------------------
def repo_root_default() -> Path:
    """默认仓库根：环境变量 → config/projects.json 默认项目 → cwd。

    平台已脱离任何单一仓库（2026-11-04）：不再用 ``Path(__file__).parents[2]``。
    """
    override = os.environ.get("DDP_AGENT_PLATFORM_REPO")
    if override:
        return Path(override).expanduser().resolve()
    from . import config

    return config.default_repo_root()


#: 2026-11-02 目录迁移前的历史 slug，只与本机历史会话兼容，不属于任何具体项目
HISTORICAL_SLUGS = ("-Users-hualei-deepseek",)


def default_session_slugs(repo_root: Path) -> list[str]:
    """仓库 slug（``/a/b`` → ``-a-b``）+ 该项目在 config 里登记的历史 slug。

    只取**本仓库**对应的项目条目，避免把别的项目 slug 算进本项目成本。
    """
    resolved = repo_root.resolve()
    current = resolved.as_posix().replace("/", "-")
    from . import config

    try:
        items = config.load_projects_config().get("projects") or []
    except Exception:  # noqa: BLE001 - 配置异常退化为只有当前 slug
        items = []
    matched: list[str] = []
    for item in items:
        raw = item.get("repo_root")
        if not raw:
            continue
        try:
            same = Path(raw).expanduser().resolve() == resolved
        except OSError:
            same = False
        if same:
            matched.extend(str(slug) for slug in (item.get("session_slugs") or []))
    return list(dict.fromkeys([current, *matched, *HISTORICAL_SLUGS]))


def workspace_root_for(code: str) -> Path:
    """新项目的独立空工作区；与平台库同目录，测试覆盖 DB 时自动隔离。"""
    return store.default_db_path().parent / "projects" / code


def _project_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["session_slugs"] = store.loads_json(data.get("session_slugs"), [])
    settings = store.loads_json(data.get("settings_json"), {})
    data["settings_json"] = settings
    data["default_branch"] = settings.get("default_branch")
    data.setdefault("owner", None)
    data.setdefault("archived_at", None)
    data.setdefault("version", 1)
    return data


def _is_favorite(conn: sqlite3.Connection, project_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM project_favorites WHERE user_key = 'local' AND project_id = ?",
        (project_id,),
    ).fetchone()
    return row is not None


def _project_row(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM agent_projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise PlatformError("project_not_found", 404)
    return _project_dict(row)


def _require_project(
    conn: sqlite3.Connection, project_id: str, *, write: bool = False
) -> dict[str, Any]:
    project = _project_row(conn, project_id)
    if write:
        _require_writable(project)
    return project


def _agent_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["metadata_json"] = store.loads_json(data.get("metadata_json"), {})
    return data


def _token_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")  # noqa: UP017


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------
def ensure_project_agents(
    conn: sqlite3.Connection, project_id: str, template_key: str
) -> int:
    """按模板补齐缺失角色；已有记录（含 worktree/status）只补不覆盖。"""
    template = templates.load_template(conn, template_key)
    if template is None:
        raise PlatformError("invalid_team_template", 422)
    roles = template.get("roles") or []
    existing = {
        row["instance_key"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM project_agents WHERE project_id = ?", (project_id,)
        ).fetchall()
    }
    created = 0
    for role in roles:
        instance_key = f"{role['role_key']}-1"
        if instance_key in existing:
            continue
        agent_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO project_agents
                (id, project_id, role_key, display_name, instance_key, agent_type,
                 execution_mode, status, current_task_id, parent_agent_id,
                 current_branch, worktree_path, last_heartbeat_at, metadata_json,
                 role_snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'offline', NULL, NULL, NULL, NULL, NULL, '{}', ?)
            """,
            (
                agent_id,
                project_id,
                role["role_key"],
                role["display_name"],
                instance_key,
                role["agent_type"],
                role["execution_mode"],
                store.dumps_json(role),
            ),
        )
        existing[instance_key] = {"id": agent_id, "role_key": role["role_key"]}
        created += 1

    # 旧库升级：已有实例若快照为空，用当前模板补一次快照（不覆盖用户改动）。
    role_by_key = {role["role_key"]: role for role in roles}
    for row in conn.execute(
        "SELECT id, role_key, role_snapshot_json FROM project_agents WHERE project_id = ?",
        (project_id,),
    ).fetchall():
        if store.loads_json(row["role_snapshot_json"], {}):
            continue
        snapshot = role_by_key.get(row["role_key"])
        if snapshot:
            conn.execute(
                "UPDATE project_agents SET role_snapshot_json = ? WHERE id = ?",
                (store.dumps_json(snapshot), row["id"]),
            )

    by_role = {row.get("role_key"): row.get("id") for row in existing.values()}
    for role in roles:
        instance_key = f"{role['role_key']}-1"
        parent_role = role.get("parent_role_key")
        parent_id = by_role.get(parent_role) if parent_role else None
        conn.execute(
            "UPDATE project_agents SET parent_agent_id = ? WHERE project_id = ? AND instance_key = ?",
            (parent_id, project_id, instance_key),
        )
    return created


def ensure_default_project(
    conn: sqlite3.Connection, repo_root: Path | None = None
) -> dict[str, Any]:
    """确保默认 DDP 项目存在；不覆盖已有项目/角色数据。"""
    repo = (repo_root or repo_root_default()).resolve()
    templates.ensure_templates(conn)
    row = conn.execute("SELECT * FROM agent_projects WHERE id = ?", (DEFAULT_PROJECT_ID,)).fetchone()
    if row is None:
        now = store.now_iso()
        conn.execute(
            """
            INSERT INTO agent_projects
                (id, code, name, description, status, repo_root, session_slugs,
                 team_template_key, team_template_version, settings_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?, 1, '{}', ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                DEFAULT_PROJECT_ID,
                DEFAULT_PROJECT_CODE,
                DEFAULT_PROJECT_NAME,
                DEFAULT_PROJECT_DESCRIPTION,
                str(repo),
                store.dumps_json(default_session_slugs(repo)),
                templates.STANDARD_1_6,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM agent_projects WHERE id = ?", (DEFAULT_PROJECT_ID,)).fetchone()
    project = _project_dict(row)
    ensure_project_agents(conn, project["id"], project["team_template_key"])
    return get_project_with(conn, DEFAULT_PROJECT_ID)


def ensure_configured_projects(conn: sqlite3.Connection) -> list[str]:
    """按 ``config/projects.json`` 登记尚未入库的项目。

    幂等：已存在的项目**一律不动**（名称、模板、角色以库为准，配置只负责首次登记）。
    仓库路径不存在的条目跳过，避免把配置里的笔误带进库。
    """
    from . import config

    templates.ensure_templates(conn)
    created: list[str] = []
    for item in config.load_projects_config().get("projects", []):
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or item.get("id") or "").strip()
        raw_repo = str(item.get("repo_root") or "").strip()
        if not code or not raw_repo:
            continue
        if not PROJECT_CODE_RE.match(code):
            continue
        repo = Path(raw_repo).expanduser()
        if not repo.is_dir():
            continue
        if conn.execute("SELECT 1 FROM agent_projects WHERE id = ?", (code,)).fetchone():
            continue
        slugs = [str(s) for s in (item.get("session_slugs") or []) if s]
        now = store.now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO agent_projects
                    (id, code, name, description, status, repo_root, session_slugs,
                     team_template_key, team_template_version, settings_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?, ?, 1, '{}', ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    code,
                    str(item.get("code") or code),
                    str(item.get("name") or code),
                    item.get("description"),
                    str(repo.resolve()),
                    store.dumps_json(slugs),
                    templates.STANDARD_1_6,
                    now,
                    now,
                ),
            )
        ensure_project_agents(conn, code, templates.STANDARD_1_6)
        created.append(code)
    return created


def bootstrap(conn: sqlite3.Connection | None = None, repo_root: Path | None = None) -> str:
    own = conn is None
    conn = conn or store.connect()
    try:
        store.init_db(conn)
        ensure_default_project(conn, repo_root)
        ensure_configured_projects(conn)
        return DEFAULT_PROJECT_ID
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 项目读取 / 写入
# ---------------------------------------------------------------------------
def get_project_with(conn: sqlite3.Connection, project_id: str, include_agents: bool = True) -> dict[str, Any]:
    project = _project_row(conn, project_id)
    project["favorite"] = _is_favorite(conn, project_id)
    if include_agents:
        agents = conn.execute(
            """
            SELECT * FROM project_agents WHERE project_id = ?
            ORDER BY current_branch IS NULL, display_name
            """,
            (project_id,),
        ).fetchall()
        project["agents"] = [_agent_dict(agent) for agent in agents]
    return project


def get_project(project_id: str) -> dict[str, Any]:
    with store.session() as conn:
        return get_project_with(conn, project_id)


def _require_writable(project: dict[str, Any]) -> None:
    if project.get("status") == "archived":
        raise PlatformError("project_archived", 403, "项目已归档，只读")


def audit(
    conn: sqlite3.Connection,
    project_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    *,
    result: str = "ok",
    detail: dict[str, Any] | None = None,
    actor: str = "local",
) -> None:
    """只追加审计记录；失败不阻断业务（写审计本身尽力而为）。"""
    conn.execute(
        """
        INSERT INTO audit_logs
            (id, project_id, actor, action, resource_type, resource_id,
             result, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            project_id,
            actor,
            action,
            resource_type,
            resource_id,
            result,
            store.dumps_json(detail or {}),
            store.now_iso(),
        ),
    )


def _audit(conn: sqlite3.Connection, project_id: str, event_type: str, message: str) -> None:
    occurred = store.now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO agent_events
            (id, project_id, agent_id, session_id, event_type, message, tool, ok, occurred_at)
        VALUES (?, ?, NULL, NULL, ?, ?, NULL, 1, ?)
        """,
        (store.stable_id("audit", project_id, event_type, occurred), project_id, event_type, message, occurred),
    )


def create_project(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlatformError("invalid_json", 400)
    code = payload.get("code")
    if not isinstance(code, str) or not PROJECT_CODE_RE.match(code):
        raise PlatformError("invalid_project_code", 422)
    name = str(payload.get("name") or "").strip()
    if not 2 <= len(name) <= 120:
        raise PlatformError("invalid_project_name", 422)
    description = str(payload.get("description") or "").strip() or None

    raw_repo = payload.get("repo_root")
    if raw_repo:
        repo = Path(str(raw_repo)).expanduser().resolve()
        if not repo.is_dir():
            raise PlatformError("invalid_project_path", 422)
    else:
        # 未显式提供仓库时，不继承 DDP 仓库/会话，改用独立空工作区。
        repo = workspace_root_for(code)
        try:
            repo.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise PlatformError("invalid_project_path", 422)

    slugs = payload.get("session_slugs")
    if slugs is None:
        session_slugs: list[str] = []
    elif isinstance(slugs, list) and all(isinstance(item, str) for item in slugs):
        session_slugs = [item for item in slugs if item.strip()]
    else:
        raise PlatformError("invalid_session_slugs", 422)

    template_key = str(payload.get("team_template_key") or templates.STANDARD_1_6)
    owner = str(payload.get("owner") or "").strip() or None
    conn = store.connect()
    try:
        store.init_db(conn)
        templates.ensure_templates(conn)
        if templates.load_template(conn, template_key) is None:
            raise PlatformError("invalid_team_template", 422)
        now = store.now_iso()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO agent_projects
                    (id, code, name, description, status, repo_root, session_slugs,
                     team_template_key, team_template_version, settings_json, created_at, updated_at,
                     owner, version)
                VALUES (?, ?, ?, ?, 'active', ?, ?, ?, 1, '{}', ?, ?, ?, 1)
                """,
                (
                    code,
                    code,
                    name,
                    description,
                    str(repo),
                    store.dumps_json(session_slugs),
                    template_key,
                    now,
                    now,
                    owner,
                ),
            )
            ensure_project_agents(conn, code, template_key)
            _audit(conn, code, "project_created", f"创建项目 {name}")
            audit(conn, code, "project.create", "project", code,
                  detail={"name": name, "template_key": template_key})
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise PlatformError("project_code_exists", 409)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return get_project_with(conn, code)
    finally:
        conn.close()


def _check_version(payload: dict[str, Any], current: int, *, required: bool = False) -> None:
    raw = payload.get("version")
    if raw is None:
        if required:
            raise PlatformError("invalid_payload", 422, "缺少 version")
        return
    try:
        version = int(raw)
    except (TypeError, ValueError):
        raise PlatformError("invalid_payload", 422, "version 必须是整数")
    if version != int(current or 1):
        raise PlatformError("version_conflict", 409, f"版本不一致：当前 {current}")


def patch_project(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlatformError("invalid_json", 400)
    conn = store.connect()
    try:
        store.init_db(conn)
        project = _require_project(conn, project_id)
        status = payload.get("status")
        if status is not None:
            if status not in PROJECT_STATUSES:
                raise PlatformError("invalid_status", 422)
            # 归档只读：只允许 archived -> active 的恢复操作。
            if project.get("status") == "archived" and status != "active":
                raise PlatformError("project_archived", 403)
        else:
            _require_writable(project)
        _check_version(payload, int(project.get("version") or 1))

        updates: list[str] = []
        params: list[Any] = []
        detail: dict[str, Any] = {}
        if "name" in payload:
            name = str(payload.get("name") or "").strip()
            if not 2 <= len(name) <= 120:
                raise PlatformError("invalid_project_name", 422)
            updates.append("name = ?")
            params.append(name)
            detail["name"] = name
        if "description" in payload:
            description = str(payload.get("description") or "").strip() or None
            updates.append("description = ?")
            params.append(description)
            detail["description"] = description
        if "owner" in payload:
            owner = str(payload.get("owner") or "").strip() or None
            updates.append("owner = ?")
            params.append(owner)
            detail["owner"] = owner
        if "repo_root" in payload:
            repo = Path(str(payload.get("repo_root") or "")).expanduser().resolve()
            if not repo.is_dir():
                raise PlatformError("invalid_project_path", 422)
            updates.append("repo_root = ?")
            params.append(str(repo))
            detail["repo_root"] = str(repo)
        if "default_branch" in payload:
            branch = str(payload.get("default_branch") or "").strip() or None
            settings = dict(project.get("settings_json") or {})
            settings["default_branch"] = branch
            updates.append("settings_json = ?")
            params.append(store.dumps_json(settings))
            detail["default_branch"] = branch
        if status is not None:
            updates.append("status = ?")
            params.append(status)
            detail["status"] = status
            if status == "archived":
                updates.append("archived_at = ?")
                archived_at = store.now_iso()
                params.append(archived_at)
                detail["archived_at"] = archived_at
            elif project.get("status") == "archived":
                updates.append("archived_at = NULL")
        if not updates:
            return get_project_with(conn, project_id)

        updates.append("updated_at = ?")
        params.append(store.now_iso())
        updates.append("version = version + 1")
        params.append(project_id)
        conn.execute(f"UPDATE agent_projects SET {', '.join(updates)} WHERE id = ?", tuple(params))
        if status is not None and status != project.get("status"):
            _audit(conn, project_id, f"project_{status}", f"项目状态 {project.get('status')} -> {status}")
            action = "project.restore" if status == "active" else f"project.{status}"
            audit(conn, project_id, action, "project", project_id,
                  detail={"from": project.get("status"), "to": status})
        else:
            audit(conn, project_id, "project.update", "project", project_id, detail=detail)
        return get_project_with(conn, project_id)
    finally:
        conn.close()


def archive_project(project_id: str) -> dict[str, Any]:
    """归档；幂等：已归档直接返回当前状态。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        project = _require_project(conn, project_id)
        if project.get("status") != "archived":
            now = store.now_iso()
            conn.execute(
                "UPDATE agent_projects SET status='archived', archived_at=?, updated_at=?, "
                "version=version+1 WHERE id=?",
                (now, now, project_id),
            )
            _audit(conn, project_id, "project_archived", "项目已归档")
            audit(conn, project_id, "project.archive", "project", project_id,
                  detail={"from": project.get("status")})
        return get_project_with(conn, project_id)
    finally:
        conn.close()


def restore_project(project_id: str) -> dict[str, Any]:
    """恢复；幂等：未归档直接返回当前状态。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        project = _require_project(conn, project_id)
        if project.get("status") == "archived":
            now = store.now_iso()
            conn.execute(
                "UPDATE agent_projects SET status='active', archived_at=NULL, updated_at=?, "
                "version=version+1 WHERE id=?",
                (now, project_id),
            )
            _audit(conn, project_id, "project_active", "项目已恢复")
            audit(conn, project_id, "project.restore", "project", project_id)
        return get_project_with(conn, project_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def _project_cost_24h(
    project_slugs: list[str],
    sessions: list[dict[str, Any]],
    cutoff: datetime,
    tokens_by_session: dict[str, int],
) -> float | None:
    """近 24h 有活动会话的成本，按窗口内 token 占会话总 token 的比例分摊。

    口径：ccusage 的 ``totalCost`` 是整段会话（可能跨天）的总成本；这里用本项目
    ``token_usage_events`` 在近 24h 的 token 占该会话 ``totalTokens`` 的比例分摊，
    仍是近似值，不是精确按日账单。无命中/无可分摊 token 返回 None。
    """
    slug_set = {slug for slug in project_slugs if slug}
    if not slug_set or not sessions:
        return None
    total: float | None = None
    for session in sessions:
        project_path = str(session.get("project_path") or "")
        candidates = {project_path}
        if project_path.startswith(("/", "\\")):
            candidates.add(project_path.replace("/", "-").replace("\\", "-").lstrip("-"))
            candidates.add(project_path.replace("/", "-").replace("\\", "-"))
        if not candidates & slug_set:
            continue
        raw_ts = session.get("last_activity")
        if not raw_ts:
            continue
        try:
            activity = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))  # noqa: FURB162
        except ValueError:
            continue
        if activity.tzinfo is None:
            activity = activity.replace(tzinfo=timezone.utc)  # noqa: UP017
        if activity < cutoff:
            continue
        cost = session.get("total_cost")
        try:
            cons = int(session.get("total_tokens") or 0)
        except (TypeError, ValueError):
            cons = 0
        window_tokens = int(tokens_by_session.get(str(session.get("session_id") or ""), 0))
        if cost is None or cons <= 0 or window_tokens <= 0:
            continue
        share = min(window_tokens / cons, 1.0)
        total = (total or 0.0) + float(cost) * share
    return round(total, 6) if total is not None else None


def project_stats(
    conn: sqlite3.Connection,
    project_id: str,
    sessions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cutoff_iso = _token_cutoff()
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=24)  # noqa: UP017
    placeholders = ",".join("?" for _ in ACTIVE_AGENT_STATUSES)
    project_row = conn.execute(
        "SELECT session_slugs FROM agent_projects WHERE id = ?", (project_id,)
    ).fetchone()
    slugs = store.loads_json(project_row["session_slugs"], []) if project_row else []
    if sessions is None:
        sessions = sources.ccusage_session_costs()
    tokens_by_session = {
        str(row["session_id"]): int(row["tokens"] or 0)
        for row in conn.execute(
            """
            SELECT session_id, SUM(total_tokens) AS tokens
            FROM token_usage_events
            WHERE project_id = ? AND occurred_at >= ? AND session_id IS NOT NULL
            GROUP BY session_id
            """,
            (project_id, cutoff_iso),
        ).fetchall()
    }
    return {
        "agent_total": _scalar(conn, "SELECT COUNT(*) FROM project_agents WHERE project_id = ?", (project_id,)),
        "agent_active": _scalar(
            conn,
            f"SELECT COUNT(*) FROM project_agents WHERE project_id = ? AND status IN ({placeholders})",
            (project_id, *ACTIVE_AGENT_STATUSES),
        ),
        "task_todo": _scalar(
            conn, "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND status = 'todo'", (project_id,)
        ),
        "task_in_progress": _scalar(
            conn, "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND status = 'in_progress'", (project_id,)
        ),
        "task_review": _scalar(
            conn, "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND status = 'review'", (project_id,)
        ),
        "task_done": _scalar(
            conn, "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND status = 'done'", (project_id,)
        ),
        "task_blocked": _scalar(
            conn, "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND status = 'blocked'", (project_id,)
        ),
        "token_24h": _scalar(
            conn,
            "SELECT COALESCE(SUM(total_tokens), 0) FROM token_usage_events "
            "WHERE project_id = ? AND occurred_at >= ?",
            (project_id, cutoff_iso),
        ),
        "cost_24h": _project_cost_24h(slugs, sessions, cutoff_dt, tokens_by_session),
    }


def project_usage(project_id: str) -> dict[str, Any]:
    """项目用量摘要：Token 来自 transcript 明细，成本来自 ccusage session。"""
    with store.session() as conn:
        get_project_with(conn, project_id, include_agents=False)
        stats = project_stats(conn, project_id)
        return {
            "token_24h": stats["token_24h"],
            "cost_24h": stats["cost_24h"],
            "source": "transcript/ccusage-session",
        }


def project_stats_for(project_id: str) -> dict[str, Any]:
    with store.session() as conn:
        get_project_with(conn, project_id, include_agents=False)
        return project_stats(conn, project_id)


def list_projects(
    conn: sqlite3.Connection | None = None,
    *,
    q: str | None = None,
    status: str | None = None,
    favorite: bool | str | None = None,
) -> dict[str, Any]:
    own = conn is None
    conn = conn or store.connect()
    try:
        store.init_db(conn)
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if q:
            where.append("(name LIKE ? OR code LIKE ? OR description LIKE ?)")
            like = f"%{q[:80]}%"
            params.extend([like, like, like])
        if favorite in (True, "1", "true", 1):
            where.append(
                "id IN (SELECT project_id FROM project_favorites WHERE user_key = 'local')"
            )
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            f"SELECT * FROM agent_projects{clause} ORDER BY updated_at DESC, code ASC",
            tuple(params),
        ).fetchall()
        sessions = sources.ccusage_session_costs()
        items = []
        for row in rows:
            project = _project_dict(row)
            project["favorite"] = _is_favorite(conn, project["id"])
            project["stats"] = project_stats(conn, project["id"], sessions)
            project.pop("settings_json", None)
            items.append(project)
        ids = [item["id"] for item in items]
        default_id = DEFAULT_PROJECT_ID if DEFAULT_PROJECT_ID in ids else (ids[0] if ids else None)
        return {"items": items, "total": len(items), "default_project_id": default_id}
    finally:
        if own:
            conn.close()


def overview(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    own = conn is None
    conn = conn or store.connect()
    try:
        store.init_db(conn)
        cutoff = _token_cutoff()
        placeholders = ",".join("?" for _ in ACTIVE_AGENT_STATUSES)
        idle_placeholders = ",".join("?" for _ in IDLE_AGENT_STATUSES)
        project_total = _scalar(conn, "SELECT COUNT(*) FROM agent_projects")
        project_active = _scalar(conn, "SELECT COUNT(*) FROM agent_projects WHERE status = 'active'")
        agent_total = _scalar(
            conn,
            "SELECT COUNT(*) FROM project_agents a JOIN agent_projects p ON p.id = a.project_id "
            "WHERE p.status != 'archived'",
        )
        agent_idle = _scalar(
            conn,
            "SELECT COUNT(*) FROM project_agents a JOIN agent_projects p ON p.id = a.project_id "
            f"WHERE p.status != 'archived' AND a.status IN ({idle_placeholders})",
            IDLE_AGENT_STATUSES,
        )
        agent_active = _scalar(
            conn,
            "SELECT COUNT(*) FROM project_agents a JOIN agent_projects p ON p.id = a.project_id "
            f"WHERE p.status != 'archived' AND a.status IN ({placeholders})",
            ACTIVE_AGENT_STATUSES,
        )
        task_in_progress = _scalar(
            conn,
            "SELECT COUNT(*) FROM tasks t JOIN agent_projects p ON p.id = t.project_id "
            "WHERE p.status != 'archived' AND t.status = 'in_progress'",
        )
        task_blocked = _scalar(
            conn,
            "SELECT COUNT(*) FROM tasks t JOIN agent_projects p ON p.id = t.project_id "
            "WHERE p.status != 'archived' AND t.status = 'blocked'",
        )
        token_24h = _scalar(
            conn,
            "SELECT COALESCE(SUM(u.total_tokens), 0) FROM token_usage_events u "
            "JOIN agent_projects p ON p.id = u.project_id "
            "WHERE p.status != 'archived' AND u.occurred_at >= ?",
            (cutoff,),
        )
        projects = []
        sessions = sources.ccusage_session_costs()
        for row in conn.execute(
            "SELECT * FROM agent_projects WHERE status != 'archived' ORDER BY updated_at DESC"
        ).fetchall():
            project = _project_dict(row)
            stats = project_stats(conn, project["id"], sessions)
            projects.append({
                "id": project["id"],
                "code": project["code"],
                "name": project["name"],
                "description": project.get("description"),
                "status": project["status"],
                "repo_root": project["repo_root"],
                "stats": {
                    key: stats[key]
                    for key in (
                        "agent_total", "agent_active", "task_in_progress",
                        "task_blocked", "token_24h", "cost_24h",
                    )
                },
                "updated_at": project["updated_at"],
            })
        project_costs = [
            item["stats"]["cost_24h"]
            for item in projects
            if item["stats"]["cost_24h"] is not None
        ]
        cost_24h = round(sum(project_costs), 6) if project_costs else None
        events = conn.execute(
            """
            SELECT e.id, e.project_id, p.code AS project_code, p.name AS project_name,
                   e.agent_id, e.event_type, e.message, e.occurred_at
            FROM agent_events e JOIN agent_projects p ON p.id = e.project_id
            ORDER BY e.occurred_at DESC LIMIT 20
            """
        ).fetchall()
        return {
            "project_total": project_total,
            "project_active": project_active,
            "agent_total": agent_total,
            "agent_idle": agent_idle,
            "agent_active": agent_active,
            "task_in_progress": task_in_progress,
            "task_blocked": task_blocked,
            "token_24h": token_24h,
            "cost_24h": cost_24h,
            "projects": projects,
            "recent_events": [dict(row) for row in events],
            "generated_at": store.now_iso(),
        }
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 只读列表（项目作用域）
# ---------------------------------------------------------------------------
def _pagination(page: int | str | None, page_size: int | str | None) -> tuple[int, int]:
    try:
        page_num = max(int(page or 1), 1)
    except (TypeError, ValueError):
        page_num = 1
    try:
        size = int(page_size or 20)
    except (TypeError, ValueError):
        size = 20
    return page_num, max(1, min(size, PAGE_SIZE_MAX))


def list_tasks(
    project_id: str,
    *,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    priority: str | None = None,
    assignee: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    page, size = _pagination(page, page_size)
    with store.session() as conn:
        _require_project(conn, project_id)
        where = ["project_id = ?"]
        params: list[Any] = [project_id]
        if status:
            where.append("status = ?")
            params.append(status)
        if priority:
            where.append("priority = ?")
            params.append(priority)
        if assignee:
            where.append("(assignee_agent_id = ? OR owner = ?)")
            params.extend([assignee, assignee])
        if q:
            where.append("(task_key LIKE ? OR title LIKE ? OR description LIKE ?)")
            like = f"%{q[:80]}%"
            params.extend([like, like, like])
        clause = " AND ".join(where)
        total = _scalar(conn, f"SELECT COUNT(*) FROM tasks WHERE {clause}", tuple(params))
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE {clause} ORDER BY updated_at DESC, task_key LIMIT ? OFFSET ?",
            (*params, size, (page - 1) * size),
        ).fetchall()
        items = [_task_dict(row) for row in rows]
        return {"items": items, "total": total, "page": page, "page_size": size}


def list_deliveries(
    project_id: str,
    *,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    kind: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    """列表合并只读镜像 + 平台内交付；镜像行标记 ``readonly: true``。"""
    page, size = _pagination(page, page_size)
    with store.session() as conn:
        _require_project(conn, project_id)
        where = ["d.project_id = ?"]
        params: list[Any] = [project_id]
        if status:
            where.append("d.status = ?")
            params.append(status)
        if kind:
            where.append("d.kind = ?")
            params.append(kind)
        if task:
            where.append("(d.task_id = ? OR t.task_key = ?)")
            params.extend([task, task])
        clause = " AND ".join(where)
        total = _scalar(
            conn,
            f"SELECT COUNT(*) FROM deliveries d "
            f"LEFT JOIN tasks t ON t.id = d.task_id AND t.project_id = d.project_id WHERE {clause}",
            tuple(params),
        )
        rows = conn.execute(
            f"""
            SELECT d.*, t.task_key
            FROM deliveries d LEFT JOIN tasks t ON t.id = d.task_id AND t.project_id = d.project_id
            WHERE {clause}
            ORDER BY d.created_at DESC LIMIT ? OFFSET ?
            """,
            (*params, size, (page - 1) * size),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["readonly"] = item.get("source") == "mirror"
            items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": size}


def list_events(project_id: str, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    page, size = _pagination(page, page_size)
    with store.session() as conn:
        get_project_with(conn, project_id, include_agents=False)
        total = _scalar(conn, "SELECT COUNT(*) FROM agent_events WHERE project_id = ?", (project_id,))
        rows = conn.execute(
            """
            SELECT e.*, a.display_name AS agent_name
            FROM agent_events e LEFT JOIN project_agents a
              ON a.id = e.agent_id AND a.project_id = e.project_id
            WHERE e.project_id = ?
            ORDER BY e.occurred_at DESC LIMIT ? OFFSET ?
            """,
            (project_id, size, (page - 1) * size),
        ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": size}


# ---------------------------------------------------------------------------
# 模板 / 回填
# ---------------------------------------------------------------------------
TEMPLATE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


def _template_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["roles"] = store.loads_json(item.pop("roles_json"), [])
    return item


def list_team_templates() -> dict[str, Any]:
    """每个 template_key 只返回最新版本（docs/113 §3）。"""
    with store.session() as conn:
        templates.ensure_templates(conn)
        rows = conn.execute(
            """
            SELECT t.* FROM agent_team_templates t
            JOIN (
                SELECT template_key, MAX(version) AS version
                FROM agent_team_templates GROUP BY template_key
            ) latest
              ON latest.template_key = t.template_key AND latest.version = t.version
            ORDER BY t.template_key
            """
        ).fetchall()
        items = [_template_dict(row) for row in rows]
        return {"items": items, "total": len(items)}


def get_team_template(template_key: str, version: int | str | None = None) -> dict[str, Any]:
    with store.session() as conn:
        templates.ensure_templates(conn)
        if version is None:
            template = templates.load_template(conn, template_key)
        else:
            try:
                version_num = int(version)
            except (TypeError, ValueError):
                raise PlatformError("invalid_payload", 422, "version 必须是整数")
            row = conn.execute(
                "SELECT * FROM agent_team_templates WHERE template_key = ? AND version = ?",
                (template_key, version_num),
            ).fetchone()
            template = _template_dict(row) if row else None
        if template is None:
            raise PlatformError("team_template_not_found", 404)
        return {"template": template}


def _validate_roles(roles: Any) -> list[dict[str, Any]]:
    if not isinstance(roles, list) or not roles:
        raise PlatformError("invalid_payload", 422, "roles 必须是非空数组")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in roles:
        if not isinstance(raw, dict):
            raise PlatformError("invalid_payload", 422, "roles 元素必须是对象")
        role_key = str(raw.get("role_key") or "").strip()
        if not role_key or role_key in seen:
            raise PlatformError("invalid_payload", 422, "role_key 缺失或重复")
        seen.add(role_key)
        role = dict(raw)
        role["role_key"] = role_key
        role["display_name"] = str(raw.get("display_name") or role_key)
        role.setdefault("responsibility", "")
        role.setdefault("layer_order", 1)
        role.setdefault("sort_order", 0)
        role.setdefault("parent_role_key", None)
        role.setdefault("agent_type", "subagent")
        role.setdefault("execution_mode", "subagent")
        role.setdefault("skills", [])
        role.setdefault("permission_profile", {})
        role.setdefault("is_required", True)
        normalized.append(role)
    return normalized


def create_template(payload: dict[str, Any]) -> dict[str, Any]:
    """创建自定义模板；同 key 已存在时自动递增版本（UNIQUE(template_key, version)）。"""
    if not isinstance(payload, dict):
        raise PlatformError("invalid_json", 400)
    key = str(payload.get("template_key") or payload.get("key") or "").strip()
    if not TEMPLATE_KEY_RE.match(key):
        raise PlatformError("invalid_payload", 422, "template_key 非法")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise PlatformError("invalid_payload", 422, "name 必填")
    roles = _validate_roles(payload.get("roles"))
    with store.session() as conn:
        if templates.load_template(conn, key) is not None:
            system = conn.execute(
                "SELECT is_system FROM agent_team_templates WHERE template_key = ? LIMIT 1",
                (key,),
            ).fetchone()
            if system and system["is_system"]:
                raise PlatformError("conflict", 409, "系统模板不能通过该接口覆盖")
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM agent_team_templates WHERE template_key = ?",
            (key,),
        ).fetchone()
        version = int(row["v"] or 0) + 1
        conn.execute(
            """
            INSERT INTO agent_team_templates
                (id, template_key, version, name, description, roles_json, is_system, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                f"tpl:{key}:v{version}",
                key,
                version,
                name,
                str(payload.get("description") or "").strip() or None,
                store.dumps_json(roles),
                store.now_iso(),
            ),
        )
        created = templates.load_template(conn, key)
        return {"template": created}


def clone_template(template_key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    new_key = str(payload.get("key") or payload.get("template_key") or "").strip()
    if not TEMPLATE_KEY_RE.match(new_key):
        raise PlatformError("invalid_payload", 422, "新 template_key 非法")
    with store.session() as conn:
        templates.ensure_templates(conn)
        source = templates.load_template(conn, template_key)
        if source is None:
            raise PlatformError("team_template_not_found", 404)
        if templates.load_template(conn, new_key) is not None:
            raise PlatformError("conflict", 409, "目标 template_key 已存在")
        name = str(payload.get("name") or f"{source['name']} 副本").strip()
        conn.execute(
            """
            INSERT INTO agent_team_templates
                (id, template_key, version, name, description, roles_json, is_system, created_at)
            VALUES (?, ?, 1, ?, ?, ?, 0, ?)
            """,
            (
                f"tpl:{new_key}:v1",
                new_key,
                name,
                source.get("description"),
                store.dumps_json(source.get("roles") or []),
                store.now_iso(),
            ),
        )
        return {"template": templates.load_template(conn, new_key)}


def _template_for_apply(
    conn: sqlite3.Connection, project_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    key = payload.get("template_key") or payload.get("key")
    version = payload.get("version")
    if key:
        template = get_team_template(str(key), version)["template"]
    else:
        project = _require_project(conn, project_id)
        template = get_team_template(
            project["team_template_key"], project.get("team_template_version")
        )["template"]
    if template is None:
        raise PlatformError("team_template_not_found", 404)
    if not template.get("roles"):
        raise PlatformError("invalid_payload", 422, "模板没有角色")
    return template


def _team_diff(
    conn: sqlite3.Connection, project_id: str, roles: list[dict[str, Any]]
) -> dict[str, Any]:
    existing = {
        row["instance_key"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM project_agents WHERE project_id = ?", (project_id,)
        ).fetchall()
    }
    template_keys: set[str] = set()
    added: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for role in roles:
        instance_key = f"{role['role_key']}-1"
        template_keys.add(instance_key)
        current = existing.get(instance_key)
        if current is None:
            added.append({"instance_key": instance_key, "role_key": role["role_key"]})
            continue
        snapshot = store.loads_json(current.get("role_snapshot_json"), {})
        if snapshot != role:
            updated.append({
                "instance_key": instance_key,
                "role_key": role["role_key"],
                "agent_id": current["id"],
            })
        else:
            kept.append({
                "instance_key": instance_key,
                "role_key": role["role_key"],
                "agent_id": current["id"],
            })
    removed = [
        {"instance_key": key, "role_key": row["role_key"], "agent_id": row["id"]}
        for key, row in existing.items()
        if key not in template_keys
    ]
    return {"added": added, "updated": updated, "removed": removed, "kept": kept}


def preview_team(project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    with store.session() as conn:
        _require_project(conn, project_id)
        template = _template_for_apply(conn, project_id, payload)
        diff = _team_diff(conn, project_id, template["roles"])
        return {
            "project_id": project_id,
            "template_key": template["template_key"],
            "version": template["version"],
            **diff,
        }


def apply_team(project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """应用模板：保留旧实例，只新增缺失 instance_key、更新快照，绝不删除。"""
    payload = payload or {}
    conn = store.connect()
    try:
        store.init_db(conn)
        _require_project(conn, project_id, write=True)
        template = _template_for_apply(conn, project_id, payload)
        roles = template["roles"]
        diff = _team_diff(conn, project_id, roles)
        now = store.now_iso()
        by_role = {
            row["role_key"]: row["id"]
            for row in conn.execute(
                "SELECT id, role_key FROM project_agents WHERE project_id = ?", (project_id,)
            ).fetchall()
        }
        created = 0
        for role in roles:
            instance_key = f"{role['role_key']}-1"
            current = conn.execute(
                "SELECT id FROM project_agents WHERE project_id = ? AND instance_key = ?",
                (project_id, instance_key),
            ).fetchone()
            if current is None:
                agent_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO project_agents
                        (id, project_id, role_key, display_name, instance_key, agent_type,
                         execution_mode, status, metadata_json, role_snapshot_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'offline', '{}', ?)
                    """,
                    (
                        agent_id,
                        project_id,
                        role["role_key"],
                        role["display_name"],
                        instance_key,
                        role["agent_type"],
                        role["execution_mode"],
                        store.dumps_json(role),
                    ),
                )
                by_role[role["role_key"]] = agent_id
                created += 1
            else:
                conn.execute(
                    "UPDATE project_agents SET role_snapshot_json = ? WHERE id = ?",
                    (store.dumps_json(role), current["id"]),
                )
        for role in roles:
            parent_role = role.get("parent_role_key")
            parent_id = by_role.get(parent_role) if parent_role else None
            conn.execute(
                "UPDATE project_agents SET parent_agent_id = ? "
                "WHERE project_id = ? AND instance_key = ?",
                (parent_id, project_id, f"{role['role_key']}-1"),
            )
        conn.execute(
            "UPDATE agent_projects SET team_template_key = ?, team_template_version = ?, updated_at = ? "
            "WHERE id = ?",
            (template["template_key"], template["version"], now, project_id),
        )
        audit(
            conn, project_id, "template.apply", "team_template", template["template_key"],
            detail={
                "version": template["version"],
                "added": [item["instance_key"] for item in diff["added"]],
                "updated": [item["instance_key"] for item in diff["updated"]],
                "removed": [item["instance_key"] for item in diff["removed"]],
                "created": created,
            },
        )
        _audit(conn, project_id, "template_applied", f"应用模板 {template['template_key']}")
        return {
            "project_id": project_id,
            "template_key": template["template_key"],
            "version": template["version"],
            "created": created,
            **diff,
            "agents": [_agent_dict(row) for row in conn.execute(
                "SELECT * FROM project_agents WHERE project_id = ? ORDER BY display_name",
                (project_id,),
            ).fetchall()],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 任务写闭环（B-05）
# ---------------------------------------------------------------------------
TASK_STATUSES = frozenset({"todo", "in_progress", "review", "blocked", "done"})
TASK_PRIORITIES = frozenset({"P0", "P1", "P2", "P3"})
TASK_TRANSITIONS: dict[str, set[str]] = {
    "todo": {"in_progress", "blocked"},
    "in_progress": {"review", "blocked"},
    "review": {"in_progress", "done"},
    "blocked": {"todo", "in_progress"},
    "done": {"in_progress"},
}


def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
    task = dict(row)
    task["acceptance_criteria"] = store.loads_json(
        task.pop("acceptance_criteria_json", None), []
    )
    task["readonly"] = task.get("source") == "board"
    return task


def _task_row(conn: sqlite3.Connection, project_id: str, task_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM tasks WHERE project_id = ? AND (id = ? OR task_key = ?)",
        (project_id, task_id, task_id),
    ).fetchone()
    if row is None:
        raise PlatformError("not_found", 404, "任务不存在")
    return _task_dict(row)


def _require_writable_task(task: dict[str, Any]) -> None:
    if task.get("readonly"):
        raise PlatformError("readonly_resource", 409, "镜像任务只读")


def _validate_assignee(conn: sqlite3.Connection, project_id: str, agent_id: Any) -> str | None:
    if agent_id is None or agent_id == "":
        return None
    text = str(agent_id)
    row = conn.execute(
        "SELECT id FROM project_agents WHERE project_id = ? AND id = ?",
        (project_id, text),
    ).fetchone()
    if row is None:
        raise PlatformError("invalid_payload", 422, "assignee_agent_id 不属于当前项目")
    return text


def _acceptance(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PlatformError("invalid_payload", 422, "acceptance_criteria 必须是数组")
    return value


def create_task(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlatformError("invalid_json", 400)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise PlatformError("invalid_payload", 422, "title 必填")
    priority = str(payload.get("priority") or "P2").upper()
    if priority not in TASK_PRIORITIES:
        raise PlatformError("invalid_payload", 422, "priority 必须是 P0-P3")
    task_key = str(payload.get("task_key") or "").strip() or f"T-{uuid.uuid4().hex[:6].upper()}"
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$", task_key):
        raise PlatformError("invalid_payload", 422, "task_key 非法")
    conn = store.connect()
    try:
        store.init_db(conn)
        _require_project(conn, project_id, write=True)
        assignee = _validate_assignee(conn, project_id, payload.get("assignee_agent_id"))
        now = store.now_iso()
        task_id = uuid.uuid4().hex
        try:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, project_id, task_key, title, status, priority, owner, qa, source,
                     updated_at, description, assignee_agent_id, created_by,
                     acceptance_criteria_json, version)
                VALUES (?, ?, ?, ?, 'todo', ?, NULL, NULL, 'platform', ?, ?, ?, ?, ?, 1)
                """,
                (
                    task_id,
                    project_id,
                    task_key,
                    title,
                    priority,
                    now,
                    str(payload.get("description") or "").strip() or None,
                    assignee,
                    str(payload.get("created_by") or "local"),
                    store.dumps_json(_acceptance(payload.get("acceptance_criteria"))),
                ),
            )
        except sqlite3.IntegrityError:
            raise PlatformError("conflict", 409, "task_key 已存在")
        _insert_task_event(conn, project_id, task_id, "task_created", f"创建任务 {task_key}")
        return {"task": _task_detail(conn, project_id, task_id)}
    finally:
        conn.close()


def _insert_task_event(
    conn: sqlite3.Connection, project_id: str, task_id: str, event_type: str, message: str
) -> None:
    occurred = store.now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO agent_events
            (id, project_id, agent_id, session_id, event_type, message, tool, ok, occurred_at)
        VALUES (?, ?, NULL, NULL, ?, ?, NULL, 1, ?)
        """,
        (
            store.stable_id("task-event", project_id, task_id, event_type, occurred),
            project_id,
            event_type,
            message,
            occurred,
        ),
    )


def _task_detail(conn: sqlite3.Connection, project_id: str, task_id: str) -> dict[str, Any]:
    task = _task_row(conn, project_id, task_id)
    dependencies = conn.execute(
        """
        SELECT d.*, t.task_key AS depends_on_task_key, t.title AS depends_on_title
        FROM task_dependencies d JOIN tasks t ON t.id = d.depends_on_task_id
        WHERE d.project_id = ? AND d.task_id = ?
        ORDER BY d.created_at
        """,
        (project_id, task["id"]),
    ).fetchall()
    dependents = conn.execute(
        """
        SELECT d.*, t.task_key AS dependent_task_key, t.title AS dependent_title
        FROM task_dependencies d JOIN tasks t ON t.id = d.task_id
        WHERE d.project_id = ? AND d.depends_on_task_id = ?
        ORDER BY d.created_at
        """,
        (project_id, task["id"]),
    ).fetchall()
    events = conn.execute(
        """
        SELECT id, event_type, message, occurred_at
        FROM agent_events
        WHERE project_id = ? AND message LIKE ?
        ORDER BY occurred_at DESC LIMIT 20
        """,
        (project_id, f"%{task['task_key']}%"),
    ).fetchall()
    task["dependencies"] = [dict(row) for row in dependencies]
    task["dependents"] = [dict(row) for row in dependents]
    task["events"] = [dict(row) for row in events]
    return task


def get_task(project_id: str, task_id: str) -> dict[str, Any]:
    with store.session() as conn:
        _require_project(conn, project_id)
        return {"task": _task_detail(conn, project_id, task_id)}


def patch_task(project_id: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlatformError("invalid_json", 400)
    conn = store.connect()
    try:
        store.init_db(conn)
        _require_project(conn, project_id, write=True)
        task = _task_row(conn, project_id, task_id)
        _require_writable_task(task)
        _check_version(payload, int(task.get("version") or 1))
        updates: list[str] = []
        params: list[Any] = []
        if "title" in payload:
            title = str(payload.get("title") or "").strip()
            if not title:
                raise PlatformError("invalid_payload", 422, "title 不能为空")
            updates.append("title = ?")
            params.append(title)
        if "description" in payload:
            updates.append("description = ?")
            params.append(str(payload.get("description") or "").strip() or None)
        if "priority" in payload:
            priority = str(payload.get("priority") or "").upper()
            if priority not in TASK_PRIORITIES:
                raise PlatformError("invalid_payload", 422, "priority 必须是 P0-P3")
            updates.append("priority = ?")
            params.append(priority)
        if "assignee_agent_id" in payload:
            assignee = _validate_assignee(conn, project_id, payload.get("assignee_agent_id"))
            updates.append("assignee_agent_id = ?")
            params.append(assignee)
        if "acceptance_criteria" in payload:
            updates.append("acceptance_criteria_json = ?")
            params.append(store.dumps_json(_acceptance(payload.get("acceptance_criteria"))))
        if "branch_name" in payload:
            updates.append("branch_name = ?")
            params.append(str(payload.get("branch_name") or "").strip() or None)
        if not updates:
            return {"task": _task_detail(conn, project_id, task["id"])}
        updates.extend(["updated_at = ?", "version = version + 1"])
        params.append(store.now_iso())
        params.append(task["id"])
        conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", tuple(params))
        _insert_task_event(conn, project_id, task["id"], "task_updated", f"更新任务 {task['task_key']}")
        return {"task": _task_detail(conn, project_id, task["id"])}
    finally:
        conn.close()


def transition_task(project_id: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlatformError("invalid_json", 400)
    to_status = str(payload.get("to_status") or "").strip()
    if to_status not in TASK_STATUSES:
        raise PlatformError("invalid_payload", 422, "to_status 非法")
    expected_raw = payload.get("expected_version")
    if expected_raw is None:
        raise PlatformError("invalid_payload", 422, "缺少 expected_version")
    try:
        expected_version = int(expected_raw)
    except (TypeError, ValueError):
        raise PlatformError("invalid_payload", 422, "expected_version 必须是整数")
    reason = str(payload.get("reason") or "").strip()
    blocked_reason = str(payload.get("blocked_reason") or reason).strip()

    conn = store.connect()
    try:
        store.init_db(conn)
        _require_project(conn, project_id, write=True)
        task = _task_row(conn, project_id, task_id)
        _require_writable_task(task)
        current = task.get("status")
        if expected_version != int(task.get("version") or 1):
            raise PlatformError("version_conflict", 409, f"版本不一致：当前 {task.get('version')}")
        if to_status == current or to_status not in TASK_TRANSITIONS.get(current, set()):
            raise PlatformError(
                "invalid_transition", 409, f"不允许 {current} -> {to_status}"
            )
        if to_status == "blocked" and not blocked_reason:
            raise PlatformError("invalid_payload", 422, "blocked 必须带 blocked_reason")
        if current == "done" and to_status == "in_progress" and not reason:
            raise PlatformError("invalid_payload", 422, "done -> in_progress 必须带 reason")

        now = store.now_iso()
        updates = ["status = ?", "updated_at = ?", "version = version + 1"]
        params: list[Any] = [to_status, now]
        if to_status == "blocked":
            updates.append("blocked_reason = ?")
            params.append(blocked_reason)
        else:
            updates.append("blocked_reason = NULL")
        if to_status == "in_progress" and not task.get("started_at"):
            updates.append("started_at = ?")
            params.append(now)
        if to_status == "done":
            updates.append("completed_at = ?")
            params.append(now)
        elif current == "done":
            updates.append("completed_at = NULL")
        params.append(task["id"])
        conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", tuple(params))
        audit(
            conn, project_id, "task.transition", "task", task["id"],
            detail={
                "task_key": task["task_key"],
                "from": current,
                "to": to_status,
                "reason": reason or None,
                "blocked_reason": blocked_reason or None,
            },
        )
        _insert_task_event(
            conn, project_id, task["id"], "task_transition",
            f"{task['task_key']} {current} -> {to_status}" + (f"：{reason}" if reason else ""),
        )
        return {"task": _task_detail(conn, project_id, task["id"])}
    finally:
        conn.close()


def _resolve_task_in_project(
    conn: sqlite3.Connection, project_id: str, reference: Any
) -> dict[str, Any]:
    text = str(reference or "").strip()
    if not text:
        raise PlatformError("invalid_payload", 422, "depends_on_task_id 必填")
    row = conn.execute(
        "SELECT * FROM tasks WHERE id = ? OR task_key = ?", (text, text)
    ).fetchone()
    if row is None:
        raise PlatformError("not_found", 404, "依赖任务不存在")
    if row["project_id"] != project_id:
        raise PlatformError("cross_project_reference", 422, "依赖任务不属于当前项目")
    return _task_dict(row)


def _dependency_cycle(
    conn: sqlite3.Connection, project_id: str, task_id: str, depends_on_task_id: str
) -> bool:
    """新增 task_id -> depends_on 边后，若 depends_on 能回到 task_id 则成环。"""
    edges: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT task_id, depends_on_task_id FROM task_dependencies WHERE project_id = ?",
        (project_id,),
    ).fetchall():
        edges.setdefault(row["task_id"], []).append(row["depends_on_task_id"])
    edges.setdefault(task_id, []).append(depends_on_task_id)
    seen: set[str] = set()
    stack = [depends_on_task_id]
    while stack:
        node = stack.pop()
        if node == task_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges.get(node, []))
    return False


def add_dependency(project_id: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlatformError("invalid_json", 400)
    conn = store.connect()
    try:
        store.init_db(conn)
        _require_project(conn, project_id, write=True)
        task = _task_row(conn, project_id, task_id)
        _require_writable_task(task)
        depends = _resolve_task_in_project(conn, project_id, payload.get("depends_on_task_id"))
        if depends["id"] == task["id"]:
            raise PlatformError("invalid_payload", 422, "任务不能依赖自身")
        dependency_type = str(payload.get("dependency_type") or "blocks")
        if dependency_type not in {"blocks", "related"}:
            raise PlatformError("invalid_payload", 422, "dependency_type 非法")
        if _dependency_cycle(conn, project_id, task["id"], depends["id"]):
            raise PlatformError("dependency_cycle", 422, "依赖成环")
        now = store.now_iso()
        dep_id = uuid.uuid4().hex
        try:
            conn.execute(
                """
                INSERT INTO task_dependencies
                    (id, project_id, task_id, depends_on_task_id, dependency_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (dep_id, project_id, task["id"], depends["id"], dependency_type, now),
            )
        except sqlite3.IntegrityError:
            raise PlatformError("conflict", 409, "依赖已存在")
        _insert_task_event(
            conn, project_id, task["id"], "dependency_added",
            f"{task['task_key']} 依赖 {depends['task_key']}",
        )
        return {"dependency_id": dep_id, "task": _task_detail(conn, project_id, task["id"])}
    finally:
        conn.close()


def delete_dependency(
    project_id: str, task_id: str, dependency_id: str
) -> dict[str, Any]:
    conn = store.connect()
    try:
        store.init_db(conn)
        _require_project(conn, project_id, write=True)
        task = _task_row(conn, project_id, task_id)
        _require_writable_task(task)
        row = conn.execute(
            "SELECT id FROM task_dependencies WHERE id = ? AND project_id = ? AND task_id = ?",
            (dependency_id, project_id, task["id"]),
        ).fetchone()
        if row is None:
            raise PlatformError("not_found", 404, "依赖不存在")
        conn.execute("DELETE FROM task_dependencies WHERE id = ?", (dependency_id,))
        _insert_task_event(
            conn, project_id, task["id"], "dependency_removed",
            f"删除 {task['task_key']} 的依赖 {dependency_id}",
        )
        return {"ok": True, "task": _task_detail(conn, project_id, task["id"])}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 交付验收（B-06）
# ---------------------------------------------------------------------------
def _delivery_dict(row: sqlite3.Row, task_key: str | None = None) -> dict[str, Any]:
    item = dict(row)
    item["readonly"] = item.get("source") == "mirror"
    if task_key is not None:
        item["task_key"] = task_key
    return item


def create_delivery(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlatformError("invalid_json", 400)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise PlatformError("invalid_payload", 422, "title 必填")
    kind = str(payload.get("kind") or "other").strip() or "other"
    conn = store.connect()
    try:
        store.init_db(conn)
        _require_project(conn, project_id, write=True)
        task_id = None
        task_reference = payload.get("task_id")
        if task_reference:
            task_id = _resolve_task_in_project(conn, project_id, task_reference)["id"]
        agent_id = payload.get("agent_id")
        if agent_id:
            _validate_assignee(conn, project_id, agent_id)
        now = store.now_iso()
        delivery_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO deliveries
                (id, project_id, task_id, title, kind, path, sha, created_at,
                 summary, status, updated_at, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?, 'platform')
            """,
            (
                delivery_id,
                project_id,
                task_id,
                title,
                kind,
                str(payload.get("path") or "").strip() or None,
                str(payload.get("sha") or "").strip() or None,
                now,
                str(payload.get("summary") or "").strip() or None,
                now,
            ),
        )
        row = conn.execute(
            "SELECT d.*, t.task_key FROM deliveries d "
            "LEFT JOIN tasks t ON t.id = d.task_id AND t.project_id = d.project_id "
            "WHERE d.id = ?",
            (delivery_id,),
        ).fetchone()
        return {"delivery": _delivery_dict(row, row["task_key"])}
    finally:
        conn.close()


def review_delivery(
    project_id: str, delivery_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlatformError("invalid_json", 400)
    decision = str(payload.get("decision") or "").strip()
    if decision not in {"accepted", "rejected"}:
        raise PlatformError("invalid_payload", 422, "decision 必须是 accepted/rejected")
    comment = str(payload.get("comment") or "").strip()
    if decision == "rejected" and not comment:
        raise PlatformError("invalid_payload", 422, "rejected 必须带 comment")
    conn = store.connect()
    try:
        store.init_db(conn)
        _require_project(conn, project_id, write=True)
        row = conn.execute(
            "SELECT * FROM deliveries WHERE id = ? AND project_id = ?",
            (delivery_id, project_id),
        ).fetchone()
        if row is None:
            raise PlatformError("not_found", 404, "交付不存在")
        if row["source"] == "mirror":
            raise PlatformError("readonly_resource", 409, "镜像交付只读")
        now = store.now_iso()
        conn.execute(
            """
            UPDATE deliveries SET status = ?, reviewer = ?, reviewed_at = ?, rejection_reason = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                decision,
                str(payload.get("reviewer") or "local"),
                now,
                comment or None,
                now,
                delivery_id,
            ),
        )
        audit(
            conn, project_id, "delivery.review", "delivery", delivery_id,
            detail={"decision": decision, "comment": comment or None,
                    "title": row["title"]},
        )
        updated = conn.execute(
            "SELECT d.*, t.task_key FROM deliveries d "
            "LEFT JOIN tasks t ON t.id = d.task_id AND t.project_id = d.project_id "
            "WHERE d.id = ?",
            (delivery_id,),
        ).fetchone()
        return {"delivery": _delivery_dict(updated, updated["task_key"])}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 执行记录（B-07）
# ---------------------------------------------------------------------------
def _execution_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["total_tokens"] = int(item.get("total_tokens") or 0)
    return item


def list_executions(
    project_id: str,
    *,
    page: int = 1,
    page_size: int = 20,
    agent: str | None = None,
    status: str | None = None,
    from_: str | None = None,
    to: str | None = None,
) -> dict[str, Any]:
    page, size = _pagination(page, page_size)
    with store.session() as conn:
        _require_project(conn, project_id)
        where = ["project_id = ?"]
        params: list[Any] = [project_id]
        if agent:
            where.append("agent_id = ?")
            params.append(agent)
        if status:
            where.append("status = ?")
            params.append(status)
        if from_:
            where.append("COALESCE(started_at, finished_at) >= ?")
            params.append(store.iso_utc(from_))
        if to:
            where.append("COALESCE(started_at, finished_at) <= ?")
            params.append(store.iso_utc(to))
        clause = " AND ".join(where)
        total = _scalar(conn, f"SELECT COUNT(*) FROM executions WHERE {clause}", tuple(params))
        rows = conn.execute(
            f"SELECT * FROM executions WHERE {clause} "
            "ORDER BY COALESCE(started_at, finished_at) DESC LIMIT ? OFFSET ?",
            (*params, size, (page - 1) * size),
        ).fetchall()
        return {
            "items": [_execution_dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": size,
        }


def get_execution(project_id: str, execution_id: str) -> dict[str, Any]:
    with store.session() as conn:
        _require_project(conn, project_id)
        row = conn.execute(
            "SELECT * FROM executions WHERE project_id = ? AND (id = ? OR session_id = ?)",
            (project_id, execution_id, execution_id),
        ).fetchone()
        if row is None:
            raise PlatformError("not_found", 404, "执行记录不存在")
        return {"execution": _execution_dict(row)}


# ---------------------------------------------------------------------------
# 文档 / 收藏 / 最近访问（B-08）
# ---------------------------------------------------------------------------
DOCUMENT_TYPES = frozenset({"readme", "prd", "adr", "api", "team", "skills", "other"})


def _normalize_document_uri(project: dict[str, Any], raw: Any) -> str:
    uri = str(raw or "").strip()
    if not uri:
        raise PlatformError("invalid_payload", 422, "uri 必填")
    candidate = Path(uri)
    if candidate.is_absolute():
        raise PlatformError("invalid_payload", 422, "uri 必须是项目内相对路径")
    root = Path(project["repo_root"]).resolve()
    try:
        resolved = (root / candidate).resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise PlatformError("invalid_payload", 422, "uri 越出项目仓库")
    return candidate.as_posix()


def _document_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def list_documents(
    project_id: str, *, page: int = 1, page_size: int = 20, q: str | None = None
) -> dict[str, Any]:
    page, size = _pagination(page, page_size)
    with store.session() as conn:
        _require_project(conn, project_id)
        where = ["project_id = ?"]
        params: list[Any] = [project_id]
        if q:
            where.append("(title LIKE ? OR uri LIKE ?)")
            like = f"%{q[:80]}%"
            params.extend([like, like])
        clause = " AND ".join(where)
        total = _scalar(conn, f"SELECT COUNT(*) FROM project_documents WHERE {clause}", tuple(params))
        rows = conn.execute(
            f"SELECT * FROM project_documents WHERE {clause} "
            "ORDER BY COALESCE(updated_at, created_at) DESC LIMIT ? OFFSET ?",
            (*params, size, (page - 1) * size),
        ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total,
                "page": page, "page_size": size}


def create_document(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlatformError("invalid_json", 400)
    conn = store.connect()
    try:
        store.init_db(conn)
        project = _require_project(conn, project_id, write=True)
        title = str(payload.get("title") or "").strip()
        if not title:
            raise PlatformError("invalid_payload", 422, "title 必填")
        uri = _normalize_document_uri(project, payload.get("uri"))
        document_type = str(payload.get("document_type") or "other")
        if document_type not in DOCUMENT_TYPES:
            raise PlatformError("invalid_payload", 422, "document_type 非法")
        now = store.now_iso()
        doc_id = uuid.uuid4().hex
        try:
            conn.execute(
                """
                INSERT INTO project_documents
                    (id, project_id, title, document_type, uri, owner_agent_id,
                     updated_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_id,
                    project_id,
                    title,
                    document_type,
                    uri,
                    str(payload.get("owner_agent_id") or "").strip() or None,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise PlatformError("conflict", 409, "该 uri 已索引")
        row = conn.execute("SELECT * FROM project_documents WHERE id = ?", (doc_id,)).fetchone()
        return {"document": _document_dict(row)}
    finally:
        conn.close()


def patch_document(project_id: str, document_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlatformError("invalid_json", 400)
    conn = store.connect()
    try:
        store.init_db(conn)
        _require_project(conn, project_id, write=True)
        row = conn.execute(
            "SELECT * FROM project_documents WHERE id = ? AND project_id = ?",
            (document_id, project_id),
        ).fetchone()
        if row is None:
            raise PlatformError("not_found", 404, "文档索引不存在")
        updates: list[str] = []
        params: list[Any] = []
        if "title" in payload:
            title = str(payload.get("title") or "").strip()
            if not title:
                raise PlatformError("invalid_payload", 422, "title 不能为空")
            updates.append("title = ?")
            params.append(title)
        if "document_type" in payload:
            document_type = str(payload.get("document_type") or "")
            if document_type not in DOCUMENT_TYPES:
                raise PlatformError("invalid_payload", 422, "document_type 非法")
            updates.append("document_type = ?")
            params.append(document_type)
        if "owner_agent_id" in payload:
            updates.append("owner_agent_id = ?")
            params.append(str(payload.get("owner_agent_id") or "").strip() or None)
        if not updates:
            return {"document": _document_dict(row)}
        updates.append("updated_at = ?")
        params.append(store.now_iso())
        params.append(document_id)
        conn.execute(
            f"UPDATE project_documents SET {', '.join(updates)} WHERE id = ?", tuple(params)
        )
        updated = conn.execute(
            "SELECT * FROM project_documents WHERE id = ?", (document_id,)
        ).fetchone()
        return {"document": _document_dict(updated)}
    finally:
        conn.close()


def delete_document(project_id: str, document_id: str) -> dict[str, Any]:
    conn = store.connect()
    try:
        store.init_db(conn)
        _require_project(conn, project_id, write=True)
        cur = conn.execute(
            "DELETE FROM project_documents WHERE id = ? AND project_id = ?",
            (document_id, project_id),
        )
        if cur.rowcount == 0:
            raise PlatformError("not_found", 404, "文档索引不存在")
        return {"ok": True, "id": document_id}
    finally:
        conn.close()


def set_favorite(project_id: str, favorite: bool) -> dict[str, Any]:
    conn = store.connect()
    try:
        store.init_db(conn)
        _require_project(conn, project_id)
        if favorite:
            conn.execute(
                "INSERT OR IGNORE INTO project_favorites(user_key, project_id, created_at) "
                "VALUES ('local', ?, ?)",
                (project_id, store.now_iso()),
            )
        else:
            conn.execute(
                "DELETE FROM project_favorites WHERE user_key = 'local' AND project_id = ?",
                (project_id,),
            )
        return {"project_id": project_id, "favorite": favorite}
    finally:
        conn.close()


def record_visit(project_id: str) -> dict[str, Any]:
    conn = store.connect()
    try:
        store.init_db(conn)
        _require_project(conn, project_id)
        visited_at = store.now_iso()
        conn.execute(
            """
            INSERT INTO project_recent_visits(user_key, project_id, visited_at)
            VALUES ('local', ?, ?)
            ON CONFLICT(user_key, project_id) DO UPDATE SET visited_at = excluded.visited_at
            """,
            (project_id, visited_at),
        )
        return {"project_id": project_id, "visited_at": visited_at}
    finally:
        conn.close()


def recent_projects(limit: int = 20) -> dict[str, Any]:
    size = max(1, min(int(limit or 20), 20))
    with store.session() as conn:
        rows = conn.execute(
            """
            SELECT p.*, v.visited_at
            FROM project_recent_visits v JOIN agent_projects p ON p.id = v.project_id
            WHERE v.user_key = 'local'
            ORDER BY v.visited_at DESC, v.rowid DESC LIMIT ?
            """,
            (size,),
        ).fetchall()
        items = []
        for row in rows:
            item = _project_dict(row)
            item["favorite"] = _is_favorite(conn, item["id"])
            item.pop("settings_json", None)
            items.append(item)
        return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# 审计查询（B-09）
# ---------------------------------------------------------------------------
def record_file_open(project_id: str, path: str) -> None:
    """文件打开审计（采样：同一文件每小时只记一次，降低噪声）。"""
    bucket = store.stable_id("file-open", project_id, path, store.now_iso()[:13])
    if int(bucket[-1], 16) % 10 != 0:
        return
    try:
        with store.session() as conn:
            audit(conn, project_id, "file.open", "file", path)
    except sqlite3.Error:
        pass


def list_audit_logs(
    *,
    project: str | None = None,
    action: str | None = None,
    from_: str | None = None,
    to: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    page, size = _pagination(page, page_size)
    with store.session() as conn:
        where: list[str] = []
        params: list[Any] = []
        if project:
            where.append("project_id = ?")
            params.append(project)
        if action:
            where.append("action = ?")
            params.append(action)
        if from_:
            where.append("created_at >= ?")
            params.append(store.iso_utc(from_))
        if to:
            where.append("created_at <= ?")
            params.append(store.iso_utc(to))
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        total = _scalar(conn, f"SELECT COUNT(*) FROM audit_logs{clause}", tuple(params))
        rows = conn.execute(
            f"SELECT * FROM audit_logs{clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, size, (page - 1) * size),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["detail"] = store.loads_json(item.pop("detail_json", None), {})
            items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": size}


# ---------------------------------------------------------------------------
# 幂等键（B-03 可选）
# ---------------------------------------------------------------------------
def idempotency_lookup(key: str, fingerprint: str) -> dict[str, Any] | None:
    with store.session() as conn:
        row = conn.execute(
            "SELECT fingerprint, response_json FROM idempotency_keys WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise PlatformError("idempotency_conflict", 409, "Idempotency-Key 已用于不同请求")
        return store.loads_json(row["response_json"], {})


def idempotency_store(key: str, fingerprint: str, response: dict[str, Any]) -> None:
    with store.session() as conn:
        conn.execute(
            """
            INSERT INTO idempotency_keys(key, fingerprint, response_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (key, fingerprint, store.dumps_json(response), store.now_iso()),
        )


def backfill_project(project_id: str) -> dict[str, Any]:
    with store.session() as conn:
        project = get_project_with(conn, project_id, include_agents=False)
        _require_writable(project)
        counts = sources.backfill(conn, project)
        conn.execute(
            "UPDATE agent_projects SET updated_at = ? WHERE id = ?",
            (store.now_iso(), project_id),
        )
        return {"ok": True, "project_id": project_id, "counts": counts}


def state_meta(project_id: str) -> dict[str, Any]:
    """供 HTTP 旧 ``/api/state`` 兼容层使用的项目元信息。"""
    with store.session() as conn:
        project = get_project_with(conn, project_id, include_agents=False)
        return {
            "id": project["id"],
            "code": project["code"],
            "name": project["name"],
            "status": project["status"],
            "repo_root": project["repo_root"],
        }
