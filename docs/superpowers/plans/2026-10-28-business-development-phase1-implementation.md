# 业务接入开发简化：阶段 1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有接入合同的前提下，建立可持续运行的兼容行为基线、可照做的业务接入与事务配方，以及不启动应用和基础设施的独立合同检查命令。

**Architecture:** 继续使用现有三类注册表、显式业务 catalog、正式发现与合同交叉校验。先补兼容证据，再隔离发现路径的导入副作用，最后让一个薄 CLI 调用同一套校验；受理、推进和恢复仅提供业务所有的配方与隔离用例，不增加运行框架。

**Tech Stack:** Python 3.13+、Pydantic、现有 pytest/pytest-asyncio、SQLAlchemy；事务验收使用显式配置的一次性 PostgreSQL/Redis，外部对端仅用 fake/loopback。不新增依赖。

**Spec:** [已合并重构设计](../specs/2026-10-27-business-development-simplification-design.md)，主要落实第 6.3、7、9 节阶段 1，以及 A2、A7–A9 的现行行为基线；A3–A6 只固定旧接口行为，不提前实现新接口。

状态：**阶段 1 任务 1–5 已实施并完成本地验收，待控制器综合评审。** 日期：2026-10-28。用户已授权 subagent-driven-development 和分任务本地提交；代码基线为 `6371ab7`（PR #30 合并后，包含 PR #29），最终受验代码为 `d006a0c`。实际结果见第 6.3 节。本文保留原任务步骤与设计范围，以下“新增/拟新增”描述的是原计划动作；阶段 2–4 的目标 API、真实业务接入、推送、PR、合并和部署均未实施。

## Global Constraints

- 正式 `catalog.entries()` 保持为空，直到合同、资源、业务实现和恢复均完整的真实业务获准接入；隔离示例不得进入生产目录、Base 或迁移。
- 继续支持 `app/integrations/<business>` 和 `app/businesses/<business>` 两种来源、单文件和入口包；不增加第四类发现树。
- 保持“全部正式入口 → 合同交叉校验 → local 入口 → 资源/工厂”顺序；API/worker 使用同一显式清单。
- 不新增 `@ecs_command`、`compile_commands`、模型形式 send/create、`resolve_declared`、`@ecs_result`；它们不属于阶段 1。
- 不改变 HTTP/wire、原身份、JSON 规范化、冻结字节/路由、错误类型、投递窗口或保留规则；新 ECS 命令沿用 `PROTOCOL_RETRY_WINDOW=300 秒`，旧命令保留原期限。
- 保留手工 `CommandBinding/DeviceAnnex`、共享模型、`CallbackPayload` 的 `strict=True, extra="ignore"` 等合法旧合同；不能以建立基线为名收紧同一附录版本。
- 受理使用原 `ctx.session`；独立推进在新 session 首条 SQL 前设置 `REPEATABLE READ`，锁序为工作线 ID → 设备 ID → 业务动作固定顺序。
- 固定完整直接前序及 caller/cycle/资源范围；100 条一批，分批/混合读取仍在同一快照；业务进度、消费依据与下一 Outbox 意图原子提交。
- `40001` 关闭失败 session，以新 session 整事务有界重试；耗尽保留待办和原等待依据，不换身份、不无限重试。实际业务的重试上限和恢复调度仍由具体业务确定。
- 无通用 transaction runner、业务 worker、第二 Sender、补偿引擎或脚手架生成器；不直接读写框架表来实现业务消费。
- 兼容性问题先用公开行为复现，再修共享根因。已有测试完整覆盖时直接复用；不增加仅验证内部实现、过渡状态或夹具接线的测试。
- 后续实施时保留其他未提交内容与暂存状态；提交、推送、真实接入和部署均另行授权。

## Review Focus

| 容易遗漏的条件 | 期望行为与负责用例 |
| --- | --- |
| 从元数据子模块进入 `app.core`，或先导入同名 `logger` 子模块 | 检查阶段不构造引擎/客户端、不要求运行配置；正常调用公共导出仍获得原对象和配置语义。任务 2 的冷进程及导入顺序回归负责 |
| 正式 catalog 为空、包内必需入口缺失，或只有模块内部依赖缺失 | 空业务清单可成功且明确报告；存在的错误声明必须失败，不能当空清单跳过。任务 3 的空目录/失败传播用例负责 |
| `--environment production` 与继承的 `ENVIRONMENT=local` 相冲突，或 dev 声明误用正式来源 | 命令参数明确检查范围；先正式后 local，来源标记不被改写，任何正式错误不能被 local 补齐。任务 1、3 的新进程矩阵负责 |
| 旧 ECS 合同共享模型、允许扩展字段，且重复调用遇到当前绑定变化 | 保留合法旧输入、原命令/Result 合同及冻结期限，不能为了未来声明检查收紧模型。任务 1 的旧接口行为基线负责 |
| 101 项前序、更新 UNKNOWN、重复/丢失唤醒，或下一意图入队后发生 40001 | 不拼接版本或部分消费；新事务重读整组，恢复仍使用原身份。任务 4 的真实 PostgreSQL 场景负责 |

---

## 1. 规划时证据与实施范围

下表保留规划时的能力和缺口；实施后的能力、验证和限制以第 6.3 节及现行指南为准：

| 核对位置 | 当前行为 | 阶段 1 处理 |
| --- | --- | --- |
| [发现协调器](../../../app/integrations/discovery.py)、[catalog](../../../app/businesses/catalog.py) | 已有正式/local 发现、跨业务校验和成功缓存；`validate_business_contracts()` 返回原绑定及附录 | 复用函数，不复制校验规则、不新增全局注册表 |
| [API 入口](../../../app/main.py)、[启动回归](../../../tests/businesses/test_workline_business_flow.py) | API 已先正式发现/校验再 local；已有资源/工厂顺序回归 | 保留并重跑；不为 CLI import `app.main` 或 Celery |
| [core 公共导出](../../../app/core/__init__.py)、[数据库设施](../../../app/core/database.py) | 导入 `app.core` 会提前加载数据库设施；logger 初始化还会读取 Settings | 作为独立检查的必要前置，限定修复导入接缝，保持公共导出兼容 |
| [WMS 发送回归](../../../tests/integrations/wms_request/test_service.py)、[ECS 服务回归](../../../tests/integrations/ecs_command/test_service.py) | 已覆盖入队/回滚、部分身份重入和期限；不是新模型 API 的等价证明 | 补位置/关键字形式、序列化及共享旧合同的实际缺口 |
| [业务结果消费](../../../tests/businesses/test_result_consumption.py)、[共享隔离夹具](../../../tests/helpers.py) | 已有受理原子性、独立推进、101 项、真实 40001、重复唤醒和配置变化证据 | 将对应场景整理为配方，避免重写第二套测试业务 |

规划时的只读冷进程探针在导入发现器时拦截到了 `app.core.database`。因此“加一个调用现有 loader 的脚本”尚不能满足无资源装配的目标。导入隔离是平台维护者工作，不转嫁给普通业务开发人员。

### 1.1 实施文件范围（原计划）

| 文件 | 计划动作与责任 |
| --- | --- |
| `tests/integrations/wms_request/test_service.py` | 扩展现行字符串发送的兼容缺口 |
| `tests/integrations/ecs_command/test_service.py`、`test_result.py`、`test_role_contract_guards.py` | 按缺口补原调用、共享模型、迟到 Result；已有重启/门禁用例继续复用 |
| `tests/integrations/test_registration_bootstrap.py`、`test_registration_discovery.py`；`tests/businesses/test_contract_assembly.py`、`test_catalog.py` | 复用/补充来源、冷启动和合同交叉校验基线 |
| `app/core/__init__.py` | 仅隔离公共导出造成的提前设施导入；不改变各导出自身的运行职责 |
| `tests/architecture/test_contract_imports.py`（新增） | 冷进程验证元数据路径无资源初始化及公共导出兼容 |
| `scripts/check_integration_contracts.py`（新增） | 单次进程的合同检查 CLI，复用现有发现与交叉校验 |
| `tests/scripts/test_check_integration_contracts.py`（新增） | CLI 退出码、输出范围、失败传播与无外部访问 |
| `docs/integrations/business-development-quickstart.md`（新增） | 兼容清单、最短接入路线和检查命令；只示范当前 API |
| `docs/integrations/business-transaction-recipes.md`（新增） | 受理 A、推进 B、消费/恢复及证据对照卡 |
| `docs/integrations/business-packages.md`、`registration.md`、`outbound-results.md` | 仅补入口/配方链接及必要的边界说明，保持原契约正文为准 |
| `tests/businesses/test_result_consumption.py`、`test_workline_business_flow.py`、`tests/helpers.py` | 只补配方无法被现有用例证明的缺口；共享夹具仍使用独立 MetaData 和 finally 清理 |

除上述导入接缝外，不计划修改应用运行代码。若发现还必须改数据库生命周期、日志语义、协议执行或启动编排，先记录复现并回到范围评审，不能默认为本计划授权扩大重构。CI 已运行全量 pytest，无需为重复执行同一测试修改 `.github/workflows/ci.yml`；不修改依赖、版本、配置、迁移或部署脚本。

## Task 1: 固定当前兼容行为与开发清单

**Files:** 上表中的 WMS/ECS 服务、注册/装配测试；新增 quickstart 的兼容清单部分。

**Interfaces:** 消费当前 `request_operation/callback_operation/ecs_event`、字符串形式 `WmsRequests.send`、手工 `CommandBinding/DeviceAnnex`、旧 `EcsCommands.create` 和公开结果读接口；产出后续阶段可重复运行的旧行为回归，不新增应用接口。

- [x] **步骤 1：建立覆盖清单。** 按下表记录已有用例与缺口；同一行为已被完整证明就引用原用例，不再复制。运行一次对应套件，记录实际通过/跳过及存储配置情况。

| 兼容项 | 必须保留的断言 | 首选现有位置 / 补测名 |
| --- | --- | --- |
| WMS 调用形式与序列化 | 旧位置/关键字调用均有效；默认值、别名输入、显式 null、嵌套 JSON 得到原规范 wire；固定 target 和原 operation_id 重入返回同 message_id，字节和 expires_at 不变 | `wms_request/test_service.py`；缺口用 `test_legacy_send_forms_preserve_canonical_payload_and_reference` |
| WMS 错误/事务 | 未注册、字段错误、非法身份不入队；异输入仍为原 DuplicateMessage；业务回滚无残留 | 同文件已有 invalid/reentry/rollback 用例 |
| 原 ECS 调用 | 同 owner/call_key/规范输入返回原 command_code、bytes、route、期限；不先解析变化后的当前路由；异输入拒绝 | `ecs_command/test_service.py` 的 duplicate/期限用例；缺口用 `test_existing_call_replays_before_current_route_resolution` |
| 共享与宽松旧附录 | 两角色共用同一模型/附录仍可装配；合法扩展字段按原规则处理；可信保留历史附录后合法迟到 Result 可接收，不改变原模型对象/版本 | contract_assembly 的 identical_annex 用例；缺口用 `test_shared_legacy_models_keep_original_result_contract`，在服务与 Result 测试按职责拆断言 |
| 来源与顺序 | 两种目录、单文件/包、production/local 的实际发现和元数据正确；内部导入错误、正式缺项和冲突必须失败 | registration bootstrap/discovery、business catalog/contract_assembly |
| Transport/结果语义 | 原任务/请求/caller、版本、成员及 UNKNOWN/闭合规则保持；ACK/DONE/CLOSED 不替代成功/许可 | 原 Transport results/predecessors 和业务结果消费套件；不重新定义协议 |

- [x] **步骤 2：只为实际缺口补行为测试。** 新增用例写在上述职责对应文件；只通过公开服务/HTTP 触发行为，冻结内容断言可读取测试拥有的持久记录。业务消费不得借此直读框架表。
- [x] **步骤 3：执行新增用例和现有对照。** 兼容基线针对现行合法行为，补测第一次通过是正常结果；若失败，先确认是否准确描述现行合同，不能为迎合未来新 API 改动旧合同。实际缺陷另以失败用例和最小修复评审。
- [x] **步骤 4：写入 quickstart 清单。** 列清三类注册、手工命令及原结果读取方式、原身份保存项和迁移资格；标明新语法尚不可用，目标改址/改标签不属于语法迁移。

验收：原形式和合法旧合同均有可执行证据；不以测试禁止未来新增接口，也不把仅有通过数量当覆盖证据。后续阶段 2–4 才增加新旧接口的等价断言。

## Task 2: 隔离合同发现的导入副作用

**Files:** `app/core/__init__.py`；新增 `tests/architecture/test_contract_imports.py`；复用 `tests/core/test_config.py`、`tests/core/test_logger.py`、`tests/businesses/test_catalog.py`。

**Interfaces:** 不新增公共 API；保留 `app.core.__all__` 中现有名称及调用行为，`from app.core import X` 与原定义处导入仍指向同一对象。元数据导入不等于实际请求数据库或 logger 等运行时导出。

- [x] **步骤 1：先写冷进程失败用例 `test_contract_discovery_does_not_initialize_runtime_resources`。** 从无 `.env` 的临时 cwd 启动子进程，移除 DATABASE_URL/REDIS_URL/JWT_SECRET_KEY 及 TEST_* 环境变量，只加入受控项目路径。在导入前安装拦截器，禁止应用数据库/Redis设施、API/Celery/runtime 装配模块，并把引擎、HTTP/Redis 客户端构造与网络连接设为失败。调用正式发现和原交叉校验；允许 Pydantic、SQLAlchemy 类型/元数据，不能用“导入 ORM 就失败”替代资源检查。
- [x] **步骤 2：运行该单例并确认红灯。** `rtk uv run --frozen --no-sync pytest tests/architecture/test_contract_imports.py -q`；当前应定位到 core 的提前导入，而不是因缺少第三方依赖或子进程未加载项目而失败。
- [x] **步骤 3：最小隔离公共导出的加载时机。** 让声明/发现使用的 core 子模块不因包初始化加载 database/deps/logger 等运行设施。正常显式请求原导出时保持原实现、Settings 校验及资源初始化责任；不增加“检查模式”环境开关、伪造 sys.modules、空实现或第二套 Settings。
- [x] **步骤 4：加入 `test_core_public_exports_keep_identity_in_both_import_orders`。** 覆盖公共导出优先、定义子模块优先、`from app.core import *`；特别覆盖同名 `logger` 子模块，公共 logger 必须仍是原 Logger，不能变成 module。沿用已有 APP_NAME 对 API/Celery/logger 名称一致性的测试，不改配置或日志语义。
- [x] **步骤 5：验证绿灯及原启动回归。** 运行 architecture、core、business catalog 和 startup 顺序用例；新增冷进程用例 `test_api_and_worker_bootstrap_keep_public_core_exports`，在受控测试配置下分别导入 API 和现有 Celery 入口，核对 APP_NAME/公共对象，不启动 worker、不连接 broker。它只证明原启动兼容，不代表已有通用业务 worker。若一个简单延迟导出映射破坏同名导出或类型依赖，不能以绕过测试交付；仍须满足本节兼容合同，超出限定接缝则停止扩围并提交范围问题。

验收：没有运行配置也能完成元数据发现；正常 API/worker 的原配置校验、资源生命周期与导出身份不变。此项不宣称任意第三方业务 import 都天然无副作用；业务 composition/入口仍须遵守声明和薄入口约束。

## Task 3: 提供独立合同检查命令

**Files:** 新增 `scripts/check_integration_contracts.py`、`tests/scripts/test_check_integration_contracts.py`；更新 quickstart 和 registration 的使用说明。

**Interfaces:** 唯一命令入口 `main(argv: Sequence[str] | None = None) -> int`；通过模块方式调用，避免另做 sys.path 引导或新包发现器：

```bash
rtk uv run --frozen --no-sync python -m scripts.check_integration_contracts --environment production
rtk uv run --frozen --no-sync python -m scripts.check_integration_contracts --environment local
```

`--environment` 必填，仅 `production/local`：production 表示正式静态范围，不是连接生产环境；其他非 local 部署可用同一正式范围检查。不自动读取 ENVIRONMENT 决定范围，不要求数据库 URL、Redis URL、JWT 或运行 `.env`。每次命令使用全新进程，不提供同进程清缓存/重扫接口。

- [x] **步骤 1：先建 CLI 公开行为用例。** 普通空 catalog 检查从真实模块入口运行；失败/多业务场景由测试临时包和受控子进程 bootstrap 注入 `catalog.entries`，不为生产命令增加 `--module/--plugin` 等动态 import 参数。共享子进程/模块写入助手放 `tests/helpers.py`，测试文件不互相导入。

| 用例名 | 断言 |
| --- | --- |
| `test_empty_business_catalog_reports_checked_formal_scope` | 退出 0，明确 businesses 为空；仍发现 Transport 的正式回调，不能用“没有业务”跳过整个检查 |
| `test_environment_selects_scope_after_formal_validation` | 两种声明来源均覆盖；参数胜过继承环境值；正式校验先于 local，production 无 local_only 项 |
| `test_invalid_contract_never_reports_success` | 参数化重复注册/错误文件名/错误角色/缺附录/附录冲突/必需模块缺失/内部 ImportError；退出 1，stdout 无成功 JSON，保留安全定位 |
| `test_invalid_formal_entry_cannot_be_repaired_by_local_entry` | local 声明不能填平或覆盖正式初始入口缺项；失败时不调用任何业务工厂 |
| `test_check_does_not_invoke_runtime_or_business_handlers` | 两种环境下资源构造、连接、factory、handler、模型表创建和消息重放均为 0；清空运行配置也成功 |
| `test_cli_arguments_are_validated_before_application_imports` | `--help` 退出 0；缺省/非法 environment 退出 2；均不提前 import 应用装配 |
| `test_failure_diagnostics_never_echo_configuration_or_payload` | 让坏模块的异常包含唯一敏感值标记；退出 1 且 stdout/stderr 均不回显该值，仍有固定失败阶段、错误类型和安全源码定位 |

正常真实空 catalog 的核心断言如下；`project_root` 是源码根路径，`clean_env` 按任务 2 清除运行配置并提供项目导入路径，定义在测试侧而非生产脚本：

```python
result = subprocess.run(
    [sys.executable, "-m", "scripts.check_integration_contracts", "--environment", "production"],
    cwd=project_root,
    env=clean_env,
    capture_output=True,
    text=True,
)
assert result.returncode == 0, result.stderr
report = json.loads(result.stdout)
assert report["environment"] == "production"
assert report["businesses"] == []
assert any(row["name"] == "transport.task.resulted@v1" for row in report["entries"])
```

- [x] **步骤 2：运行用例确认缺命令的失败原因。** 此时目标是模块尚不存在；fixture/import 路径错误不得算有效红灯。
- [x] **步骤 3：实现单次命令流程。** argparse 先解析，再调用 `load_formal_modules()` → `catalog.validate_business_contracts()` → 仅 local 时 `load_local_modules(environment="local")`；不调用 `build_runtime`、`load_business_models`、`configure_ecs`、路由构造器或应用 lifespan。不复制校验规则，不新增 registry，不实例化 RoleRoutes 来代替静态检查。
- [x] **步骤 4：收集原元数据并输出一个 JSON。** stdout 固定顶层为 `environment`、`businesses`、`entries`、`command_contracts`、`scope`；businesses 是排序后的业务 code 字符串数组，两个清单字段为对象数组，scope 是含 checked/excluded 字符串数组的对象。entries 从三类公开读取器取得，保留原 kind/name/device_role 和 module/source_file/symbol/local_only；command_contracts 来自原校验返回的 CommandBinding/DeviceAnnex，含角色、动作、附录 id/version 和原来源。按稳定身份排序，不锁定注册数量，不导出真实地址、运行配置、样例 Payload 或业务记录。
- [x] **步骤 5：明确覆盖范围和错误输出。** scope 明示只检查已发现且导入的声明、正式业务来自显式 catalog；不表示任意源码文件已加载。校验覆盖以现有注册器和 `validate_business_contracts` 为准，不增加旧 DeviceAnnex 的严格性要求，也不宣称完成未来新声明的迁移资格验证。local 可列出原 dev 三类入口，但不装配通用 v3 调试命令、Status runtime 能力或模型资源；这些未检查项必须列为不在范围，不能以空集合声称验证成功。异常退出 1；复用 `app.diagnostics.diagnose/log_exception` 的安全定位、固定阶段消息及固定修复指引，不输出原异常、配置、原始 data 或凭据。CLI 边界失败不能把部分已加载清单写成成功报告。
- [x] **步骤 6：执行 CLI 全矩阵。** 对正常、无运行配置、坏声明和正式/local 两种环境运行新进程；任务 2 的资源拦截须在 import 之前生效。命令成功只证明静态合同，不证明路由、数据库迁移、网络、许可或物理完成。

验收：退出码 0/1/2 与文档一致；真实空 catalog 和测试中的完整合同均可检查；坏声明可定位且不能静默成功；HTTP dev 目录行为和权限完全不变。此命令需要项目 Python 3.13+ 与锁定应用依赖，不扩展为发布工具那样的 Python 3.11 标准库脚本。

## Task 4: 把接入与事务规则整理成可验证配方

**Files:** 新增 quickstart、transaction-recipes；三个现行指南补链接；必要时扩展现有 business flow/result_consumption 测试及共享夹具。

**Interfaces:** 只使用现有 `require_runtime(ctx)`、Capabilities、公开 send/create/get_result/get_by_call/list_unclosed_predecessors 和调用方事务。文档中的 `accept/advance/scan_pending` 等是具体业务的方法名，不能作为新增平台 API 引用。

- [x] **步骤 1：完成最短接入路线。** 先核对合同和稳定业务经过，再 definition → 相邻模型/薄入口 → 手工命令绑定/附录 → 固定 composition → 具体业务持久状态/待办/推进 → 验证后加入 catalog。逐步列出普通开发者写什么、检查什么、读哪份文档；WMS operation、ECS Event、Command/Result 各一张流程卡，Transport 新场景通常复用既有能力。所有未确认业务示例只在文档/隔离测试，不自动注册。
- [x] **步骤 2：写受理 A 配方。** 使用 ctx.session；校验当前业务/许可/本次经过，在同事务保存原收据、业务行和持久待办；只依赖当前输入的初始意图才可同事务登记。Duplicate/Conflict 的判断在业务写入前；Rejected/普通存储异常的回滚语义按原接收器。禁止 commit/rollback、网络或另开推进事务。
- [x] **步骤 3：写独立推进 B 配方。** 提交后只通知原 execution_id；丢失/重复由持久待办发现恢复。新 session 首 SQL 前 RR；无锁定位后按固定锁序重读当前许可，完整固定前序/原身份/资源范围，100 项分批并在同一快照读原 DTO；核对成功、版本、成员和必要业务决定，原子保存消费及下一意图。Result 接收先提交，业务随后消费。
- [x] **步骤 4：写失败与恢复卡。** 对 NOT_FOUND/UNAVAILABLE、更新 UNKNOWN、HELD、非法/不匹配结果、清理后的证据不足分别保存等待或核对状态；不换号重发。40001 回滚并关闭整个失败 session，新 session 重新定位、加锁、重读整组；隔离示例沿现有最多 3 次尝试，耗尽返回 retry_later 且 pending/原等待依据仍在。真实业务的重试次数、等待原因持久化和调度间隔须写进该业务计划，不提供通用恢复器，也不为记录原因悄悄提交部分失败事务。
- [x] **步骤 5：把每张配方卡关联到公开行为证据。** 复用下表；缺少实际断言时再补测试。需要新测试的缺口先红后绿，共享测试业务只读公开结果接口，测试表继续独立 MetaData/显式创建/finally 清理，不能把测试服务复制成生产示例。

| 配方要求 | 首选原用例（`tests/businesses/` 下） |
| --- | --- |
| Event/WMS 受理同事务；重复入站、跨线许可 | result_consumption 的 accepted_execution / acceptance_receipt_pending；workline_business_flow 的 same_business_two_lines / business_mismatch / callback_restart |
| 原结果接收须提交后才能推进 | `test_result_receive_must_commit_before_independent_advance` |
| 固定完整 101 项版本/成员；旧里程碑不覆盖新 UNKNOWN | `test_runtime_snapshot_keeps_full_101_member_versions` 及 Transport results/predecessors 原用例 |
| 真正 40001 新 session 整组重读、耗尽保留待办 | `test_serialization_failure_reopens_and_rereads_complete_business_group` |
| 下一意图登记后出错仍整体回滚再核对 | `test_serialization_after_intent_rolls_back_and_rechecks_all_results` |
| 重启/丢通知/重复唤醒与配置改变不串线或多发 | `test_business_runtime_consumption_recovery`、`test_duplicate_wakeups_and_config_update_serialize_without_cross_line`、`test_original_pending_execution_does_not_follow_reassigned_device` |
| 保留窗口后只能依赖已保存业务依据 | `test_wms_consumption_survives_cleanup_only_with_saved_business_evidence` |

- [x] **步骤 6：用一次性 PostgreSQL 重跑配方证据。** SQLite/fake 的快测不能代替隔离、锁序和 serialization 验收。耗尽后须能用原 execution_id 恢复；ACK、DONE、RECORDED、PREPARE_ACCEPTED、CLOSED 均不能单独使业务成功。记录命令、存储配置是否显式、实际通过/跳过和断言结果，不记录连接凭据。

验收：开发者能按当前接口找到新建、注册、调用、读取与恢复的位置；配方每条关键事务约束都有实际用例，不只给不能执行的伪代码。此证据仍是隔离业务，不代表人工/粗分真实闭环或开发耗时指标已验收。

## Task 5: 综合验证与交付门槛

顺序固定为任务 1 → 2 → 3 → 4 → 最终验收。任务 4 的文档草拟可在任务 2–3 期间进行，但最终示例和命令只引用已完成、验证过的交付。每任务完成时审查范围和证据；代码提交仍需单独授权，不把本计划的复选框当提交或上线许可。

### 6.1 实施时执行的检查

以下是实施检查的可重复入口。实际最终验收使用第 6.3 节记录的完整套件和专属一次性存储运行器；这些命令本身不表示每次运行都已配置存储或全部用例均未跳过。

```bash
# 当前兼容、来源和资源隔离：先按任务拆小集验证，最后跑这一组。
rtk uv run --frozen --no-sync pytest tests/integrations/test_registration.py tests/integrations/test_registration_bootstrap.py tests/integrations/test_registration_discovery.py tests/businesses/test_catalog.py tests/businesses/test_contract_assembly.py tests/integrations/wms_request tests/integrations/ecs_command tests/architecture tests/core -q

# 独立 CLI（任务 2、3 完成后）。
rtk uv run --frozen --no-sync pytest tests/scripts/test_check_integration_contracts.py -q
rtk uv run --frozen --no-sync python -m scripts.check_integration_contracts --environment production
rtk uv run --frozen --no-sync python -m scripts.check_integration_contracts --environment local

# 已显式配置一次性 TEST_DATABASE_URL/TEST_REDIS_URL 后的事务证据。
rtk uv run --frozen --no-sync pytest tests/businesses/test_workline_business_flow.py tests/businesses/test_result_consumption.py tests/integrations/test_result_consumption.py tests/integrations/wms_transport/test_results.py tests/integrations/wms_transport/test_predecessors.py -ra

# 最终门槛。
rtk uv run --frozen --no-sync ruff check .
rtk uv run --frozen --no-sync ruff format --check .
rtk uv run --frozen --no-sync pytest -ra
rtk git diff --check
```

没有显式一次性存储配置时，相关集成用例应按原规则跳过并报告，阶段 1 的事务验收不能因此标完成；配置后连接失败必须失败。可使用现有 CI 的临时 PostgreSQL/Redis 取得证据，不创建业务数据库、不启动或修改联调/生产服务。新增 Markdown 用现有解析器检查实际链接与 Python 示例语法；现有发布工具依赖闭包应覆盖两份新增指南，不为此改打包机制。

### 6.2 验收清单

| 门槛 | 必须交付的可核查结果 |
| --- | --- |
| G1 兼容基线 | 任务 1 矩阵有原用例/补测及实际结果；合法共享/宽松旧合同、原身份/字节/期限保持；无新接口或协议变化 |
| G2 无设施独立检查 | 无 `.env`、无运行地址/密钥仍可冷进程检查；两种来源、production/local、正式先于 local 和全部失败分支有证据；禁止资源/网络/factory/handler 的拦截真实生效 |
| G3 工具结果可信 | 0/1/2、排序/来源/范围和 stdout/stderr 规则明确；空 catalog 不假装完整业务已交付；损坏声明不产生成功报告，CLI 不依赖 API/worker 启动 |
| G4 配方可用 | 当前 API 的三条接入路线和两类事务/恢复卡完整，原子受理、101 项、真实 40001、恢复和许可边界有独立 PostgreSQL 证据 |
| G5 变更收敛 | core 导出身份、导入顺序、APP_NAME/logger、现有 API/worker 冷启动及全量回归通过；无依赖/配置/迁移/正式业务清单变更；其他未提交内容和暂存状态保留 |
| G6 文档交付 | 现行/目标/未检查范围一致；示例语法、本地文档链接和发布文档依赖闭包检查通过；验证报告不混淆本地、CI、供应商和现场证据 |

仅 G1–G6 全部满足后才可宣布阶段 1 实施完成，并进入阶段 2 的 ECS 声明/编译实施计划。阶段 1 不承诺普通开发者已能调用新 API，也不证明真实业务已上线。

### 6.3 实际执行与本地验收记录（2026-10-28）

受验代码为 `d006a0c`，在隔离工作区 `feature/business-development-phase1` 完成以下检查；任务 5 只更新本文，不增加应用能力。任务 1–4 的本地实现提交依次为 `179e956`、`2e2750d`、`a31aa2f`、`d9c13a8`；文档修正 `d006a0c` 已关闭 WMS 薄入口未转交业务 Outcome/data 的问题。各任务评审已完成，控制器对整个分支的最终综合评审另行进行。

- [x] G1：兼容清单、补测与原用例均有执行证据。
- [x] G2：独立冷进程和资源边界证据齐全。
- [x] G3：CLI 退出、输出、来源、顺序和失败规则有公开行为证据。
- [x] G4：配方关键事务及恢复场景有实际 PostgreSQL 证据，无相关跳过。
- [x] G5：完整套件无失败，原导出/启动兼容及变更范围检查完成；跳过限制见下文。
- [x] G6：文档语法、实际链接、测试引用与原发布依赖闭包检查完成。

| 门槛 | 实际证据与结论 |
| --- | --- |
| G1 | [quickstart](../../integrations/business-development-quickstart.md) 的兼容矩阵关联原测试和补测。任务 1 专属存储回归为 452 passed、7 skipped（SQLite 并发/锁分支，PostgreSQL 对照已运行）；最终完整专属存储套件再次覆盖这些模块。合法共享/宽松旧附录、位置/关键字 WMS 发送、规范化、原身份/字节/路由/期限及历史 Result 均保留。原 ECS 重入用例已完整覆盖，不新增计划中的重复测试。 |
| G2 | 最终完整套件含 8 项 architecture 冷进程/导出回归及 28 项 CLI 行为用例，覆盖两种来源、文件/包、正式先于 local、所有声明失败矩阵。任务 3 的七个负向探针已证明 configure、路由/命令构造、factory、handler、replay 在函数体前被拦截；最终受验 guard 未改。另以 `python -I`、仅 PATH 环境和无 `.env` 的临时 cwd 分别运行真实 production/local 模块入口：均退出 0、stderr 为空，各输出一个 JSON，entries 分别为 2/6，businesses 和 command_contracts 均为空。 |
| G3 | CLI 的 0/1/2、参数优先、稳定排序、原来源、空正式目录、损坏声明不输出成功 JSON及安全 stderr 已由公开子进程用例覆盖。CLI 只调用现有发现与交叉校验，不启动 API/worker，不装配运行资源；空业务目录不能解释为真实业务已交付。 |
| G4 | [事务与恢复配方](../../integrations/business-transaction-recipes.md) 的受理 A、独立推进 B 与三条接入卡均使用当前 API。完整专属存储套件实际执行 business flow/result consumption、WMS/Transport 读接口与原接收器用例：原子受理/回滚、Result 先提交、完整 101 项同快照、真实 40001 新 session 整事务重读/耗尽后原 ID 恢复、下一意图后回滚、许可/配置变化及保留窗口均无跳过。业务等待原因、实际重试上限及调度仍由具体业务负责。 |
| G5 | 两次完整套件均退出 0，无失败或 warning summary；完整数目和跳过理由如下。三种导入顺序、18 个 core 公共导出身份、APP_NAME/logger、原 Settings 校验与 API/Celery 冷导入均运行。相对 `6371ab7` 无依赖、配置、迁移、正式 catalog、发现编排或 CI 修改；原工作区及原计划未修改，隔离工作区任务开始时暂存区/工作树干净。 |
| G6 | 用已安装 MarkdownIt commonmark 解析 6 份文档（5 份指南及本文），67 个本地链接/锚点、15 个 Python 代码块语法检查通过；5 份指南的 56 个测试函数引用实际存在。原 `scripts.release_bundle.payload_files` 得到 55 个文件的传递闭包，包含两份新指南；没有修改发布工具或新增依赖。现行/目标/未检查范围和证据边界一致。 |

最终完整测试只各运行一次，使用锁定依赖，不启用或修改联调/生产服务：

```bash
# 快速完整套件：显式去除 TEST_* 存储设置。
rtk proxy env -u TEST_DATABASE_URL -u TEST_REDIS_URL uv run --frozen --no-sync pytest -ra
# 专属一次性 PostgreSQL/Redis：由本次本地运行器私下提供 TEST_*，不输出连接值。
rtk python3 .superpowers/sdd/2026-10-28-business-development-phase1-implementation/run-store-tests.py -ra
```

| 运行 | 实际结果 | 跳过原因及限制 |
| --- | --- | --- |
| 快速完整套件 | 2286 passed、749 skipped，109.59 秒，退出 0 | 706 项需要 TEST_DATABASE_URL，28 项需要 TEST_REDIS_URL，1 项需要显式 worker Redis；12 项为 PostgreSQL 专属语义/SQLite 分支；另 2 项需要独立 DEVICE_MIGRATION_DATABASE_URL / BINDING_MIGRATION_DATABASE_URL。未配置存储的跳过不能作为事务验收。 |
| 专属存储完整套件 | 3005 passed、30 skipped，201.24 秒，退出 0 | 28 项是 PostgreSQL 并发/锁/独立 Sender 语义的 SQLite 分支，实际 PostgreSQL 对照已执行；另 2 项仍需要上述单独迁移库，本次未创建或使用这些库。没有 business flow/result consumption、101 项、40001 或恢复场景跳过。 |

`ruff check .` 通过；`ruff format --check .` 为 436 files already formatted；`git diff --check` 通过。任务 5 仅修改验收文档，未在文档记录更新后重复完整套件。完整原始摘要、逐项跳过和一次性检查命令保存在本地 `.superpowers/sdd/2026-10-28-business-development-phase1-implementation/task-5-report.md` 及相邻日志；这些本地流程证据不进入正式业务目录。

本记录证明阶段 1 的实现和本地/隔离验收，不证明 CI、完整真实业务、供应商、现场、生产或开发耗时指标。正式 `catalog.entries()` 仍为空；未新增模型形式 send/create、`@ecs_command`、`compile_commands`、`resolve_declared` 或 `@ecs_result`，未开始阶段 2–4 实施。未推送、创建 PR、合并、部署或向真实 WMS/ECS 发送请求；专属临时存储保留给控制器最终评审后处理。

## 7. 回退与本轮交付边界

后续代码回退按任务保留独立、可审查的修改；CLI 和指南可移除，兼容行为测试建议保留。若回退 core 导入接缝，须同时移除依赖其无副作用保证的 CLI，不能留下表面可运行却偷偷初始化设施的工具。无数据库迁移、无数据重写，不触碰原消息、Evidence、UNKNOWN/HELD 或旧附录。

**原规划轮次只完成本文；本轮经另行授权已实施阶段 1 任务 1–5，并取得第 6.3 节的本地/隔离证据。** 独立 CLI、导入隔离、兼容基线和当前 API 配方已交付到隔离分支及本地提交，控制器综合评审尚待完成。阶段 2–4 的新 API、真实业务/合同接入及供应商/现场验收未实施；推送、PR、合并、部署和真实请求未执行。本文复选框只记录上述任务进度，不构成后续接入或发布授权。
