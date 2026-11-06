# Quarry Rust 功能等价版：范围与验收规格

状态：原始设计规格；Rust 已实现并完成本机链路验收，目前按 RC 发布流程进行跨平台安装验收。
基线：Python Quarry 1.11.0，提交 `a08ee49`。
工作分支：`codex/quarry-rust`。

## 本次决定

用户授权：先完成 Rust 兼容版，现有功能和 UI 都保留，为后续两个 Rust 平台 CLI 共用做好内核基础，再做增强版。旧 API 无需拘泥兼容，以合理设计为准。

因此，本项目所称“兼容版”是**功能与用户数据等价版**，不是 Python API、HTTP 路径、JSON 字段、CLI 数字退出码的逐字兼容实现。

- 所有现有功能必须有明确归属和验收证据，不能以新 API 为由删掉少用的能力。
- UI 的布局、视觉、交互、快捷键、草稿保护、结果归属保持；允许调整前端 API adapter/types。
- 已有配置、查询文件、助手历史、浏览器状态、Docker 数据卷和端口可以继续使用；必要的数据格式迁移必须可验证、可回退。
- 不默认保留 Python binding、Python 运行时或兼容 facade。旧 Python 稳定发行物作为迁移前回退版本。
- 暂不接入真实 IAM、公司资源、生产数据库授权、托管网页与本机配对，也不改两个平台 CLI。
- 本阶段为上述集成提供稳定 Rust facade、明确上下文与可替换策略，不预先建设一套 IAM。

## 功能覆盖矩阵

| 能力面 | 等价版范围 | 主要验收来源 |
| --- | --- | --- |
| 数据库 | PostgreSQL、MySQL、SQLite、Redis；Neptune/openCypher 继续为实验性 | COMPATIBILITY.md；engine/release-contract tests |
| 查询 | 临时与命名查询、参数、只读/显式写入、分页/截断、类型精度、超时、记录定位、导出 | core/safety/timeout/row-link tests |
| 查询资产 | list/describe/save/validate/fingerprint/audit/edit/remove，明确 workspace 与覆盖行为 | core/cli/MCP management tests |
| 配置目录 | workspace 聚合、分组/env-set、连接管理、冲突消歧、Skill 查询目录链接 | workspace/groups/private-connection tests |
| 连接运行时 | SSH、系统配置/代理、隧道复用与跨进程附着、keeper、重连、health、speedtest | tunnel/proxy/keepalive/process-identity tests |
| Local | PostgreSQL/Redis 生命周期、数据卷/端口、连接登记、schema-only sync、Neptune empty/strict mock | local/local-sync/docker tests |
| GUI | 当前全部 TESTING.md 功能矩阵、静态资源、配置事件、版本/更新/Changelog | browser/visual/react-app tests |
| MCP | 当前九个工具的能力；工具 schema 与名称可优化 | MCP unit/integration/process tests |
| SQL Assistant | 模型配置、流式/非流式、Bifrost 协议、metadata tools、仅提议 SQL、取消、历史与 checkpoint | assistant/provider/state/browser tests |
| 发布 | 原生安装、升级/导入、离线运行、CLI/GUI/MCP、供嵌入的 Rust 库 | 新分发与 consumer acceptance |

完整能力清单来源是现有源码、COMPATIBILITY.md、TESTING.md 和测试，不以此表代替细项核对。

## 代码组织

采用同一仓库的 Cargo workspace。先以少量 crates 和清晰模块组织，避免每个内部类型都拆独立包。

- `quarry-core`：类型、查询策略、引擎 adapters、执行上下文、结果和错误、metadata 能力；不依赖 CLI/HTTP/IAM。
- `quarry-runtime`：workspace/catalog、查询资产、配置加载/变更、缓存、隧道/代理/keeper、执行调度和资源生命周期。
- `quarry-local`：Docker、端口/卷、managed-local 登记、结构同步、Neptune 本地服务。
- `quarry-assistant`：模型协议、metadata 工具、提议、会话历史与 revision/checkpoint；复用 core/runtime。
- 应用适配层：`qy` CLI、本机 HTTP 服务、MCP；作为薄入口装配上述库。
- `web/`：现有 UI；HTTP DTO 变化集中在 client adapter，业务交互与存储保护规则保留。

依赖方向：应用 → services/runtime → core。库不调用 CLI，不解析 shell stdout，不自行创建全局 Tokio runtime，不在构造/导入时启动服务、切 cwd、打印或退出进程。

后续宿主 CLI 应通过 Rust dependency 直接调用 facade，不依赖独立安装的 `qy` 或 Python。用两个最小 consumer fixture 验证嵌入，而不是本阶段直接修改宿主仓库。

## 内核必须具备的基础

### 明确的目标与上下文

- 资源引用明确区分 workspace、逻辑数据库、环境、执行位置；配置代次用于避免缓存或连接误复用。
- 运行时把资源引用解析为具体连接；密码不进入资源 ID、普通错误或审计日志。
- `WorkspaceCatalog`、`TunnelManager`、`LocalManager` 等可实例化并注入，没有全局“当前 workspace”。
- library 使用者显式提供配置、访问策略、预算和取消上下文；宿主决定身份与授权，内核执行操作限制。
- 本机 profile 的文件/连接编辑、密码 reveal、Docker 管理不自动成为未来 hosted profile 的权限。

### 执行与值模型

- 内部行按列位置保存，保留重复列名；对象字段后缀在序列化边界处理。
- 值区分大整数、精确小数、浮点、文本、JSON、二进制、日期时间、NULL 等；不能先转 JSON 再补救精度。
- 明确区分返回行数、影响行数、截断、未完成原因、原始/实际执行查询与大小统计口径。
- 执行上下文具备内部 execution ID、状态、取消令牌和连接/执行/结果预算。跨入口共享历史/结果是增强版，本阶段不为此改变 UI。
- 取消请求、停止客户端等待、确认数据库已停止是不同事实；不能以 drop future 或 socket timeout 宣称服务端取消成功。
- 连接与并发有界，连接归还前完成事务/会话清理；缓存按目标、配置代次及策略边界隔离。
- 各引擎显式声明事务、取消、metadata、执行计划、同步等能力，不能用一个泛化接口掩盖差异。

## 驱动与外部依赖

初步候选，PG/MySQL 在真实类型解码与取消验证后定案：

| 能力 | 方向 | 首先验证 |
| --- | --- | --- |
| PostgreSQL | SQLx 或 tokio-postgres | arbitrary SQL、NUMERIC/JSON/数组/自定义类型、空结果、RETURNING、TLS/SSH hostname、取消 |
| MySQL | SQLx 或 mysql_async | MySQL 8.4 认证、DECIMAL/unsigned BIGINT、时间/二进制、超时与会话恢复 |
| SQLite | rusqlite，专用阻塞 worker | 动态类型、只读/authorizer、interrupt、WAL、事务回滚、缺失文件不创建 |
| Redis | redis-rs | RESP 值、nil/空串/二进制/嵌套数组、SCAN、按 argv 执行命令保护 |
| Neptune | Rust HTTP client 适配现有 openCypher | 现有自定义 endpoint/TLS/代理；真实 AWS/IAM 支持不凭迁移自动承诺 |
| Docker | Docker API 或受控 CLI adapter，验证 context 后定案 | 当前 daemon/context/socket、旧数据卷/端口、自定义挂载、幂等与数据保留 |

普通查询不再依赖 psql/redis-cli；系统 OpenSSH、pg_dump/必要时 psql 可作为有意保留的专用工具。Rust 化不意味着替换 OpenSSH 配置语义或自行重新实现 PostgreSQL dump。

TLS 的原始目标 hostname、SSH 转发地址、证书校验应明确分离；不把关闭证书校验当作隧道兼容方案。

## 用户状态与已有问题

迁移保护：config.toml、各 workspace connections.toml/queries、assistant-credentials.json、助手 history.sqlite3、浏览器 drafts/results/preferences、已有 Docker 卷、managed-local 元数据、端口和挂载。

- 使用读取/导入现有格式或可回退迁移；不擅自修改真实连接凭据。
- 保留现有浏览器 origin/存储格式，或提供明确迁移；仅改端口也可能让用户无法发现草稿。
- Rust 不信任旧 keeper/PID 记录；不能误附着或杀死其他进程。
- local down 保留数据，purge 明确授权；端口冲突不停止其他应用。
- Rust 本地 Docker 明确绑定 loopback。旧 Python 的 `-p port:internal` 与 localhost-only 文档不一致，不照抄。
- schema sync 保持源只读、local loopback 无隧道目标、staging 导入。当前旧版最终 rename 序列不等于原子切换；Rust 需并发锁、恢复状态和失败补偿，覆盖第二次 rename 失败。
- dump/连接凭据不放入可见进程参数或普通日志。
- SQL Assistant 仍然只生成提议，不自动执行查询；保留 revision CAS、幂等 Apply、Restore、fingerprint 约束与模型输出边界。

## 阶段与完成门槛

### M0：规格和基线

全量功能清单、状态清单、功能等价验收矩阵、可回退发布策略；保留 Python 基线作为测试对照。

### M1：驱动验证和 Rust 内核

先验证最难的真实类型、TLS/隧道、只读与取消；实现明确上下文、值/错误/能力模型及有界执行。新旧差分运行在专用测试库，写入绝不重复发送到真实业务库。

### M2：运行时、本地能力和助手

完成目录/连接/查询资产、SSH/代理/keeper、Docker/local sync、SQL Assistant 完整服务。现有状态可导入，旧数据卷可继续使用。

### M3：入口与 UI 接回

CLI/MCP/HTTP 使用同一服务 facade；UI 只调整适配层。browser/process fixtures 必须启动真实 Rust binary，不能继续启动 Python 再把测试通过当作迁移完成。

### M4：原生候选与升级验收

- PostgreSQL、MySQL、Redis、SQLite 真实引擎验收；Neptune 单列实验性与真实服务证据。
- 原生 macOS arm64/x64、Linux x64 候选及实际运行验证；其他平台不因能编译而宣称支持。
- 新安装、从 1.11.0 导入、单机离线、CLI/GUI/MCP、助手协议与历史、所有 GUI 行为矩阵、两个 Rust consumer fixture。
- 连接/查询/大结果/取消的端到端时间和峰值内存实测，不预先承诺提速倍数。
- 库与安装包不需要 Python/Node runtime；Docker/SSH/结构同步所需外部工具单独报告。
- 旧 Python 稳定发行物保留为可回退项。Rust 功能等价未完成前不得切换日常安装入口。

## 发布策略

迁移前，main 的 semantic-release 可触发 Python tag/PyPI/MCP 发布。Rust 迁移在隔离分支/worktree推进；在合并实现前调整发布链，Rust 候选使用明确的原生预发布渠道。完成 M4 后再更新安装、升级、MCP Registry 和 PyPI 收尾说明，不默认维护永久双实现或 Python facade。

本规格本身不暂停/更改线上发布配置，不替换当前 qy 安装，不操作业务数据库或本机数据卷。

## 增强版（功能等价验收后）

1. 人与 Agent 可共享同一次执行：有界、隔离、可管理留存的执行记录/结果；打开历史默认不重跑。
2. 有过滤、上限与详情级别的渐进式 metadata 搜索。
3. 项目查询资产的验证与结构变化提示，以及可解释的多环境结构/结果对比。
4. 独立任务接入 IAM/托管资源授权/网页到本机配对与宿主 CLI。

功能等价版只为这些能力建立内核基础，不同时上线新 UI、完整 DBA 平台、多租户 IAM 或大量新数据库。
