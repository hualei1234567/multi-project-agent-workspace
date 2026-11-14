"""第四期 Intelligence & Operations Layer（P4-M1 起）。

约定（ADR-P4-001/002）：

* 与第三期 Control Plane **同进程、同库、同 HTTP 层**，不新增服务；
* 第三期负责：项目、任务、调度、执行、运行时、审计、Usage；
* 第四期负责：身份与授权、Policy、Kill Switch、审批、事件平面，
  以及其上的 Evaluation / Knowledge / Workflow / Release / Improvement /
  Outcome / Autonomy 七个业务域；
* 任何写操作必须经过 :func:`p4.policy.decide`，任何执行必须经过
  ``agent_platform.executor``，任何状态变更必须写审计。
"""

from __future__ import annotations

__all__ = ["events", "governance", "killswitch", "policy"]


def audited(action: str, resource_type: str, *, id_arg: str = "id"):
    """给高风险状态变更函数补审计（R2 / S-3）。

    为什么用装饰器而不是在每个函数体里手写：P4 八个域共约 20 个状态变更入口，
    逐个手写既容易漏，也无法保证格式一致。装饰器统一取：

    * ``project_id``：优先用函数参数里的同名参数，其次从结果/入参推断；
    * ``actor``：优先 ``actor`` / ``approved_by`` / ``published_by`` / ``reviewed_by``；
    * ``resource_id``：取 ``id_arg`` 指定的参数（默认 ``id``）。

    审计失败不阻断业务（与 ``service.audit`` 的语义一致）：记录本身就失败时
    不该让发布这类操作连带失败，但会打印到 stderr 便于发现。
    """
    import functools
    import sqlite3
    import sys
    from collections.abc import Callable
    from typing import Any

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = fn(*args, **kwargs)
            try:
                conn = args[0] if args and isinstance(args[0], sqlite3.Connection) else None
                if conn is not None:
                    from .. import service as _service
                    bound_kw = dict(kwargs)
                    # 位置参数与形参名对应（跳过 conn）
                    names = list(fn.__code__.co_varnames[: fn.__code__.co_argcount])
                    for idx, name in enumerate(names[1:], start=1):
                        if idx < len(args):
                            bound_kw.setdefault(name, args[idx])
                    project_id = bound_kw.get("project_id")
                    if not project_id and isinstance(result, dict):
                        project_id = result.get("project_id")
                    actor = ""
                    for key in ("actor", "approved_by", "published_by",
                                "reviewed_by", "created_by", "revoked_by"):
                        if bound_kw.get(key):
                            actor = str(bound_kw[key])
                            break
                    resource_id = bound_kw.get(id_arg) or (
                        result.get("id") if isinstance(result, dict) else None
                    )
                    with conn:
                        _service.audit(
                            conn, str(project_id) if project_id else None,
                            action, resource_type,
                            str(resource_id) if resource_id else None,
                            actor=actor or "local",
                            detail={"fn": fn.__name__},
                        )
            except Exception as exc:  # noqa: BLE001 - 审计失败不阻断业务
                print(f"[p4.audit] {action} 审计写入失败：{exc}", file=sys.stderr)
            return result

        return wrapper

    return decorator
