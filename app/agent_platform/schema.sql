-- DDP 多项目 Agent 管理平台 · 独立 SQLite schema（schema v2）
-- 依据 docs/112 §2/§3、docs/113；与 DDP 业务库/迁移链完全独立。
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
  source     TEXT NOT NULL DEFAULT 'platform'
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
  created_at    TEXT NOT NULL
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
  updated_at       TEXT NOT NULL
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
