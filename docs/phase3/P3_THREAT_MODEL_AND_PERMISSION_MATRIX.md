# P3 威胁模型与权限矩阵

> 日期：2026-11-15
> 依据：蓝图 §3「P3-M0」第 3 项
> 资产 → 主体 → 威胁 → 控制 → 验证 五段式

---

## 1. 资产清单

| # | 资产 | 位置 | 敏感度 |
|---|---|---|---|
| A1 | 项目源码（MaterialDev / DDP） | `~/vscodeworkspace/*` | **高** |
| A2 | 平台库（任务/执行/成本/审计） | `data/platform.db` | 中 |
| A3 | Claude 凭据与 token | `~/.claude/`、`data/token` | **高** |
| A4 | Git 远端凭据（`gh`） | 系统 keychain | **高** |
| A5 | 生产环境（materialdev-lan-prod） | 内网 | **高** |
| A6 | 数据集与产物 | `~/Downloads`、huggingface 缓存 | 中 |
| A7 | 审计日志 | `audit_logs` | 中（完整性） |

---

## 2. 主体

| 主体 | 权限来源 | 说明 |
|---|---|---|
| `local-owner` | 回环来源自动解析 | 平台所有者，跨项目 |
| `remote-token` | Bearer token | **默认不是任何项目成员** |
| Agent 主体 | `principal_type='agent'` + `agent_project_id` | 绑定单项目 |
| 外部设备 | 无（Gateway 未建） | — |

---

## 3. 威胁与控制

### T1 · 跨项目越权读写

| 项 | 内容 |
|---|---|
| 威胁 | 持合法凭据者读写无权项目 |
| 面 | 所有 `/api/projects/{id}/*` |
| 控制 | `_gate_project` + `governance.authorize` + slug 精确匹配 |
| 验证 | `test_project_isolation`、S-1/S-2 |
| 状态 | ✅ VERIFIED |

### T2 · 越权派发 / 未审批执行

| 项 | 内容 |
|---|---|
| 威胁 | 未获授权就往项目里派发 claude 进程 |
| 面 | `/dispatch/items/{id}/run` |
| 控制 | ① 全局开关默认 off ② 项目开关默认 false ③ Policy ⑥ 审批消费 |
| 验证 | 实测 412 approval_required；`test_approvals.py` 10 用例 |
| 状态 | ✅ **P0 已修复**（原为 FAIL） |

### T3 · CLI 命令注入

| 项 | 内容 |
|---|---|
| 威胁 | prompt 或参数注入 shell 命令 |
| 面 | `executor._spawn()` |
| 控制 | `shell=False` + argv 白名单 + `--disallowedTools` + Bash 默认关 |
| 验证 | `executor_policy` 测试；R3 实测 material `git status` 干净 |
| 状态 | ✅ VERIFIED |

### T4 · Gateway 凭据泄漏 / 设备冒用

| 项 | 内容 |
|---|---|
| 威胁 | 设备凭据泄漏后可无限执行；重放旧命令 |
| 面 | 平台侧 `gateway.py`（P3-M4 落地）。**Gateway 进程本体未实现**，故仍无对外端口 |
| 控制（已实现） | 凭据只存 SHA-256 摘要（`gateway_devices.credential_hash`）；设备撤销同时写 `gateway_devices.status` 与 `registry_runtimes.status`（失败关闭）；命令幂等靠条件更新（`UPDATE … WHERE status='dispatched'`），并发下只有一个认领成功；命令过期（`expires_at` / created_at+TTL）即 EXPIRED，迟到的旧桥接回报会被 `windows.command_result` 拒绝（`command_closed`） |
| 控制（未实现） | 凭据轮换/短期化、Gateway 进程侧凭据落盘保护、出站长轮询鉴权握手 |
| 验证 | `app/tests/test_gateway.py`（54 项，含 4 线程并发认领只有一次成功） |
| 状态 | 🟡 **平台侧已建**（P3-M4）；Gateway 进程 ⏳ 未建 |

### T5 · Context Package 泄漏

| 项 | 内容 |
|---|---|
| 威胁 | 跨项目委派时把 Secret/完整对话传过去 |
| 面 | 尚无委派 |
| 控制（设计） | 最小化 + 脱敏 + 用途限制 + 有效期 + 审计 |
| 状态 | ⏳ **未建**（P3-M5） |

### T6 · PWA 会话盗用

| 项 | 内容 |
|---|---|
| 威胁 | 手机丢失后凭 token 无限访问；XSS 窃取 localStorage |
| 面 | 尚无 PWA |
| 控制（设计） | 短期会话 + 设备撤销 + MFA（若身份系统支持）+ 不存长期 Secret |
| 现状 | 已是 Bearer token + localStorage（非长期 Secret）✅ |
| 状态 | ⏳ **部分**（P3-M6） |

### T7 · Git Token 泄漏

| 项 | 内容 |
|---|---|
| 威胁 | 凭证进仓库/日志 |
| 面 | 全部提交历史 |
| 控制 | 全量扫描 + `git filter-repo` 重写 + `.npmrc` 移出索引 |
| 验证 | 99 commit 全历史扫描无泄漏（R6） |
| 状态 | ✅ VERIFIED |

### T8 · Usage 篡改 / 重复记账

| 项 | 内容 |
|---|---|
| 威胁 | 同一 token 记录入账两次，成本虚高 |
| 面 | `token_usage_events` |
| 控制 | `idempotency_keys` 表；三档成本口径 |
| 验证 | 成本覆盖率 96.75%；残差 0.0000% |
| 状态 | ⚠️ PARTIAL（去重未全面验证） |

### T9 · Approval 绕过

| 项 | 内容 |
|---|---|
| 威胁 | 绕过审批直接执行 |
| 面 | `/dispatch/items/{id}/run`、`/runtime/actions/*/run` |
| 控制 | `_policy_guard` + `_consume_approval` 双闸门；摘要绑定；一次性 |
| 验证 | `test_approvals.py` 10 用例 |
| 状态 | ✅ **P0 已修复** |

### T10 · Kill Switch 失效

| 项 | 内容 |
|---|---|
| 威胁 | 熔断后仍能执行 |
| 面 | 所有 Policy 判定点 |
| 控制 | `policy.decide()` 第一步即查 Kill Switch；fail-closed |
| 验证 | 无库返 503 而非放行（CI 实测） |
| 状态 | ✅ VERIFIED |

### T11 · 未授权目录访问

| 项 | 内容 |
|---|---|
| 威胁 | 读写 `repo_root` / worktree 之外的路径 |
| 面 | 文件查看、执行 cwd |
| 控制 | 路径白名单 + 越界拒绝 |
| 验证 | `file_viewer_smoke.mjs`；S-4 |
| 状态 | ✅ VERIFIED |

---

## 4. 权限矩阵

「动作 × 角色」，`—` 表示拒绝。

| 资源 / 动作 | owner | operator | viewer | auditor |
|---|---|---|---|---|
| project / read | ✅ | ✅ | ✅ | ✅ |
| project / update | ✅ | ✅ | — | — |
| task / create·update | ✅ | ✅ | — | — |
| task / transition | ✅ | ✅ | — | — |
| **dispatch_item / run** | ✅ **需审批** | ✅ **需审批** | — | — |
| dispatch_item / cancel | ✅ | ✅ | — | — |
| **runtime_action / run** | ✅ **需审批** | ✅ **需审批** | — | — |
| exec_runtime / enable | ✅ | — | — | — |
| **agent_release / publish** | ✅ **需审批** | — | — | — |
| approval / approve | ✅ | — | — | — |
| audit_log / read | ✅ | — | — | ✅ |
| policy_rule / read | ✅ | — | — | ✅ |
| **Secret 原文** | — | — | — | — |

> 注：V6.0 §7.4 —— Platform Owner **不自动**获得所有项目 Secret 或原始文件访问权。
> 上表最后一行即该原则的体现。

---

## 5. 残余风险

| # | 风险 | 等级 | 责任方 | 缓解 |
|---|---|---|---|---|
| R1 | `team.py` 绕过 Facade | 中 | 本平台 | P3-M3 收敛；现阶段 Bash 默认关 + 工具白名单 |
| R2 | 无 Gateway，设备模型空缺 | 中 | 本平台 | P3-M4 |
| R3 | 无预算控制，可能超支 | 中 | 本平台 | P3-M7 |
| R4 | 单用户无 MFA | 低 | 用户 | 本机回环免 token 是设计取舍 |
| R5 | 断电/磁盘满未演练 | 低 | 运维 | 有备份 + KeepAlive |

---

## 6. 结论

**能证明的安全属性**：跨项目隔离、执行白名单、审批不可绕过、Kill Switch、
Secret 不入库不入日志、路径不越界。

**不能证明的**：设备级撤销（无 Gateway）、委派数据最小化（无委派）、
预算强制（无预算）。

后者均为「功能未建」，非「建了不安全」。
