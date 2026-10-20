---
name: quarry
description: >
  使用 Quarry（qy）查询 PostgreSQL、MySQL、Redis 和 Neptune，对比不同环境的数据，
  维护可复用查询，管理连接或数据库工作台。任务需要数据库证据或明确涉及 Quarry 时使用；
  一般的数据库设计讨论不必使用。
---

# Quarry

[English](SKILL.md) · 两版规则相同，任选其一即可。

你负责选择查询和解释结果；CLI 负责执行、查询文件、元信息、校验和 skill 目录软链接。

## 调用 CLI

使用安装后的 SKILL.md 所在目录；全局参数放在子命令之前：

```sh
qy --skill-dir "<skill-dir>" --workspace "<workspace-dir>" <command> ...
```

`--workspace` 选择工作区；不指定时使用 config.toml 中的登记，未登记则使用当前目录。
保存时明确目标：多个工作区的默认写入位置是第一个，不会根据 `--db` 自动选其所属工作区。
不要把 skill 目录作为工作区。

`--skill-dir` 创建 `skill/queries/<workspace-name>/` 软链接，指向工作区的查询，
不包含连接凭据。每次传入可以重建缺失的链接。文件和链接交给 CLI 管理；
遇到冲突时说明情况，不覆盖用户文件。`qy` 或此选项不可用时，按项目安装说明处理。

按需选择命令，参数查 `qy <command> --help`：

- 发现：`connections`、`workspace list`、`schema`。
- 查询：`exec`；复用查询用 `list`、`describe`、`run`。
- 维护：`save`、`validate`、`fingerprint`、`audit`、`edit`、`remove`。
- 操作：`gui`、`connections`、`proxy`、`up/down/status`、`speedtest`、`local`。

## 查询要点

- 有合适的查询就复用；明确的 SQL 或一次性问题不必先搜索命名查询。
  名称有歧义时用 `--workspace` 缩小范围。
- 按需查看实时结构或项目模型。源码帮助理解业务，但可能与部署不同；没有源码也能查库。
- 明确数据库和环境，使用对应语言：SQL、Redis 命令或 Neptune openCypher。
  跨环境比较时保持查询参数一致，并标明各自目标。
- 探索性 `exec` 和 `run` 用 `--max-rows` 限制输出，只选需要的字段。
  总数用聚合查询；返回行数上限不代表查询成本上限。
- 参数值用 `:'name'`；裸 `:name` 只用于可信的原始替换。
- 写操作需要授权和 CLI 写入选项；生产写入需要明确确认，当前会话已授权的除外。
  写后核实数据变化。
- 说明结论、数据库、环境及截断情况。查看数据用 JSON，用户需要文件时用 CSV/NDJSON。
  从 GUI 分享查询时使用其复制查询链接按钮。

## 保留可复用查询

值得复用或用户要求保留的查询用 `save` 保存，按用途命名，将变化值通过 `--param` 声明为参数。
实际参考的结构定义文件用 `--schema-source` 声明；只参考实时结构的查询无需填写。
只有确实要替换时才用 `--overwrite`。

校验检查可执行性，不代表业务正确。保存校验失败后文件可能已存在；修正并重新校验后再报告成功。
指纹检查源码变化，不检查实时结构漂移；按需使用，批量用 `audit`，当前可执行性用 `validate`。

重试前先区分查询、连接和授权错误，限制重试次数，避免更改无关配置。
