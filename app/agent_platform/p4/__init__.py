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
