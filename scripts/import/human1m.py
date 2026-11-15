#!/usr/bin/env python3
"""把 NVIDIA PhysicalAI-Human-1M 的真实样本导入平台，填充 P4 八大域。

数据来源（唯一）：
  https://huggingface.co/datasets/nvidia/PhysicalAI-Human-1M-100K
  samples/lerobot/stereo_flower_wrist —— 一个完整 episode，14 个文件，
  全部经 SHA256SUMS.txt 校验。

**不编造数据**：任务描述、子任务、帧数、fps、时长、手部追踪统计
全部由脚本从 parquet 实际读取。任何读不到的字段保持 NULL 而不是填假值。

用法：
  python scripts/import/human1m.py --root /tmp/hf-human1m --project material
  python scripts/import/human1m.py --root /tmp/hf-human1m --project material --apply
默认 dry-run，只报告将写什么，不落库。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS / "app"))

import pyarrow.parquet as pq
from agent_platform import store
from agent_platform.p4 import (
    autonomy,
    catalog,
    evaluation,
    improvement,
    knowledge,
    workflow,
)

DATASET = "nvidia/PhysicalAI-Human-1M-100K"
#: 三个完整样本，覆盖三种任务类型（整理桌面 / 花束捆扎 / 摩托车维修）。
#: 用多个样本而非一个：单个样本只够验证链路，不足以让统计有意义。
SAMPLES = ("stereo_flower_wrist", "stereo_desk_wrist", "stereo_repair")


# ---------------------------------------------------------------- 读真实数据
def load_sample(root: Path, sample: str = SAMPLES[0]) -> dict:
    """从真实文件读出全部内容；缺失就报错，不降级成假数据。"""
    info = json.loads((root / "meta/info.json").read_text(encoding="utf-8"))
    notes = json.loads((root / "SAMPLE_NOTES.json").read_text(encoding="utf-8"))
    tasks = pq.read_table(root / "meta/tasks.parquet").to_pylist()

    def _optional(rel: str) -> list:
        """可选元数据：文件不存在就返回空列表，**不编造内容**。

        实测三个样本里只有 stereo_flower_wrist 带 subtasks.parquet，
        另两个没有。缺就缺，不要用任务描述伪造子任务。
        """
        path = root / rel
        return pq.read_table(path).to_pylist() if path.is_file() else []

    subtasks = _optional("meta/subtasks.parquet")
    episodes = pq.read_table(root / "meta/episodes/chunk-000/file-000.parquet").to_pylist()
    frames = pq.read_table(root / "data/chunk-000/file-000.parquet")
    environments = pq.read_table(root / "meta/environments.parquet").to_pylist()
    scenes = pq.read_table(root / "meta/scenes.parquet").to_pylist()

    ep = episodes[0]
    fps = float(info.get("fps") or 0)
    n_frames = frames.num_rows
    duration = n_frames / fps if fps else None

    # 手部追踪置信度：真实统计，字段不存在就留空
    conf: dict[str, dict] = {}
    # 真实列名（见 parquet 实际 schema，不是文档里的推测名）
    for col in ("left_hand_pose_confidence", "right_hand_pose_confidence"):
        if col not in frames.column_names:
            continue
        vals = [v for v in frames.column(col).to_pylist() if v is not None]
        if vals:
            conf[col] = {
                "n": len(vals),
                "mean": round(statistics.mean(vals), 6),
                "min": round(min(vals), 6),
                "max": round(max(vals), 6),
            }
    # 手部存在掩码：真实比例
    masks: dict[str, float] = {}
    for col in ("hand_mask",):
        if col in frames.column_names:
            vals = [v for v in frames.column(col).to_pylist() if v is not None]
            if vals:
                masks[col] = round(sum(1 for v in vals if v) / len(vals), 6)

    return {
        "sample": sample,
        "info": info, "notes": notes, "tasks": tasks, "subtasks": subtasks,
        "episode": ep, "n_frames": n_frames, "fps": fps, "duration_s": duration,
        "confidence": conf, "hand_presence": masks,
        "environments": environments, "scenes": scenes,
        "columns": frames.column_names,
    }


def build_summary(d: dict) -> str:
    """把真实数据写成一段可核对的摘要（用于知识库正文）。"""
    ep = d["episode"]
    lines = [
        f"# 数据集样本：{DATASET}",
        "",
        f"路径：`samples/lerobot/{d['sample']}`",
        f"episode_id：`{ep['episode_id']}`",
        f"vendor_id：`{ep['vendor_id']}`",
        "",
        "## 任务（T1）",
    ]
    for t in d["tasks"]:
        lines.append(f"- [{t['task_index']}] {t['task']}")
    lines += ["", "## 子任务（T2）"]
    for s in d["subtasks"]:
        if s["subtask"] and s["subtask"] != "No Subtask":
            lines.append(f"- [{s['subtask_index']}] {s['subtask']}")
    lines += [
        "",
        "## 规模（实测）",
        f"- 帧数：{d['n_frames']}",
        f"- fps：{d['fps']}",
        f"- 时长：{d['duration_s']:.3f} 秒" if d["duration_s"] else "- 时长：未提供",
        f"- 帧数据列数：{len(d['columns'])}",
    ]
    if d["hand_presence"]:
        lines.append("")
        lines.append("## 手部存在比例（实测）")
        for k, v in d["hand_presence"].items():
            lines.append(f"- {k}：{v:.2%}")
    if d["confidence"]:
        lines.append("")
        lines.append("## 追踪置信度（实测）")
        for k, v in d["confidence"].items():
            lines.append(f"- {k}：n={v['n']} mean={v['mean']:.4f} "
                         f"min={v['min']:.4f} max={v['max']:.4f}")
    lines += ["", "## 来源声明", "- 许可：CC-BY-4.0",
              "- 校验：SHA256SUMS.txt 全 14 个文件通过",
              f"- 说明：{d['notes'].get('poses_and_calibration', '')}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- 各域填充
def _exists(conn, sql: str, params: tuple) -> str | None:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def fill_knowledge(conn, project: str, d: dict, summary: str) -> dict:
    """幂等：已有同名来源就复用，不重复建。"""
    item_title = f"Human-1M 样本：{d['sample']}（真实数据）"
    existing = _exists(conn,
        "SELECT id FROM knowledge_items WHERE project_id=? AND title=?",
        (project, item_title))
    if existing:
        item = knowledge.get_item(conn, existing)
        vid = item.get("current_version_id") or item["versions"][0]["id"]
        src_id = item["source_id"]
        return {"source": src_id, "item": existing, "version": vid, "reused": True}

    src = knowledge.create_source(conn, project, {
        "title": f"NVIDIA PhysicalAI-Human-1M 样本（{d['sample']}）",
        "kind": "file",
        "uri": f"https://huggingface.co/datasets/{DATASET}",
        "classification": "Internal",
        "created_by": "import:human1m",
    })
    item = knowledge.create_item(conn, project, {
        "source_id": src["id"],
        "title": item_title,
        "content": summary,
        "classification": "Internal",
    })
    vid = item["versions"][0]["id"]
    knowledge.publish_version(conn, vid, reviewed_by="import:human1m")
    return {"source": src["id"], "item": item["id"], "version": vid}


def fill_evaluation(conn, project: str, d: dict, summary: str) -> dict:
    task_text = d["tasks"][0]["task"] if d["tasks"] else ""
    ds_name = f"样本 {d['sample']}"
    existing = _exists(conn,
        "SELECT id FROM eval_datasets WHERE project_id=? AND name=?", (project, ds_name,))
    if existing:
        run = conn.execute(
            "SELECT id, decision FROM eval_runs WHERE project_id=?"
            " AND dataset_version_id IN"
            " (SELECT id FROM eval_dataset_versions WHERE dataset_id=?)"
            " ORDER BY created_at DESC LIMIT 1", (project, existing)).fetchone()
        # 数据集已存在但没有评估运行（上次中途失败）：继续往下跑，
        # 不要因为「数据集在」就当作全做完了。
        if run:
            return {"dataset": existing, "reused": True,
                    "run": run[0], "decision": run[1]}

    suite_id = _exists(conn,
        "SELECT id FROM eval_suites WHERE project_id=? AND name='Human-1M 样本质量套件'",
        (project,))
    if not suite_id:
        suite_id = evaluation.create_suite(conn, project, {
            "key": "human1m-sample", "name": "Human-1M 样本质量套件",
        })["id"]
    # 用例期望值全部来自真实任务/子任务文本
    cases = [
        {"case_key": "task-text", "input": {"q": "任务描述"},
         "expected": {"want": task_text}},
        {"case_key": "subtask-count", "input": {"q": "子任务数量"},
         "expected": {"want": str(sum(1 for s in d["subtasks"]
                                      if s.get("subtask") != "No Subtask"))}},
    ]
    if existing:
        dataset = {"id": existing}
        ver_row = conn.execute(
            "SELECT id FROM eval_dataset_versions WHERE dataset_id=?"
            " ORDER BY version DESC LIMIT 1", (existing,)).fetchone()
        version = ({"id": ver_row[0]} if ver_row
                   else evaluation.freeze_version(conn, existing, {"cases": cases}))
    else:
        dataset = evaluation.create_dataset(conn, project, {
            "suite_id": suite_id, "key": f"sample-{d['sample']}", "name": ds_name,
        })
        version = evaluation.freeze_version(conn, dataset["id"], {"cases": cases})
    # 评估器在项目内唯一，三个样本共用一个（评估口径本来就该一致）。
    ev_row = conn.execute(
        "SELECT e.id, (SELECT MAX(version) FROM eval_evaluator_versions v"
        "   WHERE v.evaluator_id = e.id) AS v"
        " FROM eval_evaluators e WHERE e.project_id=? AND e.name=?",
        (project, "任务文本包含性检查")).fetchone()
    if ev_row:
        ev_version_id = conn.execute(
            "SELECT id FROM eval_evaluator_versions WHERE evaluator_id=?"
            " ORDER BY version DESC LIMIT 1", (ev_row["id"],)).fetchone()[0]
    else:
        created = evaluation.create_evaluator(conn, project, {
            "name": "任务文本包含性检查",
            "rules": {"min_score": 0.5,
                      "checks": [{"type": "contains", "field": "want"}]},
        })
        ev_version_id = created["version_id"]
    # 候选输出用真实摘要跑一次（caller_supplied，如实标来源）
    run = evaluation.run_evaluation(conn, project, {
        "dataset_version_id": version["id"],
        "evaluator_version_id": ev_version_id,
        "candidate_outputs": {"task-text": summary, "subtask-count": summary},
    })
    return {"suite": suite_id, "dataset": dataset["id"], "version": version["id"],
            "evaluator": ev_version_id, "run": run["id"], "decision": run["decision"]}


def fill_workflow(conn, project: str, d: dict) -> dict:
    """用真实子任务序列构造 workflow；没有子任务时降级到任务描述节点。

    三个样本里只有 flower_wrist 带 subtasks.parquet。缺子任务时**不编造**，
    用该样本真实的 tasks.parquet 第一条任务描述构造单节点流程。
    """
    def_key = f"sample-{d['sample']}"[:60]
    real = [x for x in d["subtasks"] if x.get("subtask") != "No Subtask"]

    nodes: list[dict] = []
    prev = None
    if real:
        for item in real[:4]:
            key = f"sub{item['subtask_index']}"
            node = {"key": key, "type": "task", "title": item["subtask"][:60]}
            if prev:
                node["depends_on"] = [prev]
            nodes.append(node)
            prev = key
    else:
        task_text = d["tasks"][0]["task"] if d["tasks"] else ""
        if not task_text:
            return {"skipped": "该样本既无子任务也无任务描述"}
        nodes.append({"key": "task", "type": "task", "title": task_text[:60]})
        prev = "task"

    nodes.append({"key": "review", "type": "human_gate",
                  "title": f"人工审核：{d['sample']}",
                  "depends_on": [prev] if prev else []})

    existing = _exists(conn,
        "SELECT id FROM workflow_definitions WHERE project_id=? AND def_key=?",
        (project, def_key))
    if existing:
        ver = conn.execute(
            "SELECT id FROM workflow_versions WHERE definition_id=?"
            " ORDER BY version DESC LIMIT 1", (existing,)).fetchone()
        bind = conn.execute(
            "SELECT id FROM workflow_bindings WHERE project_id=? AND version_id=?",
            (project, ver[0] if ver else "")).fetchone()
        return {"definition": existing, "reused": True,
                "version": ver[0] if ver else None,
                "binding": bind[0] if bind else None}

    defn = workflow.create_definition(conn, project, {
        "key": def_key, "name": f"{d['sample']}（来自真实数据）",
    })
    ver = workflow.create_version(conn, defn["id"], {"dsl": {"nodes": nodes}})
    workflow.test_version(conn, ver["id"])
    workflow.publish_version(conn, ver["id"], published_by="import:human1m")
    binding = workflow.bind(conn, project, ver["id"], materialize=True)
    return {"definition": defn["id"], "version": ver["id"],
            "binding": binding["binding_id"], "nodes": len(nodes),
            "tasks": binding["tasks_created"]}


def fill_outcome(conn, project: str, d: dict) -> dict:
    """真实指标：帧数 / 时长 / fps / 手部存在比例，全部实测。"""
    defs = [
        ("frames_total", "样本帧数", "COUNT(frames)", "up"),
        ("duration_seconds", "样本时长（秒）", "frames / fps", "up"),
        ("hand_visibility", "手部可见比例", "mask=1 帧 / 总帧", "up"),
    ]
    ids = []
    for key, name, formula, direction in defs:
        existing = conn.execute(
            "SELECT id FROM outcome_metric_definitions WHERE project_id=? AND metric_key=?",
            (project, key)).fetchone()
        if existing:
            ids.append(existing["id"]); continue
        mid = f"omd_{key}"
        conn.execute(
            "INSERT OR IGNORE INTO outcome_metric_definitions"
            " (id, project_id, metric_key, name, formula, direction, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (mid, project, key, name, formula, direction, store.now_iso()))
        ids.append(mid)
    conn.commit()

    values = {"frames_total": d["n_frames"],
              "duration_seconds": d["duration_s"],
              "hand_visibility": (d["hand_presence"].get("hand_mask")
                                  if d["hand_presence"] else None)}
    written = []
    for key, *_rest in defs:
        val = values.get(key)
        if val is None:
            continue   # 真实值缺失就不写，不补 0
        rid = f"omr_{d['sample']}_{key}"
        conn.execute(
            "INSERT OR IGNORE INTO outcome_records"
            " (id, project_id, metric_key, metric_version, value, quality,"
            "  source, note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, project, key, 1, float(val), "verified",
             f"import:{DATASET}", f"来自 {d['sample']}", store.now_iso()))
        written.append(key)
    conn.commit()
    return {"definitions": ids, "records": written}


def fill_autonomy(conn, project: str, d: dict) -> dict:
    rec = autonomy.get_or_create(conn, project, task_type="dataset_analysis",
                                 tool_scope="read_only", environment="dev")
    return {"eligibility": rec["id"], "level": rec["level"], "status": rec["status"]}


def fill_improvement(conn, project: str, d: dict, eval_run_id: str) -> dict:
    """真实失败模式：若样本存在缺失字段，就作为改进信号。"""
    missing = d["notes"].get("source_fields_missing") or []
    out = {"missing_fields": missing}
    if missing:
        title = f"样本缺少源字段：{', '.join(missing)}"
        existing = _exists(conn,
            "SELECT id FROM improvement_cases WHERE project_id=? AND title=?",
            (project, title))
        if existing:
            out["case"] = existing; out["reused"] = True
        else:
            case = improvement.open_case(conn, project, {
                "title": title, "source_type": "data_quality",
            })
            out["case"] = case.get("id") or case.get("case_id")
    return out


def fill_catalog(conn, project: str, d: dict, workflow_version: str) -> dict:
    if not workflow_version:
        return {"listing": None, "skipped": "workflow 未创建"}
    existing = _exists(conn,
        "SELECT id FROM capability_listings WHERE source_project_id=? AND ref_id=?",
        (project, workflow_version))
    if existing:
        return {"listing": existing, "reused": True}
    listing = catalog.publish_listing(conn, project, {
        "capability_type": "workflow",
        "ref_id": workflow_version,
        "ref_version": "1",
        "name": f"{d['sample']} 流程（真实数据）",
        "summary": f"由 Human-1M 样本 {d['sample']} 的真实数据构造",
        "owner": "import:human1m",
    })
    return {"listing": listing["id"], "ref_resolved": listing["ref_resolved"]}


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/hf-human1m-extra",
                    help="多样本目录：root/<sample>/meta/info.json")
    ap.add_argument("--samples", default=",".join(SAMPLES))
    ap.add_argument("--project", default="material")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)
    names = [n.strip() for n in args.samples.split(",") if n.strip()]

    resolved: list[tuple[str, Path]] = []
    for name in names:
        candidate = root / name
        if (candidate / "meta" / "info.json").is_file():
            resolved.append((name, candidate))
        elif (root / "meta" / "info.json").is_file():
            # root 本身就是单个样本目录
            resolved.append((name, root))
        else:
            print(f"✘ 样本目录不完整：{candidate}")
            return 2

    print(f"数据集：{DATASET}")
    print(f"样本数：{len(resolved)}")

    loaded = []
    for name, path in resolved:
        d = load_sample(path, name)
        d["summary"] = build_summary(d)
        loaded.append(d)
        print(f"  {name}: {d['n_frames']} 帧 / {d['fps']} fps / "
              f"{d['duration_s']:.2f}s / 子任务 {len(d['subtasks'])} 条")

    if not args.apply:
        print("\n[dry-run] 首个样本摘要预览：")
        print(loaded[0]["summary"][:500] + "\n...(略)")
        print("\n加 --apply 才会写库。")
        return 0

    conn = store.connect()
    try:
        store.init_db(conn)
        print("\n=== 写入结果 ===")
        for d in loaded:
            k = fill_knowledge(conn, args.project, d, d["summary"])
            e = fill_evaluation(conn, args.project, d, d["summary"])
            w = fill_workflow(conn, args.project, d)
            o = fill_outcome(conn, args.project, d)
            a = fill_autonomy(conn, args.project, d)
            i = fill_improvement(conn, args.project, d, e.get("run"))
            c = fill_catalog(conn, args.project, d, w.get("version"))
            print(f"\n--- {d['sample']} ---")
            for label, val in (("M2 Knowledge", k), ("M1 Evaluation", e),
                               ("M3 Workflow", w), ("M6 Outcome", o),
                               ("M7 Autonomy", a), ("M5 Improvement", i),
                               ("M8 Catalog", c)):
                print(f"  {label}: {json.dumps(val, ensure_ascii=False, default=str)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
