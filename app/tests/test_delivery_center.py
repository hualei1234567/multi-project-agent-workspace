"""交付中心：提交 → 校验 → 验收 → 返工 的显式流程测试。

覆盖点：
* 校验三态（pass / fail / unverified）都由事实推导，绝不把「无法核对」当成功
* 验收前必跑校验，硬失败项拦住验收
* 返工只能从 rejected 出发且必须说明修改内容
* 流转历史来自 audit_logs，不新建平行历史表
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import service, sources


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_real_ccusage(monkeypatch):
    monkeypatch.setattr(sources, "ccusage_session_costs", list)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "platform.db"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "ai-team" / "TASK_BOARD.md", "# Board\n\n| Task ID | 标题 |\n|---|---|\n")
    _write(repo / "ai-team" / "reports" / "TEAM-900-qa.md", "# QA\n")
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(db_path))
    config_file = tmp_path / "projects.json"
    config_file.write_text(json.dumps({"projects": []}), encoding="utf-8")
    monkeypatch.setenv("AGWS_PLATFORM_CONFIG", str(config_file))
    monkeypatch.setenv("AGWS_PLATFORM_PROJECTS_ROOT", str(projects_root))
    monkeypatch.setenv("AGWS_PLATFORM_REPO", str(repo))
    service.bootstrap(repo_root=repo)
    return SimpleNamespace(tmp=tmp_path, db=db_path, repo=repo)


def _delivery(env, **payload):
    return service.create_delivery("default", payload)["delivery"]


def _ready_task():
    return service.create_task("default", {"task_key": "TEAM-900", "title": "交付任务"})["task"]


def test_validation_reports_evidence_not_guesses(env):
    task = _ready_task()
    for target in ("in_progress", "review"):
        current = service.get_task("default", task["id"])["task"]
        service.transition_task("default", task["id"], {
            "to_status": target, "expected_version": current["version"],
        })
    artifact = env.repo / "docs" / "out.txt"
    _write(artifact, "hello")
    digest = hashlib.sha256(b"hello").hexdigest()

    good = _delivery(env, title="有证据", kind="document", path="docs/out.txt",
                     sha=digest, summary="改了 1 个文件", task_id=task["id"])
    result = service.validate_delivery("default", good["id"])
    assert result["validation"]["state"] == "pass"
    assert {c["key"]: c["state"] for c in result["validation"]["checks"]}["sha"] == "pass"
    assert result["stage"] == "validated"

    wrong_sha = _delivery(env, title="指纹不符", kind="document", path="docs/out.txt",
                          sha="0" * 64, summary="x")
    assert service.validate_delivery("default", wrong_sha["id"])["validation"]["state"] == "fail"

    missing = _delivery(env, title="文件不存在", kind="document", path="docs/nope.txt",
                        summary="x")
    checked = service.validate_delivery("default", missing["id"])["validation"]
    assert checked["state"] == "fail"
    assert {c["key"]: c["state"] for c in checked["checks"]}["artifact"] == "fail"

    bare = _delivery(env, title="没有证据", kind="other")
    checked = service.validate_delivery("default", bare["id"])["validation"]
    assert checked["state"] == "unverified"
    # 没有证据 ≠ 失败，也绝不能被当成通过
    assert {c["key"]: c["state"] for c in checked["checks"]}["artifact"] == "unverified"


def test_validate_persists_snapshot_and_audit(env):
    delivery = _delivery(env, title="待校验", kind="other")
    service.validate_delivery("default", delivery["id"])
    detail = service.get_delivery("default", delivery["id"])
    assert detail["delivery"]["validation_state"] == "unverified"
    assert detail["delivery"]["validated_at"]
    assert detail["validation"]["state"] == "unverified"
    actions = [e["action"] for e in detail["history"]]
    assert actions == ["delivery.submit", "delivery.validate"]


def test_accept_is_blocked_by_hard_validation_failure(env):
    delivery = _delivery(env, title="产物丢了", kind="document", path="docs/gone.md", summary="x")
    with pytest.raises(service.PlatformError) as err:
        service.review_delivery("default", delivery["id"], {"decision": "accepted"})
    assert err.value.code == "validation_failed" and err.value.status == 409
    # 退回不被拦：退回本来就是因为成果不合格
    rejected = service.review_delivery("default", delivery["id"], {
        "decision": "rejected", "comment": "产物不存在",
    })["delivery"]
    assert rejected["status"] == "rejected"


def test_resubmit_only_from_rejected_and_requires_note(env):
    delivery = _delivery(env, title="第一版", kind="other")
    with pytest.raises(service.PlatformError) as not_rejected:
        service.resubmit_delivery("default", delivery["id"], {"note": "补了测试"})
    assert not_rejected.value.code == "invalid_state" and not_rejected.value.status == 409

    service.review_delivery("default", delivery["id"], {"decision": "rejected", "comment": "缺测试"})
    with pytest.raises(service.PlatformError) as no_note:
        service.resubmit_delivery("default", delivery["id"], {})
    assert no_note.value.code == "invalid_payload" and no_note.value.status == 422

    artifact = env.repo / "docs" / "v2.txt"
    _write(artifact, "v2")
    again = service.resubmit_delivery("default", delivery["id"], {
        "note": "补了测试", "path": "docs/v2.txt", "summary": "第二版",
    })["delivery"]
    assert again["status"] == "submitted"
    assert again["resubmit_count"] == 1
    assert again["rejection_reason"] is None
    assert again["validation_state"] is None

    detail = service.get_delivery("default", delivery["id"])
    actions = [e["action"] for e in detail["history"]]
    assert actions == ["delivery.submit", "delivery.review", "delivery.resubmit"]
    resubmit_event = detail["history"][-1]
    assert resubmit_event["detail"]["previous_reason"] == "缺测试"


def test_pipeline_counts_follow_stages(env):
    a = _delivery(env, title="A", kind="other")
    b = _delivery(env, title="B", kind="other")
    c = _delivery(env, title="C", kind="other")
    service.validate_delivery("default", b["id"])
    service.review_delivery("default", a["id"], {"decision": "rejected", "comment": "返工"})
    service.review_delivery("default", c["id"], {"decision": "accepted"})

    pipeline = service.list_deliveries("default", page_size=100)["pipeline"]
    counts = {stage["key"]: stage["count"] for stage in pipeline["stages"]}
    assert counts == {"submitted": 0, "validated": 1, "accepted": 1, "rejected": 1}
    assert pipeline["reviewable"] == 3

    # 环节筛选走 SQL：分页前就过滤，计数与实际条目一致
    assert [i["id"] for i in service.list_deliveries("default", stage="rejected")["items"]] == [a["id"]]
    assert [i["id"] for i in service.list_deliveries("default", stage="validated")["items"]] == [b["id"]]
    assert [i["id"] for i in service.list_deliveries("default", stage="accepted")["items"]] == [c["id"]]
    with pytest.raises(service.PlatformError) as bad_stage:
        service.list_deliveries("default", stage="whatever")
    assert bad_stage.value.code == "invalid_payload"


def test_mirror_delivery_is_not_validatable(env):
    service.backfill_project("default")
    items = service.list_deliveries("default", page_size=100)["items"]
    mirror = next(item for item in items if item["readonly"])
    with pytest.raises(service.PlatformError) as err:
        service.validate_delivery("default", mirror["id"])
    assert err.value.code == "readonly_resource" and err.value.status == 409
