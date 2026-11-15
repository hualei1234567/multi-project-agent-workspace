-- 多项目 Agent 管理平台 · 独立 SQLite schema
-- 与业务库/迁移链完全独立。
-- 所有语句幂等：重复启动只补缺失对象。老库的新增列由 migrate.py 用
-- PRAGMA table_info + ALTER TABLE 补齐，这里只描述 v2 的完整形态。

CREATE TABLE IF NOT EXISTS agent_projects (
  id                    TEXT PRIMARY KEY,
  code                  TEXT NOT NULL UNIQUE,
  name                  TEXT NOT NULL,
  description           TEXT,
  status                TEXT NOT NULL DEFAULT 'active',
  repo_root             TEXT NOT NULL,
  session_slugs         TEXT NOT NULL DEFAULT '[]',
  team_template_key     TEXT NOT NULL DEFAULT 'standard-1-6',
  team_template_version INTEGER NOT NULL DEFAULT 1,
  settings_json         TEXT NOT NULL DEFAULT '{}',
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  owner                 TEXT,
  archived_at           TEXT,
  version               INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS agent_team_templates (
  id          TEXT PRIMARY KEY,
  template_key TEXT NOT NULL,
  version     INTEGER NOT NULL,
  name        TEXT NOT NULL,
  description TEXT,
  roles_json  TEXT NOT NULL,
  is_system   INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  UNIQUE(template_key, version)
);

CREATE TABLE IF NOT EXISTS project_agents (
  id               TEXT PRIMARY KEY,
  project_id       TEXT NOT NULL REFERENCES agent_projects(id),
  role_key         TEXT NOT NULL,
  display_name     TEXT NOT NULL,
  instance_key     TEXT NOT NULL,
  agent_type       TEXT NOT NULL,
  execution_mode   TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'offline',
  current_task_id  TEXT,
  parent_agent_id  TEXT,
  current_branch   TEXT,
  worktree_path    TEXT,
  last_heartbeat_at TEXT,
  metadata_json    TEXT NOT NULL DEFAULT '{}',
  role_snapshot_json TEXT NOT NULL DEFAULT '{}',
  model_id         TEXT,
  last_seen_at     TEXT,
  UNIQUE(project_id, instance_key)
);

CREATE TABLE IF NOT EXISTS tasks (
  id         TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES agent_projects(id),
  task_key   TEXT NOT NULL,
  title      TEXT NOT NULL,
  status     TEXT NOT NULL,
  priority   TEXT NOT NULL DEFAULT 'P2',
  owner      TEXT,
  qa         TEXT,
  source     TEXT NOT NULL DEFAULT 'board',
  updated_at TEXT NOT NULL,
  description TEXT,
  assignee_agent_id TEXT,
  created_by TEXT,
  acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
  blocked_reason TEXT,
  branch_name TEXT,
  started_at TEXT,
  completed_at TEXT,
  version    INTEGER NOT NULL DEFAULT 1,
  UNIQUE(project_id, task_key)
);

CREATE TABLE IF NOT EXISTS deliveries (
  id         TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES agent_projects(id),
  task_id    TEXT,
  title      TEXT NOT NULL,
  kind       TEXT NOT NULL,
  path       TEXT,
  sha        TEXT,
  created_at TEXT NOT NULL,
  summary    TEXT,
  status     TEXT NOT NULL DEFAULT 'submitted',
  reviewer   TEXT,
  reviewed_at TEXT,
  rejection_reason TEXT,
  updated_at TEXT,
  source     TEXT NOT NULL DEFAULT 'platform',
  -- v14 交付中心：校验结果与返工次数（校验只存证据快照，不复制交付事实）
  validation_state TEXT,
  validated_at     TEXT,
  validation_json  TEXT NOT NULL DEFAULT '{}',
  resubmit_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agent_events (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES agent_projects(id),
  agent_id    TEXT,
  session_id  TEXT,
  event_type  TEXT NOT NULL,
  message     TEXT,
  tool        TEXT,
  ok          INTEGER,
  reported_state TEXT,
  occurred_at TEXT NOT NULL,
  UNIQUE(session_id, agent_id, occurred_at, event_type, tool)
);

CREATE TABLE IF NOT EXISTS token_usage_events (
  id                 TEXT PRIMARY KEY,
  project_id         TEXT NOT NULL REFERENCES agent_projects(id),
  agent_id           TEXT,
  session_id         TEXT,
  message_uuid       TEXT,
  model              TEXT,
  input_tokens       INTEGER NOT NULL DEFAULT 0,
  output_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens       INTEGER NOT NULL DEFAULT 0,
  source             TEXT NOT NULL,
  occurred_at        TEXT NOT NULL,
  -- cost_usd 是历史账面列（来源不明），保留兼容；
  -- R2 / D-2 起一律以 actual/estimated + cost_quality 为准。
  cost_usd           REAL,
  price_version      TEXT,
  cost_source        TEXT,
  actual_cost_usd    REAL,
  estimated_cost_usd REAL,
  cost_quality       TEXT,
  UNIQUE(session_id, message_uuid, agent_id, model)
);

-- 任务依赖（DAG）
CREATE TABLE IF NOT EXISTS task_dependencies (
  id                 TEXT PRIMARY KEY,
  project_id         TEXT NOT NULL REFERENCES agent_projects(id),
  task_id            TEXT NOT NULL REFERENCES tasks(id),
  depends_on_task_id TEXT NOT NULL REFERENCES tasks(id),
  dependency_type    TEXT NOT NULL DEFAULT 'blocks',
  -- v3（M3-2）：跨项目依赖标记。0=同项目，1=跨项目。
  cross_project      INTEGER NOT NULL DEFAULT 0,
  created_at         TEXT NOT NULL,
  CHECK (task_id <> depends_on_task_id),
  UNIQUE (task_id, depends_on_task_id)
);

-- 执行记录（权威来源：session jsonl）
CREATE TABLE IF NOT EXISTS executions (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES agent_projects(id),
  agent_id      TEXT,
  task_id       TEXT,
  session_id    TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'unknown',
  started_at    TEXT,
  finished_at   TEXT,
  duration_ms   INTEGER,
  summary       TEXT,
  error_summary TEXT,
  model_id      TEXT,
  total_tokens  INTEGER NOT NULL DEFAULT 0,
  source        TEXT NOT NULL DEFAULT 'transcript',
  correlation_id TEXT,
  -- R2 / D-1：这条执行与 Task 的关联是否可信。
  -- linked / dispatcher_missing_task / legacy_unlinked / unknown
  link_quality      TEXT,
  first_seen_source TEXT,
  -- R2 / D-2：执行粒度成本。actual 来自 provider 账单（CLI 的 total_cost_usd），
  -- estimated 由价格版本算出；两者皆空即 UNKNOWN，不补 0。
  actual_cost_usd    REAL,
  estimated_cost_usd REAL,
  cost_quality       TEXT,
  price_version      TEXT,
  UNIQUE (project_id, session_id, agent_id)
);

-- 文档索引（仓库文件只读，不复制内容）
CREATE TABLE IF NOT EXISTS project_documents (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES agent_projects(id),
  title         TEXT NOT NULL,
  document_type TEXT NOT NULL DEFAULT 'other',
  uri           TEXT NOT NULL,
  owner_agent_id TEXT,
  updated_at    TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE (project_id, uri)
);

-- 本机偏好（单用户，二期不接 RBAC）
CREATE TABLE IF NOT EXISTS project_favorites (
  user_key   TEXT NOT NULL DEFAULT 'local',
  project_id TEXT NOT NULL REFERENCES agent_projects(id),
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_key, project_id)
);

CREATE TABLE IF NOT EXISTS project_recent_visits (
  user_key   TEXT NOT NULL DEFAULT 'local',
  project_id TEXT NOT NULL REFERENCES agent_projects(id),
  visited_at TEXT NOT NULL,
  PRIMARY KEY (user_key, project_id)
);

-- 审计（只追加）
CREATE TABLE IF NOT EXISTS audit_logs (
  id            TEXT PRIMARY KEY,
  project_id    TEXT,
  actor         TEXT NOT NULL DEFAULT 'local',
  action        TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id   TEXT,
  result        TEXT NOT NULL DEFAULT 'ok',
  detail_json   TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL,
  correlation_id    TEXT
);

-- POST 幂等键（内部实现，契约见 docs/113 §1）
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key          TEXT PRIMARY KEY,
  fingerprint  TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_project_status
  ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_events_project_time
  ON agent_events(project_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_token_usage_project_time
  ON token_usage_events(project_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_deliveries_project_time
  ON deliveries(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_project_agents_project_status
  ON project_agents(project_id, status);
CREATE INDEX IF NOT EXISTS idx_executions_project_time ON executions(project_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_project_time ON audit_logs(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_project ON project_documents(project_id);
CREATE INDEX IF NOT EXISTS idx_task_deps_task ON task_dependencies(task_id);

-- ===========================================================================
-- schema v3（三期 M3-1~M3-3）：自主调度 / 跨项目协作 / 运行时控制
-- 全部 IF NOT EXISTS，v2 库可原地升级；v2 既有表语义不变。
-- ===========================================================================

-- 调度批次：一次「决定谁做什么」的快照
CREATE TABLE IF NOT EXISTS dispatch_plans (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES agent_projects(id),
  status      TEXT NOT NULL DEFAULT 'draft',   -- draft/approved/running/done/aborted
  strategy    TEXT NOT NULL DEFAULT 'manual',  -- manual/priority/dependency
  created_by  TEXT,
  notes       TEXT,
  created_at  TEXT NOT NULL,
  approved_at TEXT,
  finished_at TEXT,
  version     INTEGER NOT NULL DEFAULT 1
);

-- 调度项：最小执行单元（任务 → 角色）
CREATE TABLE IF NOT EXISTS dispatch_items (
  id               TEXT PRIMARY KEY,
  plan_id          TEXT NOT NULL REFERENCES dispatch_plans(id),
  project_id       TEXT NOT NULL REFERENCES agent_projects(id),
  task_id          TEXT,
  agent_id         TEXT,
  role_key         TEXT,
  status           TEXT NOT NULL DEFAULT 'queued', -- queued/leased/running/succeeded/failed/cancelled/skipped
  priority         TEXT NOT NULL DEFAULT 'P2',
  attempt          INTEGER NOT NULL DEFAULT 0,
  max_attempts     INTEGER NOT NULL DEFAULT 2,
  lease_owner      TEXT,
  lease_expires_at TEXT,
  session_id       TEXT,
  started_at       TEXT,
  finished_at      TEXT,
  exit_code        INTEGER,
  error_code       TEXT,
  error_summary    TEXT,
  result_json      TEXT NOT NULL DEFAULT '{}',
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  correlation_id  TEXT
);

-- 同一任务在同一时刻只允许一个活跃调度项（防双跑）
CREATE UNIQUE INDEX IF NOT EXISTS uq_dispatch_items_active_task
  ON dispatch_items(project_id, task_id)
  WHERE status IN ('leased', 'running') AND task_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dispatch_items_plan
  ON dispatch_items(plan_id, status);

-- 执行器日志：进程 stdout/stderr 摘要（写入时截断）
CREATE TABLE IF NOT EXISTS dispatch_logs (
  id         TEXT PRIMARY KEY,
  item_id    TEXT NOT NULL REFERENCES dispatch_items(id),
  stream     TEXT NOT NULL DEFAULT 'stdout',  -- stdout/stderr/system
  seq        INTEGER NOT NULL DEFAULT 0,
  content    TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatch_logs_item ON dispatch_logs(item_id, seq);

-- 调度事件：状态机流转留痕
CREATE TABLE IF NOT EXISTS dispatch_events (
  id          TEXT PRIMARY KEY,
  item_id     TEXT NOT NULL REFERENCES dispatch_items(id),
  event_type  TEXT NOT NULL,
  from_status TEXT,
  to_status   TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatch_events_item ON dispatch_events(item_id, created_at);

-- 共享资源登记（M3-2）：同一时刻只能一个项目独占的资源
CREATE TABLE IF NOT EXISTS shared_resources (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL UNIQUE,
  kind             TEXT NOT NULL,   -- gpu/device/port/service/host
  owner_project_id TEXT REFERENCES agent_projects(id),
  capacity         INTEGER NOT NULL DEFAULT 1,
  exclusive        INTEGER NOT NULL DEFAULT 1,
  metadata_json    TEXT NOT NULL DEFAULT '{}',
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

-- 资源占用申请
CREATE TABLE IF NOT EXISTS resource_claims (
  id            TEXT PRIMARY KEY,
  resource_id   TEXT NOT NULL REFERENCES shared_resources(id),
  project_id    TEXT NOT NULL REFERENCES agent_projects(id),
  task_id       TEXT,
  amount        INTEGER NOT NULL DEFAULT 1,
  status        TEXT NOT NULL DEFAULT 'active',  -- active/released/expired
  expires_at    TEXT,
  created_at    TEXT NOT NULL,
  released_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_resource_claims_resource
  ON resource_claims(resource_id, status);

-- 运行时服务登记（M3-3）：本机/远端可观测的服务
CREATE TABLE IF NOT EXISTS runtime_services (
  id                TEXT PRIMARY KEY,
  project_id        TEXT REFERENCES agent_projects(id),
  name              TEXT NOT NULL,
  kind              TEXT NOT NULL DEFAULT 'port',  -- process/docker/systemd/launchd/port
  target            TEXT NOT NULL,
  health_check_json TEXT NOT NULL DEFAULT '{}',
  metadata_json     TEXT NOT NULL DEFAULT '{}',
  enabled           INTEGER NOT NULL DEFAULT 1,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  UNIQUE(project_id, name)
);

-- 运行时健康检查历史
CREATE TABLE IF NOT EXISTS runtime_checks (
  id         TEXT PRIMARY KEY,
  service_id TEXT NOT NULL REFERENCES runtime_services(id),
  status     TEXT NOT NULL,   -- healthy/degraded/down/unknown
  latency_ms INTEGER,
  detail     TEXT,
  checked_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_checks_service
  ON runtime_checks(service_id, checked_at DESC);

-- 运行时操作动作库（M3-3）。
-- 注：实际可执行动作以代码里的 ACTION_CATALOG 为准（命令必须硬编码，不能由库注入）；
-- 本表只用于「某个服务允许哪些动作」的可选扩展，平台不依赖它执行。
CREATE TABLE IF NOT EXISTS runtime_actions (
  id                TEXT PRIMARY KEY,
  service_id        TEXT NOT NULL REFERENCES runtime_services(id),
  action_key        TEXT NOT NULL,
  label             TEXT NOT NULL,
  command_template  TEXT NOT NULL,
  rollback_template TEXT,
  requires_confirm  INTEGER NOT NULL DEFAULT 1,
  timeout_s         INTEGER NOT NULL DEFAULT 300,
  created_at        TEXT NOT NULL,
  UNIQUE(service_id, action_key)
);

-- 运行时操作执行记录。
-- action_key 刻意不加外键：动作目录在代码里，库表可能为空，且历史记录
-- 不应因动作下线而失去追溯能力。
CREATE TABLE IF NOT EXISTS runtime_action_runs (
  id               TEXT PRIMARY KEY,
  action_id        TEXT NOT NULL,
  project_id       TEXT,
  status           TEXT NOT NULL DEFAULT 'pending', -- pending/preflight/running/succeeded/failed/aborted
  preflight_output TEXT,
  preflight_token  TEXT,
  dry_run          INTEGER NOT NULL DEFAULT 1,
  output           TEXT,
  exit_code        INTEGER,
  started_at       TEXT,
  finished_at      TEXT,
  created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_action_runs_action
  ON runtime_action_runs(action_id, created_at DESC);

-- ============================================================
-- schema v4（四期 M1 地基）：身份授权 / Policy / Kill Switch / 审批 / 事件平面
-- ============================================================

-- 主体：人、Agent、服务。Agent Profile 只是能力定义，不自动拥有项目数据访问权。
CREATE TABLE IF NOT EXISTS principals (
  id                TEXT PRIMARY KEY,
  principal_type    TEXT NOT NULL,                    -- user / agent / service
  display_name      TEXT NOT NULL,
  is_platform_owner INTEGER NOT NULL DEFAULT 0,
  agent_project_id  TEXT,                             -- agent 绑定的项目
  status            TEXT NOT NULL DEFAULT 'active',   -- active / suspended / revoked
  expires_at        TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

-- 项目成员：RBAC 的角色来源。角色只有 owner / operator / viewer / auditor 四类，
-- 领域责任角色（Evaluation Owner 等）映射到这四类，不新建全局超级角色。
CREATE TABLE IF NOT EXISTS project_members (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES agent_projects(id),
  principal_id TEXT NOT NULL REFERENCES principals(id),
  role         TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  UNIQUE(project_id, principal_id)
);

CREATE INDEX IF NOT EXISTS idx_project_members_principal
  ON project_members(principal_id);

-- 策略规则：effect=allow 时放行，effect=deny 时拒绝；无命中即拒绝（Deny by Default）。
CREATE TABLE IF NOT EXISTS policy_rules (
  id            TEXT PRIMARY KEY,
  scope         TEXT NOT NULL DEFAULT 'global',       -- global / project
  project_id    TEXT,
  principal_id  TEXT,
  principal_type TEXT,
  resource_type TEXT NOT NULL DEFAULT '*',
  action        TEXT NOT NULL DEFAULT '*',
  environment   TEXT NOT NULL DEFAULT '*',            -- dev / staging / production / *
  max_risk      TEXT NOT NULL DEFAULT 'R4',           -- 允许的最高业务风险等级
  effect        TEXT NOT NULL DEFAULT 'allow',        -- allow / deny
  requires_approval INTEGER NOT NULL DEFAULT 0,
  enabled       INTEGER NOT NULL DEFAULT 1,
  note          TEXT,
  created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_policy_rules_lookup
  ON policy_rules(enabled, resource_type, action);

-- Kill Switch：DB 驱动，按范围生效，带 TTL。触发后新请求立即被拒。
CREATE TABLE IF NOT EXISTS kill_switches (
  id           TEXT PRIMARY KEY,
  scope        TEXT NOT NULL,                         -- global / project / project_domain
  project_id   TEXT,
  domain       TEXT,
  reason       TEXT NOT NULL,
  triggered_by TEXT NOT NULL,
  expires_at   TEXT,                                  -- NULL = 不自动解除
  released_at  TEXT,
  created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kill_switches_active
  ON kill_switches(scope, project_id, released_at);

-- 审批：高风险动作的前置。提案者不得自批（服务层校验）。
CREATE TABLE IF NOT EXISTS approvals (
  id             TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL,
  resource_type  TEXT NOT NULL,
  resource_id    TEXT NOT NULL,
  action         TEXT NOT NULL,
  requested_by   TEXT NOT NULL,
  approved_by    TEXT,
  status         TEXT NOT NULL DEFAULT 'pending',     -- pending / approved / rejected / expired
  digest         TEXT,                                -- 绑定的内容摘要，内容变则审批失效
  request_note   TEXT,
  decision_note  TEXT,
  expires_at     TEXT,
  created_at     TEXT NOT NULL,
  decided_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_pending
  ON approvals(status, resource_type, resource_id);

-- 事件 Outbox：与业务状态同事务写入，由 dispatcher 单线程投递（至少一次）。
CREATE TABLE IF NOT EXISTS event_outbox (
  id             TEXT PRIMARY KEY,
  event_id       TEXT NOT NULL UNIQUE,
  event_type     TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  project_id     TEXT,
  resource_type  TEXT,
  resource_id    TEXT,
  correlation_id TEXT,
  causation_id   TEXT,
  payload_json   TEXT NOT NULL DEFAULT '{}',
  occurred_at    TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'pending',     -- pending / dispatched / failed
  attempts       INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT,
  dispatched_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_outbox_pending
  ON event_outbox(status, occurred_at);

-- 消费端去重：至少一次投递必然重复，靠这里保证不重复产生副作用。
CREATE TABLE IF NOT EXISTS event_inbox (
  event_id     TEXT NOT NULL,
  consumer     TEXT NOT NULL,
  processed_at TEXT NOT NULL,
  PRIMARY KEY (event_id, consumer)
);

-- ============================================================
-- schema v5（四期 M1）：Evaluation & Benchmark
-- 运行状态与决策状态分开存（V6.0 §9.3）：status 表示跑没跑完，
-- decision 表示评没评过。样本没执行不得计入通过数。
-- ============================================================

CREATE TABLE IF NOT EXISTS eval_suites (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES agent_projects(id),
  name        TEXT NOT NULL,
  description TEXT,
  status      TEXT NOT NULL DEFAULT 'active',
  created_by  TEXT NOT NULL DEFAULT 'local',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS eval_datasets (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES agent_projects(id),
  suite_id     TEXT NOT NULL REFERENCES eval_suites(id),
  name         TEXT NOT NULL,
  split        TEXT NOT NULL DEFAULT 'regression',  -- holdout / regression / safety / train
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   TEXT NOT NULL,
  UNIQUE(suite_id, name)
);

CREATE TABLE IF NOT EXISTS eval_dataset_versions (
  id             TEXT PRIMARY KEY,
  dataset_id     TEXT NOT NULL REFERENCES eval_datasets(id),
  version        INTEGER NOT NULL,
  case_count     INTEGER NOT NULL DEFAULT 0,
  content_digest TEXT NOT NULL DEFAULT '',
  status         TEXT NOT NULL DEFAULT 'frozen',    -- draft / frozen / active / revoked
  created_at     TEXT NOT NULL,
  frozen_at      TEXT,
  UNIQUE(dataset_id, version)
);

CREATE TABLE IF NOT EXISTS eval_cases (
  id           TEXT PRIMARY KEY,
  version_id   TEXT NOT NULL REFERENCES eval_dataset_versions(id),
  case_key     TEXT NOT NULL,
  input_json   TEXT NOT NULL DEFAULT '{}',
  expected_json TEXT NOT NULL DEFAULT '{}',
  tags_json    TEXT NOT NULL DEFAULT '[]',
  weight       REAL NOT NULL DEFAULT 1.0,
  created_at   TEXT NOT NULL,
  UNIQUE(version_id, case_key)
);

CREATE TABLE IF NOT EXISTS eval_evaluators (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES agent_projects(id),
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'deterministic', -- deterministic / model / human
  created_at  TEXT NOT NULL,
  UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS eval_evaluator_versions (
  id           TEXT PRIMARY KEY,
  evaluator_id TEXT NOT NULL REFERENCES eval_evaluators(id),
  version      INTEGER NOT NULL,
  rules_json   TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL,
  UNIQUE(evaluator_id, version)
);

CREATE TABLE IF NOT EXISTS eval_runs (
  id                     TEXT PRIMARY KEY,
  project_id             TEXT NOT NULL REFERENCES agent_projects(id),
  suite_id               TEXT NOT NULL,
  dataset_version_id     TEXT NOT NULL,
  evaluator_version_id   TEXT NOT NULL,
  release_ref            TEXT,
  status                 TEXT NOT NULL DEFAULT 'queued',
  decision               TEXT,
  total_cases            INTEGER NOT NULL DEFAULT 0,
  executed_cases         INTEGER NOT NULL DEFAULT 0,
  passed_cases           INTEGER NOT NULL DEFAULT 0,
  failed_cases           INTEGER NOT NULL DEFAULT 0,
  skipped_cases          INTEGER NOT NULL DEFAULT 0,
  safety_violations      INTEGER NOT NULL DEFAULT 0,
  score                  REAL,
  error_summary          TEXT,
  correlation_id         TEXT,
  started_at             TEXT,
  finished_at            TEXT,
  created_at             TEXT NOT NULL,
  -- R3 / M1：候选输出的来源。runtime = 由真实 Execution 产生；
  -- caller_supplied = 调用方直接塞进来的（骨架期做法，现在必须显式标出）。
  output_source          TEXT NOT NULL DEFAULT 'caller_supplied',
  runtime_execution_id   TEXT,
  output_digest          TEXT
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_project
  ON eval_runs(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS eval_results (
  id         TEXT PRIMARY KEY,
  run_id     TEXT NOT NULL REFERENCES eval_runs(id),
  case_id    TEXT NOT NULL,
  case_key   TEXT NOT NULL,
  status     TEXT NOT NULL,               -- pass / fail / skipped / error
  score      REAL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(run_id, case_id)
);

-- ============================================================
-- schema v6（四期 M2）：Knowledge Fabric
-- 版本不可变；发布才建索引；撤销与索引删除同事务（V6.0 §13.8）。
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_sources (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES agent_projects(id),
  kind          TEXT NOT NULL DEFAULT 'note',   -- note / file / url / transcript
  uri           TEXT,
  title         TEXT NOT NULL,
  classification TEXT NOT NULL DEFAULT 'Internal',
  license       TEXT,
  status        TEXT NOT NULL DEFAULT 'active',
  created_by    TEXT NOT NULL DEFAULT 'local',
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_items (
  id                 TEXT PRIMARY KEY,
  project_id         TEXT NOT NULL REFERENCES agent_projects(id),
  source_id          TEXT NOT NULL REFERENCES knowledge_sources(id),
  title              TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'draft',
  current_version_id TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_items_project
  ON knowledge_items(project_id, status);

CREATE TABLE IF NOT EXISTS knowledge_versions (
  id            TEXT PRIMARY KEY,
  item_id       TEXT NOT NULL REFERENCES knowledge_items(id),
  version       INTEGER NOT NULL,
  content       TEXT NOT NULL,
  content_digest TEXT NOT NULL,
  classification TEXT NOT NULL DEFAULT 'Internal',
  status        TEXT NOT NULL DEFAULT 'candidate',
  reviewed_by   TEXT,
  created_at    TEXT NOT NULL,
  published_at  TEXT,
  revoked_at    TEXT,
  revoke_reason TEXT,
  UNIQUE(item_id, version)
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id         TEXT PRIMARY KEY,
  version_id TEXT NOT NULL REFERENCES knowledge_versions(id),
  seq        INTEGER NOT NULL,
  content    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_version
  ON knowledge_chunks(version_id);

-- 全文索引：project_id / version_id 不参与分词，只用于过滤，保证检索在
-- 候选范围内执行（禁止先全局召回再由调用方过滤，V6.0 §13.5）。
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
  chunk_id UNINDEXED,
  project_id UNINDEXED,
  version_id UNINDEXED,
  content
);

CREATE TABLE IF NOT EXISTS retrieval_traces (
  id             TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL,
  principal_id   TEXT,
  query          TEXT NOT NULL,
  hits_json      TEXT NOT NULL DEFAULT '[]',
  correlation_id TEXT,
  created_at     TEXT NOT NULL
);

-- ============================================================
-- schema v7（四期 M3）：Workflow / Playbook Factory
-- 执行事实仍归第三期 tasks / scheduler，这里只存"定义与绑定"，
-- 不建第二套执行状态机（V6.0 §9.5）。
-- ============================================================

CREATE TABLE IF NOT EXISTS workflow_definitions (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES agent_projects(id),
  def_key     TEXT NOT NULL,
  name        TEXT NOT NULL,
  description TEXT,
  status      TEXT NOT NULL DEFAULT 'draft',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE(project_id, def_key)
);

CREATE TABLE IF NOT EXISTS workflow_versions (
  id            TEXT PRIMARY KEY,
  definition_id TEXT NOT NULL REFERENCES workflow_definitions(id),
  version       INTEGER NOT NULL,
  dsl_json      TEXT NOT NULL,
  digest        TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'draft',
  created_at    TEXT NOT NULL,
  published_at  TEXT,
  UNIQUE(definition_id, version)
);

CREATE TABLE IF NOT EXISTS workflow_test_runs (
  id         TEXT PRIMARY KEY,
  version_id TEXT NOT NULL REFERENCES workflow_versions(id),
  status     TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_bindings (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES agent_projects(id),
  version_id   TEXT NOT NULL REFERENCES workflow_versions(id),
  status       TEXT NOT NULL DEFAULT 'enabled',
  materialized INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  UNIQUE(project_id, version_id)
);

-- ============================================================
-- schema v8（四期 M4）：Agent Lifecycle & Release Governance
-- Manifest 不可变；审批绑定 manifest_digest，内容一变审批自动失效。
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_releases (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES agent_projects(id),
  release_key     TEXT NOT NULL,
  name            TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'draft',
  manifest_digest TEXT NOT NULL DEFAULT '',
  eval_run_id     TEXT,
  approval_id     TEXT,
  created_by      TEXT NOT NULL DEFAULT 'local',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  -- R3 / M4：这个发布到底有没有真实流量。
  -- SIMULATED = 只有状态流转，没有任何真实路由；没有流量入口时只能标这个，
  -- 不允许用「canary」这种词让人误以为已经在生产分流。
  release_mode    TEXT NOT NULL DEFAULT 'SIMULATED',
  -- 流量入口证据：真实路由存在时记录入口标识，SIMULATED 时必须为空。
  traffic_entry   TEXT,
  UNIQUE(project_id, release_key)
);

CREATE TABLE IF NOT EXISTS release_manifest_items (
  id              TEXT PRIMARY KEY,
  release_id      TEXT NOT NULL REFERENCES agent_releases(id),
  artifact_type   TEXT NOT NULL,   -- prompt / skill / model / workflow
  artifact_ref    TEXT NOT NULL,
  artifact_version TEXT NOT NULL,
  digest          TEXT NOT NULL,
  UNIQUE(release_id, artifact_type, artifact_ref)
);

CREATE TABLE IF NOT EXISTS canary_deployments (
  id              TEXT PRIMARY KEY,
  release_id      TEXT NOT NULL REFERENCES agent_releases(id),
  project_id      TEXT NOT NULL REFERENCES agent_projects(id),
  status          TEXT NOT NULL DEFAULT 'planned',
  traffic_pct     INTEGER NOT NULL DEFAULT 5,
  stop_threshold_json TEXT NOT NULL DEFAULT '{}',
  observed_json   TEXT NOT NULL DEFAULT '{}',
  started_at      TEXT,
  ended_at        TEXT,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rollback_records (
  id              TEXT PRIMARY KEY,
  release_id      TEXT NOT NULL REFERENCES agent_releases(id),
  deployment_id   TEXT,
  reason          TEXT NOT NULL,
  restored_digest TEXT,
  created_by      TEXT NOT NULL DEFAULT 'local',
  created_at      TEXT NOT NULL
);

-- ============================================================
-- schema v9（四期 M5）：Continuous Improvement
-- 失败/反馈 → Case → 根因 → 变更提案 → 沙箱评估 → 人工批准
-- Agent 不得直接改生产配置（V6.0 §16.6）。
-- ============================================================

CREATE TABLE IF NOT EXISTS failure_patterns (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES agent_projects(id),
  pattern_key TEXT NOT NULL,
  summary     TEXT NOT NULL,
  occurrences INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE(project_id, pattern_key)
);

CREATE TABLE IF NOT EXISTS improvement_cases (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES agent_projects(id),
  title         TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'open',
  source_type   TEXT NOT NULL DEFAULT 'evaluation',   -- evaluation / feedback / regression
  source_ref    TEXT,
  pattern_id    TEXT,
  root_cause    TEXT,
  root_cause_evidence_json TEXT NOT NULL DEFAULT '{}',
  owner         TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_proposals (
  id             TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL REFERENCES agent_projects(id),
  case_id        TEXT NOT NULL REFERENCES improvement_cases(id),
  title          TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'draft',
  base_version   TEXT,
  candidate_version TEXT,
  change_json    TEXT NOT NULL DEFAULT '{}',
  expected_benefit TEXT,
  rollback_plan  TEXT,
  approval_id    TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sandbox_evaluations (
  id            TEXT PRIMARY KEY,
  proposal_id   TEXT NOT NULL REFERENCES change_proposals(id),
  eval_run_id   TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',
  result_json   TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL
);

-- ============================================================
-- schema v10（四期 M6）：Business Outcome & ROI
-- 真实值 / 估算值 / 待验证值必须分开存，不允许混在一起算平均。
-- ============================================================

CREATE TABLE IF NOT EXISTS outcome_metric_definitions (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES agent_projects(id),
  metric_key  TEXT NOT NULL,
  name        TEXT NOT NULL,
  formula     TEXT NOT NULL,
  unit        TEXT,
  direction   TEXT NOT NULL DEFAULT 'higher_better',
  owner       TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE(project_id, metric_key)
);

CREATE TABLE IF NOT EXISTS outcome_metric_versions (
  id            TEXT PRIMARY KEY,
  definition_id TEXT NOT NULL REFERENCES outcome_metric_definitions(id),
  version       INTEGER NOT NULL,
  formula       TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  UNIQUE(definition_id, version)
);

CREATE TABLE IF NOT EXISTS outcome_records (
  id             TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL REFERENCES agent_projects(id),
  metric_key     TEXT NOT NULL,
  metric_version INTEGER NOT NULL DEFAULT 1,
  value          REAL,
  quality        TEXT NOT NULL DEFAULT 'unknown',
  source         TEXT,
  window_start   TEXT,
  window_end     TEXT,
  note           TEXT,
  created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outcome_records_project
  ON outcome_records(project_id, metric_key, created_at DESC);

-- ============================================================
-- schema v11（四期 M7）：Evidence-based Autonomy
-- 资格只表示"可以申请更高自动化"，不等于执行授权；
-- 真正放行仍然由 policy.decide 判定（V6.0 §18）。
-- ============================================================

CREATE TABLE IF NOT EXISTS autonomy_eligibilities (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES agent_projects(id),
  task_type     TEXT NOT NULL,
  tool_scope    TEXT NOT NULL DEFAULT 'read_write_code',
  environment   TEXT NOT NULL DEFAULT 'dev',
  level         TEXT NOT NULL DEFAULT 'A0',
  status        TEXT NOT NULL DEFAULT 'not_assessed',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  expires_at    TEXT,
  approved_by   TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE(project_id, task_type, tool_scope, environment)
);

CREATE TABLE IF NOT EXISTS autonomy_changes (
  id             TEXT PRIMARY KEY,
  eligibility_id TEXT NOT NULL REFERENCES autonomy_eligibilities(id),
  from_level     TEXT,
  to_level       TEXT NOT NULL,
  reason         TEXT NOT NULL,
  requested_by   TEXT NOT NULL,
  approved_by    TEXT,
  created_at     TEXT NOT NULL
);

-- ============================================================
-- schema v12（四期 M8 / P1）：Capability Catalog
-- 目录只做"引用与授权"，不复制 Registry 权威数据，也不扩大项目数据权限。
-- ============================================================

CREATE TABLE IF NOT EXISTS capability_listings (
  id              TEXT PRIMARY KEY,
  source_project_id TEXT NOT NULL REFERENCES agent_projects(id),
  capability_type TEXT NOT NULL,   -- agent / skill / workflow / domain_pack
  ref_id          TEXT NOT NULL,
  ref_version     TEXT NOT NULL,
  name            TEXT NOT NULL,
  summary         TEXT,
  status          TEXT NOT NULL DEFAULT 'draft',
  owner           TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  -- R3 / M8：上架时对 ref 的解析结果。悬空引用不允许上架，
  -- 但历史上已存在的要能看出是不是悬空，所以记录解析状态而不是靠 JOIN 现算。
  ref_resolved    INTEGER NOT NULL DEFAULT 0,
  ref_checked_at  TEXT,
  UNIQUE(source_project_id, capability_type, ref_id, ref_version)
);

CREATE TABLE IF NOT EXISTS capability_grants (
  id                TEXT PRIMARY KEY,
  listing_id        TEXT NOT NULL REFERENCES capability_listings(id),
  target_project_id TEXT NOT NULL REFERENCES agent_projects(id),
  actions_json      TEXT NOT NULL DEFAULT '[]',
  purpose           TEXT,
  granted_by        TEXT NOT NULL,
  expires_at        TEXT,
  revoked_at        TEXT,
  created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_capability_grants_target
  ON capability_grants(target_project_id, revoked_at);

-- ---------------------------------------------------------------------------
-- v13（体验层）：消息已读状态 + 个人保存视图
-- 只存个人偏好，不复制任何业务事实（任务/审批/交付状态仍在各自权威表）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notification_states (
  id               TEXT PRIMARY KEY,
  principal_id     TEXT NOT NULL,
  notification_key TEXT NOT NULL,          -- <type>:<source_id>，例如 approval:abc
  state            TEXT NOT NULL DEFAULT 'read',  -- read / unread / snoozed / pinned
  updated_at       TEXT NOT NULL,
  UNIQUE(principal_id, notification_key)
);

CREATE INDEX IF NOT EXISTS idx_notification_states_principal
  ON notification_states(principal_id, state);

CREATE TABLE IF NOT EXISTS saved_views (
  id            TEXT PRIMARY KEY,
  principal_id  TEXT NOT NULL,
  scope         TEXT NOT NULL,             -- portfolio / tasks / deliveries
  name          TEXT NOT NULL,
  filters_json  TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE(principal_id, scope, name)
);

-- ---------------------------------------------------------------------------
-- v15（Agent Team）：真实 Claude Code 后台会话的启动与指令记录
-- 只记录「平台启动过什么」，会话本身的真相仍以 claude agents --json 为准
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_traces (
  id          TEXT PRIMARY KEY,
  device_id   TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  project_id  TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  updated_at  TEXT NOT NULL,
  UNIQUE(device_id, session_id)
);

CREATE TABLE IF NOT EXISTS device_sessions (
  id          TEXT PRIMARY KEY,
  device_id   TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  name        TEXT,
  cwd         TEXT,
  kind        TEXT,
  status      TEXT,
  pid         INTEGER,
  project_id  TEXT,
  reported_at TEXT NOT NULL,
  UNIQUE(device_id, session_id)
);

CREATE TABLE IF NOT EXISTS device_commands (
  id          TEXT PRIMARY KEY,
  device_id   TEXT NOT NULL,
  project_id  TEXT,
  session_id  TEXT,
  instruction TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending',
  created_at  TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT,
  result      TEXT,
  error       TEXT,
  actor       TEXT
);

CREATE TABLE IF NOT EXISTS team_sessions (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES agent_projects(id),
  session_id  TEXT,
  agent       TEXT NOT NULL DEFAULT 'general-owner',
  status      TEXT NOT NULL DEFAULT 'launched',  -- launched / instructed / stopped
  instruction TEXT,
  output      TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_team_sessions_project
  ON team_sessions(project_id, created_at DESC);

-- schema v19（R2）：执行追溯质量 + 成本口径分离（真实 / 估算 / 未知）
--
-- D-1：executions.task_id 为空有两种完全不同的含义——
--      「这条执行本来就不属于任何 Task」（如 transcript 采集到的人工会话）
--      和「本来该关联但没关联上」（缺陷）。只靠 NULL 区分不出来，
--      所以显式记录 link_quality，让报表能算真实追溯率而不是把所有 NULL 都算漏。
-- D-2：原 cost_usd 一列混了真实账单和估算，无法判断可信度。拆成两列，
--      任何一列为 NULL 就是 UNKNOWN，绝不补 0。

-- 执行粒度的成本：token_usage_events 是「逐条消息」，这里是「一次执行」。
-- actual 来自 Claude CLI 结果里的 total_cost_usd（provider 侧计算，可核对）；
-- estimated 用价格版本算。两者都空就是 UNKNOWN，报表显示未知而不是 0。
CREATE TABLE IF NOT EXISTS model_price_versions (
  id              TEXT PRIMARY KEY,
  provider        TEXT NOT NULL DEFAULT 'anthropic',
  model_pattern   TEXT NOT NULL,      -- 匹配 token_usage_events.model，'%' 通配
  effective_from  TEXT NOT NULL,      -- ISO8601，含
  effective_to    TEXT,               -- ISO8601，不含；NULL = 至今有效
  input_usd_per_mtok       REAL NOT NULL DEFAULT 0,
  output_usd_per_mtok      REAL NOT NULL DEFAULT 0,
  cache_read_usd_per_mtok  REAL NOT NULL DEFAULT 0,
  cache_write_usd_per_mtok REAL NOT NULL DEFAULT 0,
  source          TEXT NOT NULL,      -- 价格从哪来（如 ccusage 拟合 / 官方价目）
  source_detail   TEXT,
  created_at      TEXT NOT NULL,
  UNIQUE(model_pattern, effective_from)
);

CREATE INDEX IF NOT EXISTS idx_price_versions_lookup
  ON model_price_versions(model_pattern, effective_from);

-- ============================================================
-- schema v20（R3）：P4 八大域真实链路
--
-- 审计事实（2026-11-15）：P4 相关 33 张表全部 0 行。代码骨架存在、单测通过，
-- 但没有任何一条真实受控链路跑过。v20 补齐让链路能真实跑起来的缺件：
--
--   M1 Evaluation  —— 输出必须来自真实 Execution，不能由调用方塞 candidate_outputs
--   M3 Workflow    —— human_gate 必须是真阻断，不是标签
--   M4 Release     —— 必须能区分「有真实流量」与「仿真」
--   M8 Catalog     —— ref 必须指向真实资源，不允许悬空引用
-- ============================================================

-- Workflow 运行实例：一次绑定的实际执行状态机。
-- 节点级状态单独存，因为 human_gate 的等待是「节点级」事实，
-- 用绑定表的 status 表达不了「流程卡在第 3 个节点的人是审」。
CREATE TABLE IF NOT EXISTS workflow_runs (
  id             TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL REFERENCES agent_projects(id),
  binding_id     TEXT NOT NULL REFERENCES workflow_bindings(id),
  version_id     TEXT NOT NULL REFERENCES workflow_versions(id),
  status         TEXT NOT NULL DEFAULT 'running',
                 -- running / waiting_approval / completed / failed / cancelled
  current_node   TEXT,
  input_digest   TEXT NOT NULL DEFAULT '',   -- 审批必须绑定输入内容
  correlation_id TEXT,
  started_at     TEXT,
  finished_at    TEXT,
  error_summary  TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_project
  ON workflow_runs(project_id, created_at DESC);

-- Workflow 节点执行状态。human_gate 到达这里变成 waiting_approval，
-- 没有有效审批记录就不能推进——这是 M3 的验收核心。
CREATE TABLE IF NOT EXISTS workflow_node_runs (
  id            TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES workflow_runs(id),
  node_key      TEXT NOT NULL,
  node_type     TEXT NOT NULL,             -- task / agent / human_gate / quality_gate
  status        TEXT NOT NULL DEFAULT 'pending',
                -- pending / running / waiting_approval / approved / rejected
                -- / completed / failed / skipped
  task_id       TEXT,
  approver      TEXT,                      -- 审批人 principal（可信来源）
  approved_at   TEXT,
  input_digest  TEXT NOT NULL DEFAULT '',  -- 审批绑定当时的输入摘要
  decided_at    TEXT,
  error_summary TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE(run_id, node_key)
);

CREATE INDEX IF NOT EXISTS idx_node_runs_run ON workflow_node_runs(run_id);

-- 人工审批记录：审批必须绑定 run + node + input_digest。
-- 输入一变（digest 不同）审批即失效，需要重新审批。
CREATE TABLE IF NOT EXISTS workflow_approvals (
  id            TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES workflow_runs(id),
  node_key      TEXT NOT NULL,
  input_digest  TEXT NOT NULL,
  decision      TEXT NOT NULL,             -- approved / rejected
  approver      TEXT NOT NULL,
  reason        TEXT,
  expires_at    TEXT,
  revoked_at    TEXT,
  created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_approvals_lookup
  ON workflow_approvals(run_id, node_key, input_digest);

-- 知识跨项目授权（R3 / M2）。
-- 默认检索是项目内封闭的；跨项目必须有一条**显式、有期限、可撤销**的授权。
-- 把授权建成独立表而不是给 items 加个 shared 标志，是因为授权有接收方、用途、
-- 有效期和审批人——这些是授权本身的属性，不是知识条目的属性。
CREATE TABLE IF NOT EXISTS knowledge_grants (
  id                 TEXT PRIMARY KEY,
  item_id            TEXT NOT NULL REFERENCES knowledge_items(id),
  version_id         TEXT,
  source_project_id  TEXT NOT NULL REFERENCES agent_projects(id),
  target_project_id  TEXT NOT NULL REFERENCES agent_projects(id),
  purpose            TEXT,
  granted_by         TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'pending',
                     -- pending / approved / rejected / revoked / expired
  approved_by        TEXT,
  approved_at        TEXT,
  expires_at         TEXT,
  revoked_at         TEXT,
  revoke_reason      TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  CHECK (source_project_id <> target_project_id)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_grants_target
  ON knowledge_grants(target_project_id, status);
