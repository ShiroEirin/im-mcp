# Changelog

本文件逐条记录本仓库相对上游 `stkoecn/im-mcp` 初始版本（v0.0.1）的全部改动，
便于上游维护者对照 review（对应 PR #10）。所有改动均已实测复现后再修改。

## 新增 MCP 工具（绿泡泡侧 6 → 15 个，全部只读）

| 工具 | 说明 |
|---|---|
| `search_contacts` | 按昵称 / 备注 / 微信号搜索联系人 |
| `list_chatroom_members` | 群成员名单（自动关联昵称与微信号，支持 keyword 过滤） |
| `get_messages_by_range` | 任意时间范围取消息（支持跨天与任意时段，带 `offset`） |
| `get_chat_activity_stats` | 活跃度统计（消息总量、按小时 / 按天分布、发言最多的人） |
| `search_media_files` | 图片 / 文件 / 视频索引及磁盘路径（可按类型、月份筛选） |
| `list_favorites` | 收藏条目（标题 / 描述 / 来源） |
| `list_emoticons` | 表情包与表情（含官方文字说明） |
| `get_persona_style` | 沟通风格蒸馏（画像 + 高频用语 + 典型语句） |
| `get_sync_status` | 索引新鲜度与规模（判断是否需要重新同步） |

## 新增参数 / 行为

- `search_wechat_messages`、`get_messages_by_date`、`get_messages_by_range` 新增可选
  `offset` 分页参数（默认 0，向后兼容；此前单日 / 区间查询无法翻页）。
- 全部工具的 `limit` 统一钳制到合法区间（新增 `_clamp()`）。SQLite 中 `LIMIT -1`
  等价于「无限制」，原实现下 `limit` 传 0 或负数会让单次调用返回全表；绿泡泡与
  钉钉两侧策略一致。
- 搜索类工具 `query` 为空时返回明确的错误项，而不是静默返回空结果。
- `wechat_mcp.tools.key_tool` 的 `extract` 子命令新增 `--decrypted-dir`
  （代码本就读取该属性，此前只能经 `wechat-init` 间接传入）。

## 数据完整性 / 正确性修复

- **消息库漏索引**：除 `message_*.db` 外还有 `biz_message_0.db`，原 glob 只匹配
  `message_*.db`，整个库被跳过（本机恢复 5,191 条消息）。
- **FTS 索引膨胀**：ETL 每次重复写入造成 `messages_fts_docsize` 5 倍膨胀
  （1,505,094 → 256,040），改为按版本号只 rebuild 一次，并在 ETL 收尾重建。
- **中文检索召回**：FTS5 `unicode61` 分词器对中文召回极低（实测约 3.8%），改为
  `LIKE` 子串匹配（FTS 表仍维护，留作备用），并转义 `% _ \` 通配符。
- **引用消息解析**：被引用内容本身是 appmsg 时递归解析 `<title>`/`<des>`，并剥离
  `wxid_` / `gh_` / `@openim` 前缀；自闭合标签 `<tag />` 会吞掉后续同名真标签，
  改为扫描前先剔除。
- **解密主链路补 WAL 处理**：原先只解密 `.db`、完全忽略 `-wal`，若 WAL 真压着未
  checkpoint 的数据会丢消息。新增带三重保护的合并：页数守卫（WAL 记录的库页数小于
  主库文件页数 ⇒ 已 checkpoint 的过期残留，跳过）+ 只应用最后一个提交帧及之前的帧
  + 合并后 `quick_check` 校验失败即回退为仅解密数据库本体。
- **DuangDuang 解密**：输出目录残留的 `dingtalk.db-wal` / `-shm` 会被 SQLite 叠加到
  新解密库上（实测读成 `database disk image is malformed`），解密前清理；
  `DINGTALK_DATA_DIR` 带尾部分隔符时版本被误判为 V2 而用错密钥。
- **DuangDuang 检索**：分表检索改为扫描全部分表后全局按时间排序再截断（原先按分表
  顺序边扫边停，结果被低编号分表主导）；`LIKE` 补 `ESCAPE`。
- **多账号**：`db_storage` 探测不再依赖 glob 顺序（实测会固定选中较旧账号），改取
  最近写入者；`wechat-init` 与 MCP 侧使用同一策略。
- **密钥工具**：`icacls` 失败时如实上报（原先不看返回码，失败也报「已限制」，等于
  谎报密钥文件已收紧）；`load_passphrase()` 对非法内容不再让 `bytes.fromhex` 抛异常
  中断整个 extract。
- **派生表结构**：`media_files` 主键改为 `id` + `UNIQUE(md5, rel_path)`，同一张图在
  多个会话下的路径不再互相覆盖；派生表带版本号，结构升级时整体重建。
- **其他健壮性**：`limit` / `offset` 边界、时间戳溢出兜底、缺表时 `init_schema()`
  自愈、旧库无 `Msg_` 表时跳过、`Name2Id` 读取失败不再写入不稳定会话键、会话昵称
  预载消除 N+1 查询、`sync_data` 返回真实统计（原先统计缺失导致显示为 0）。
- **依赖锁定**：`uv.lock` 与 `pyproject.toml` 版本对齐（原先 `uv lock --check`
  直接报错：lockfile needs to be updated）。

## 文档

- `README.md` 与 `skills/im-mcp/SKILL.md` 补齐全部工具、参数与 CLI 说明，修正
  DuangDuang 侧工具名与参数名。
