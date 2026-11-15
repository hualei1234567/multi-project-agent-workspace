"""评估域 V1：Suite / Dataset / Case / Evaluator / Run / Result（P4-M1）。

设计要点（对齐 V6.0 §9.2/§9.3/§12）：

* **版本不可变**：Dataset 版本 ``frozen`` 后内容不再修改，改样本只能出新版本；
* **运行状态与决策状态分开**：``status`` 表示跑没跑完，``decision`` 表示评没评过；
* **未执行的样本不计入通过数**：``partial`` 必须显示实际执行数与跳过数；
* **安全维度不能被平均分掩盖**：任一 safety 检查失败，整轮直接 ``fail``；
* **不建第二套执行引擎**：V1 评估的是"已有产出"（candidate output），
  产出由第三期执行器产生；需要现场生成时由上层先跑 ``executor`` 再喂进来。
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any

from .. import store
from . import events

DEFAULT_MIN_SCORE = 0.8

VALID_CHECKS = frozenset({"exact", "contains", "not_contains", "regex", "json_keys"})
VALID_SPLITS = frozenset({"holdout", "regression", "safety", "train"})


class EvaluationError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------------------
# Suite / Dataset / Version / Case
# --------------------------------------------------------------------------
def create_suite(conn: sqlite3.Connection, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise EvaluationError("invalid_payload", 422, "name 必填")
    now = store.now_iso()
    suite_id = _new_id("suite")
    try:
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO eval_suites (id, project_id, name, description, created_by,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (suite_id, project_id, name, payload.get("description"),
                 str(payload.get("created_by") or "local"), now, now),
            )
            events.emit(conn, "evaluation.suite.created", project_id=project_id,
                        resource_type="evaluation_suite", resource_id=suite_id)
    except sqlite3.IntegrityError as exc:
        raise EvaluationError("conflict", 409, "同一项目下 Suite 名称已存在") from exc
    return get_suite(conn, suite_id)


def get_suite(conn: sqlite3.Connection, suite_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM eval_suites WHERE id = ?", (suite_id,)).fetchone()
    if row is None:
        raise EvaluationError("not_found", 404, "Suite 不存在")
    return dict(row)


def list_suites(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM eval_suites WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}


def create_dataset(conn: sqlite3.Connection, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    suite_id = str(payload.get("suite_id") or "").strip()
    split = str(payload.get("split") or "regression")
    if not name or not suite_id:
        raise EvaluationError("invalid_payload", 422, "name 与 suite_id 必填")
    if split not in VALID_SPLITS:
        raise EvaluationError("invalid_payload", 422, f"split 必须是 {sorted(VALID_SPLITS)}")
    suite = get_suite(conn, suite_id)
    if suite["project_id"] != project_id:
        raise EvaluationError("forbidden", 403, "Suite 不属于该项目")

    dataset_id = _new_id("ds")
    try:
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO eval_datasets (id, project_id, suite_id, name, split, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (dataset_id, project_id, suite_id, name, split, store.now_iso()),
            )
    except sqlite3.IntegrityError as exc:
        raise EvaluationError("conflict", 409, "同一 Suite 下数据集名称已存在") from exc
    return get_dataset(conn, dataset_id)


def get_dataset(conn: sqlite3.Connection, dataset_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM eval_datasets WHERE id = ?", (dataset_id,)).fetchone()
    if row is None:
        raise EvaluationError("not_found", 404, "数据集不存在")
    return dict(row)


def list_datasets(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT d.*, (SELECT MAX(version) FROM eval_dataset_versions v"
        "  WHERE v.dataset_id = d.id) AS latest_version"
        " FROM eval_datasets d WHERE d.project_id = ? ORDER BY d.created_at DESC",
        (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}


def freeze_version(conn: sqlite3.Connection, dataset_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """用给定样本创建一个**不可变**数据集版本。"""
    dataset = get_dataset(conn, dataset_id)
    cases = payload.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise EvaluationError("invalid_payload", 422, "cases 不能为空")

    seen: set[str] = set()
    for case in cases:
        key = str(case.get("case_key") or "").strip()
        if not key:
            raise EvaluationError("invalid_payload", 422, "每个 case 必须有 case_key")
        if key in seen:
            raise EvaluationError("invalid_payload", 422, f"case_key 重复：{key}")
        seen.add(key)

    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM eval_dataset_versions WHERE dataset_id = ?",
        (dataset_id,),
    ).fetchone()
    version_no = int(row["v"]) + 1
    version_id = _new_id("dsv")
    now = store.now_iso()
    digest = store.stable_id(dataset_id, version_no, *sorted(seen))

    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO eval_dataset_versions (id, dataset_id, version, case_count,"
            " content_digest, status, created_at, frozen_at) VALUES (?, ?, ?, ?, ?, 'frozen', ?, ?)",
            (version_id, dataset_id, version_no, len(cases), digest, now, now),
        )
        for case in cases:
            conn.execute(
                "INSERT INTO eval_cases (id, version_id, case_key, input_json, expected_json,"
                " tags_json, weight, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_id("case"), version_id, str(case["case_key"]),
                    store.dumps_json(case.get("input") or {}),
                    store.dumps_json(case.get("expected") or {}),
                    store.dumps_json(case.get("tags") or []),
                    float(case.get("weight") or 1.0), now,
                ),
            )
        events.emit(conn, "evaluation.dataset.frozen", project_id=dataset["project_id"],
                    resource_type="evaluation_dataset_version", resource_id=version_id,
                    payload={"version": version_no, "case_count": len(cases)})
    return get_version(conn, version_id)


def get_version(conn: sqlite3.Connection, version_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM eval_dataset_versions WHERE id = ?", (version_id,)
    ).fetchone()
    if row is None:
        raise EvaluationError("not_found", 404, "数据集版本不存在")
    return dict(row)


def list_cases(conn: sqlite3.Connection, version_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM eval_cases WHERE version_id = ? ORDER BY case_key", (version_id,)
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Evaluator
# --------------------------------------------------------------------------
def create_evaluator(conn: sqlite3.Connection, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    rules = payload.get("rules")
    if not name:
        raise EvaluationError("invalid_payload", 422, "name 必填")
    if not isinstance(rules, dict) or not rules.get("checks"):
        raise EvaluationError("invalid_payload", 422, "rules.checks 不能为空")
    for check in rules["checks"]:
        if check.get("type") not in VALID_CHECKS:
            raise EvaluationError(
                "invalid_payload", 422, f"不支持的检查类型：{check.get('type')}"
            )

    evaluator_id = _new_id("ev")
    version_id = _new_id("evv")
    now = store.now_iso()
    try:
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO eval_evaluators (id, project_id, name, kind, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (evaluator_id, project_id, name,
                 str(payload.get("kind") or "deterministic"), now),
            )
            conn.execute(
                "INSERT INTO eval_evaluator_versions (id, evaluator_id, version, rules_json,"
                " created_at) VALUES (?, ?, 1, ?, ?)",
                (version_id, evaluator_id, store.dumps_json(rules), now),
            )
    except sqlite3.IntegrityError as exc:
        raise EvaluationError("conflict", 409, "同一项目下评估器名称已存在") from exc
    return {"evaluator": get_evaluator(conn, evaluator_id), "version_id": version_id}


def get_evaluator(conn: sqlite3.Connection, evaluator_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM eval_evaluators WHERE id = ?", (evaluator_id,)).fetchone()
    if row is None:
        raise EvaluationError("not_found", 404, "评估器不存在")
    return dict(row)


def list_evaluators(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT e.*, (SELECT MAX(version) FROM eval_evaluator_versions v"
        "  WHERE v.evaluator_id = e.id) AS latest_version"
        " FROM eval_evaluators e WHERE e.project_id = ? ORDER BY e.created_at DESC",
        (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}


# --------------------------------------------------------------------------
# 规则引擎
# --------------------------------------------------------------------------
def _check_one(check: dict[str, Any], output: str, expected: dict[str, Any]) -> bool:
    kind = check["type"]
    target = check.get("value")
    if target is None and check.get("field"):
        target = expected.get(check["field"])
    if target is None and kind not in {"regex"}:
        return False
    text = str(target)
    if kind == "exact":
        return output.strip() == text.strip()
    if kind == "contains":
        return text in output
    if kind == "not_contains":
        return text not in output
    if kind == "regex":
        try:
            return re.search(str(check.get("value") or ""), output) is not None
        except re.error:
            return False
    if kind == "json_keys":
        return all(key in expected for key in str(target).split(","))
    return False


def score_case(rules: dict[str, Any], output: str, expected: dict[str, Any]) -> dict[str, Any]:
    checks = rules.get("checks") or []
    total_weight = 0.0
    passed_weight = 0.0
    details = []
    safety_violation = False
    for check in checks:
        weight = float(check.get("weight") or 1.0)
        total_weight += weight
        ok = _check_one(check, output, expected)
        if ok:
            passed_weight += weight
        if check.get("safety") and not ok:
            safety_violation = True
        details.append({
            "dimension": check.get("dimension") or check["type"],
            "type": check["type"],
            "passed": ok,
            "safety": bool(check.get("safety")),
        })
    score = (passed_weight / total_weight) if total_weight else 0.0
    return {
        "score": round(score, 4),
        "safety_violation": safety_violation,
        "checks": details,
    }


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------
def _output_digest(outputs: dict[str, Any]) -> str:
    """候选输出的内容摘要：同样输入应得到同样评估，便于复现与比对。"""
    import hashlib

    canonical = json.dumps(
        {str(k): str(v) for k, v in sorted(outputs.items())},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_runtime_outputs(conn: sqlite3.Connection, project_id: str,
                          execution_id: str) -> dict[str, str]:
    """从真实 execution 取候选输出（R3 / M1）。

    输出写在 executions 关联的证据里。拿不到就报错——**不允许**在这种情况下
    回落到调用方塞的 candidate_outputs 冒充「运行时产出」，那正是审计指出的问题。
    """
    row = conn.execute(
        "SELECT * FROM executions WHERE id = ? AND project_id = ?",
        (execution_id, project_id),
    ).fetchone()
    if row is None:
        raise EvaluationError("not_found", 404, "执行记录不存在或不属于该项目")
    if row["status"] != "succeeded":
        raise EvaluationError(
            "invalid_state", 409,
            f"执行状态为 {row['status']}，只有 succeeded 的执行才能作为评估输入",
        )
    # sqlite3.Row 没有 .get()，先转 dict 再取可选列。
    raw = dict(row).get("summary")
    payload = store.loads_json(raw, None)
    if isinstance(payload, dict) and isinstance(payload.get("outputs"), dict):
        return {str(k): str(v) for k, v in payload["outputs"].items()}
    raise EvaluationError(
        "missing_outputs", 422,
        "该执行没有可用的 outputs 证据；评估必须读取真实运行时产出，"
        "不能回落到调用方传入的候选输出",
    )


def run_evaluation(conn: sqlite3.Connection, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """执行一次评估。

    两种候选输出来源，**必须显式指定**且来源会被记录：

    * ``runtime_execution_id``：从真实 execution 读产出（推荐，M1 验收路径）；
    * ``candidate_outputs``：调用方直接传入（骨架期做法，会标 ``caller_supplied``，
      报表里能看出这次评估并没有真的跑过 runtime）。
    """
    version_id = str(payload.get("dataset_version_id") or "").strip()
    evaluator_version_id = str(payload.get("evaluator_version_id") or "").strip()
    if not version_id or not evaluator_version_id:
        raise EvaluationError("invalid_payload", 422, "dataset_version_id 与 evaluator_version_id 必填")

    runtime_execution_id = str(payload.get("runtime_execution_id") or "").strip()
    if runtime_execution_id:
        outputs = _load_runtime_outputs(conn, project_id, runtime_execution_id)
        output_source = "runtime"
    else:
        outputs = payload.get("candidate_outputs") or {}
        output_source = "caller_supplied"
    if not isinstance(outputs, dict):
        raise EvaluationError("invalid_payload", 422, "candidate_outputs 必须是对象")

    version = get_version(conn, version_id)
    dataset = get_dataset(conn, version["dataset_id"])
    if dataset["project_id"] != project_id:
        raise EvaluationError("forbidden", 403, "数据集不属于该项目")

    ev_row = conn.execute(
        "SELECT * FROM eval_evaluator_versions WHERE id = ?", (evaluator_version_id,)
    ).fetchone()
    if ev_row is None:
        raise EvaluationError("not_found", 404, "评估器版本不存在")
    evaluator = get_evaluator(conn, ev_row["evaluator_id"])
    if evaluator["project_id"] != project_id:
        raise EvaluationError("forbidden", 403, "评估器不属于该项目")

    rules = store.loads_json(ev_row["rules_json"], {}) or {}
    min_score = float(rules.get("min_score") or DEFAULT_MIN_SCORE)
    cases = list_cases(conn, version_id)

    run_id = _new_id("run")
    now = store.now_iso()
    correlation_id = payload.get("correlation_id") or _new_id("corr")

    total = len(cases)
    executed = passed = failed = skipped = safety_hits = 0
    weighted_total = 0.0
    weighted_pass = 0.0
    rows: list[tuple] = []

    for case in cases:
        key = case["case_key"]
        output = outputs.get(key)
        if output is None:
            skipped += 1
            rows.append((_new_id("res"), run_id, case["id"], key, "skipped", None,
                         store.dumps_json({"reason": "no_candidate_output"}), now))
            continue
        expected = store.loads_json(case["expected_json"], {}) or {}
        result = score_case(rules, str(output), expected)
        executed += 1
        weight = float(case["weight"] or 1.0)
        weighted_total += weight
        if result["safety_violation"]:
            safety_hits += 1
        # 轮次分数是"各用例得分的加权平均"，决策另外由安全项与阈值决定：
        # 安全违规时分数仍如实显示（0.9），但 decision 必定为 fail。
        weighted_pass += weight * float(result["score"])
        ok = result["score"] >= min_score and not result["safety_violation"]
        if ok:
            passed += 1
        else:
            failed += 1
        rows.append((_new_id("res"), run_id, case["id"], key,
                     "pass" if ok else "fail", result["score"],
                     store.dumps_json(result), now))

    if executed == 0:
        status, decision = "failed", "undetermined"
    elif executed < total:
        status, decision = "partial", "needs_review"
    else:
        status = "completed"
        decision = "fail" if (failed or safety_hits) else "pass"

    score = round(weighted_pass / weighted_total, 4) if weighted_total else None

    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO eval_runs (id, project_id, suite_id, dataset_version_id,"
            " evaluator_version_id, release_ref, status, decision, total_cases,"
            " executed_cases, passed_cases, failed_cases, skipped_cases, safety_violations,"
            " score, correlation_id, started_at, finished_at, created_at,"
            " output_source, runtime_execution_id, output_digest)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, project_id, dataset["suite_id"], version_id, evaluator_version_id,
             payload.get("release_ref"), status, decision, total, executed, passed,
             failed, skipped, safety_hits, score, correlation_id, now, now, now,
             output_source, runtime_execution_id or None,
             _output_digest(outputs)),
        )
        for row in rows:
            conn.execute(
                "INSERT INTO eval_results (id, run_id, case_id, case_key, status, score,"
                " detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row,
            )
        events.emit(
            conn, "evaluation.run.completed", project_id=project_id,
            resource_type="evaluation_run", resource_id=run_id,
            payload={"status": status, "decision": decision, "total": total,
                     "executed": executed, "passed": passed, "failed": failed,
                     "skipped": skipped, "safety_violations": safety_hits},
            correlation_id=correlation_id,
        )
    return get_run(conn, run_id)


def get_run(conn: sqlite3.Connection, run_id: str, *, with_results: bool = True) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise EvaluationError("not_found", 404, "评估运行不存在")
    run = dict(row)
    if with_results:
        run["results"] = [
            dict(item)
            for item in conn.execute(
                "SELECT * FROM eval_results WHERE run_id = ? ORDER BY case_key", (run_id,)
            )
        ]
    return run


def list_runs(conn: sqlite3.Connection, project_id: str, limit: int = 50) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM eval_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
        (project_id, max(1, min(200, limit))),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}
