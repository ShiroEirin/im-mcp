---
name: im-mcp
description: Windows / macOS 本地微信 (WeChat) 与钉钉 (DingTalk) 聊天记录、通讯录、群聊与文件检索 MCP 服务运维管理指南。当需要初始化离线数据库、提取微信解密密钥 (wechat-init)、离线同步钉钉数据库 (dingtalk-init)、检索聊天历史/周报/工时，或在一键彻底清理导出的解密数据与缓存 (im-clean) 时使用。
---

# IM-MCP 智能体运维与数据生命周期管理指南

本技能指导智能体在 Windows 及 macOS 环境下管理 `im-mcp` 工具集（包含微信 MCP 与钉钉 MCP），涵盖**前置环境检查、离线数据库初始化与解密、聊天数据检索以及任务完成后的安全清理**。

---

## 1. 核心定位与运行策略 (Core Principles & Execution Strategy)

### 1.1 运行方式优先原则与执行策略
智能体在引导或协助用户部署时，应遵循以下运行策略：
1. **优先持久化安装**：若在 Claude Desktop、Cursor 等桌面客户端或支持持久化工具的环境中，推荐使用 `uv tool install "git+https://github.com/stkoecn/im-mcp.git" --with "im-mcp[all]"`；
2. **沙箱/受限环境降级**：若当前 AI 客户端环境（如 Pi 沙箱环境）拦截了全局工具安装（提示 `Blocked ... use uvx`），应直接遵循该环境安全策略，使用 `uvx --from "git+https://github.com/stkoecn/im-mcp.git[all]"` 方式即席运行相关命令；
3. **国内网络镜像备选（Gitee 源）**：若用户处于国内网络环境或访问 GitHub 受限/较慢，可直接将上述 Git 地址替换为 Gitee 镜像仓库：
   - 持久化：`uv tool install "git+https://gitee.com/ws4m/im-mcp.git" --with "im-mcp[all]"`
   - 即席运行：`uvx --from "git+https://gitee.com/ws4m/im-mcp.git[all]" <command>`
4. **禁止私自编写协议测试代码**：安装并运行初始化（`wechat-init` 与 `dingtalk-init`）后，**严禁私自编写脚本或代码模拟底层 JSON-RPC/stdio 协议测试**，必须直接交付用户并提供客户端启用引导；
5. **宿主环境感知与交付引导**：智能体应根据当前所运行的客户端环境（如 Pi、Claude Code/Desktop、Cursor、Cline 等），自主判断并向用户提示具体的 Skill 与 MCP 启用步骤（如告知输入 `/reload`、重启客户端或在侧边栏开启信任），并直接给出 1~2 个日常对话的使用示例。

### 1.2 命令矩阵与权限一览

```bash
# 方式 A：全局持久化隔离安装 CLI 工具链（推荐优先选择）
uv tool install "git+https://github.com/stkoecn/im-mcp.git" --with "im-mcp[all]"
```

命令矩阵如下：

| 命令 | 全局安装后调用方式 | 沙箱/受限环境 `uvx` 即席运行方式 | 适用场景 | 权限要求 | 关键产物 / 路径 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`wechat-init`** | `wechat-init` | `uvx --from "git+https://github.com/stkoecn/im-mcp.git[all]" wechat-init` | 提取微信 DBKey 并离线解密为 SQLite 数据库 | Windows 需管理员权限 (扫内存)；macOS 支持 `--passphrase` 或配置持久化 | `~/.wechat_mcp/decrypted/`, `~/.wechat_mcp/app.db` |
| **`dingtalk-init`** | `dingtalk-init` | `uvx --from "git+https://github.com/stkoecn/im-mcp.git[all]" dingtalk-init` | 离线提取钉钉密钥、解密 DB 并合并 WAL 事务 | 普通用户权限 | `~/.dingtalk_mcp/decrypted/dingtalk.db` |
| **`im-clean`** | `im-clean` | `uvx --from "git+https://github.com/stkoecn/im-mcp.git[all]" im-clean` | 一键安全清理所有导出的明文数据、密钥与缓存 | 普通/管理员权限 | 清理上述两者的解密库及全局数据目录 |
| **`wechat-mcp`** | `wechat-mcp` | `uvx --from "git+https://github.com/stkoecn/im-mcp.git[all]" wechat-mcp` | 微信 FastMCP 标准服务端入口 | 普通用户权限 | stdio 接口 |
| **`dingtalk-mcp`** | `dingtalk-mcp` | `uvx --from "git+https://github.com/stkoecn/im-mcp.git[all]" dingtalk-mcp` | 钉钉 FastMCP 标准服务端入口 | 普通用户权限 | stdio 接口 |

---

## 2. 智能体操作工作流 (Lifecycle Workflows)

### 2.1 任务开始前：自动初始化数据库 (Pre-flight Initialization)

当用户要求“分析最近微信/钉钉沟通”、“生成周报”、“查某某群聊文件”时，若 MCP 报错提示“数据库未找到”或返回空结果，智能体应自主执行初始化：

#### 钉钉数据库初始化 (DingTalk)
钉钉是纯 Python 本地离线解密，几乎 100% 自动化，无需管理员权限：
```bash
# 智能体执行初始化
dingtalk-init
```
- **预期输出**：检测到活跃 UID，输出 `🎉 钉钉数据初始化同步成功！`。
- **环境前置判断（禁止盲目扫描）**：若提示未找到数据目录或 UID，**禁止智能体自行在宿主机上漫无目的地递归深搜扫描进程或磁盘**。请直接暂停并向用户说明情况，询问并提醒用户确认钉钉客户端是否已在当前机器上登录。

#### 微信数据库初始化 (WeChat)
微信需要密钥并对 WCDB 数据库执行解密与校验：
- **Windows**：
  ```bash
  # Windows: 管理员身份运行
  wechat-init
  ```
  微信需要从正在运行的客户端进程内存中提取密钥并执行校验。**微信客户端必须处于启动并已登录状态**。若微信未启动，`wechat-init` 会报错。
- **macOS**：
  ```bash
  # macOS: 一键执行初始化（自动检测并启用 LLDB 持续断点捕获）
  wechat-init
  ```
  工具会自动定位 macOS 微信数据目录（`~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/*/db_storage`），并按层级进行密钥获取：
  - **前置权限（如遇 attach 失败）**：macOS 系统存在 Hardened Runtime 与 SIP 保护，官方安装的微信默认禁止外部调试器附加。若执行提示权限受阻，需指引用户在 Mac 终端执行一次重签名去除限制（不影响微信正常使用）：
    ```bash
    sudo codesign --force --deep --sign - /Applications/WeChat.app
    ```
  - **智能体必须遵循的异步协同交互规范（重要！Mac 微信退出账号与监听规范）**：
    微信 4.1+ 仅在**扫码/确认登录的瞬间**调用系统加密库派生密钥。在后台常驻运行时无法捕获。
    ⚠️ **核心操作区分（重中之重）**：
    - ✅ **正确操作**：在 Mac 微信主界面点击**「设置」->「退出」->「退出账号」**（停留在微信扫码/切换账号界面），或者点击微信界面左下角图标弹出菜单选择**「退出账号」**。此时微信进程保持运行，断点稳固监听！
    - ❌ **严重误区**：绝对不能按 `Cmd + Q` 或在 Dock 栏右键选择「退出微信」，这会导致微信整个软件进程被彻底杀掉；
    - **标准交互流程**：
      1. **后台拉起持续监听**：智能体在后台静默启动 `wechat-init`（默认持续监听）：
         ```bash
         nohup wechat-init > /tmp/wechat-init.log 2>&1 &
         ```
      2. **当场向用户发出精准提示**：
         > 💡 **“我已在后台开启密钥监听。请在 Mac 微信主界面操作：点击左下角「设置」->「账号与存储」->「退出登录」（注意是退出当前账号，切勿使用 Cmd+Q 退出整个微信软件），然后掏出手机重新扫码登录。登录完成后请回复我，我为您检查并完成解密！”**
      3. **等待用户确认**：用户回复已重新扫码登录后，检查 `/tmp/wechat-init.log` 验证 passphrase 捕获与解密。
      4. **进程漂移与容错保障**：底层已内置 PID 自动巡检（若用户不慎重启导致 PID 变更会精准提醒）、常驻 stdin 保活（后台启动绝不因 EOF 退出）以及优雅退出保护（绝不误杀微信）。
  - **⚠️ 智能体绝对禁令：严禁手动清理 lldb / debugserver 进程（真机实测血泪教训）**
    本节为 macOS 调试子系统的高危操作区，智能体必须严格遵守以下规则，否则会**直接杀死用户的微信进程**：

    | 操作 | 后果（真机实验结论） |
    | :--- | :--- |
    | `kill -TERM <debugserver pid>` | ❌ **微信立即被连带 SIGKILL 强杀** |
    | `kill -9 <debugserver pid>` | ❌ **微信立即被连带 SIGKILL 强杀** |
    | `pkill -f "LLDB.framework.*debugserver"` | ❌ **微信立即被连带 SIGKILL 强杀**（`-TERM` 也会！） |
    | `kill -TERM <lldb 前端 pid>` | ✅ 安全，tracee 存活 |
    | `kill -9 <lldb 前端 pid>` | ✅ 安全，tracee 存活 |

    **核心结论**：
    - **向 debugserver 发送任何信号（无论 SIGTERM 还是 SIGKILL）都会导致 macOS 调试子系统连带 SIGKILL 掉 tracee（微信）。** 历史注释只警告了 `-9`，但 `-TERM` 同样致命。
    - **唯一安全的清理对象是 lldb 前端进程**，因为它退出时会自行优雅关闭 debugserver。
    - 因此 `wechat-init` 的 `_cleanup_lldb_orphans_macos()` **已刻意完全不触碰 debugserver**，并且在 tracee 仍存活时**直接跳过所有清理**。

    **智能体行为要求**：
    1. **禁止**在 `wechat-init` 运行期间或刚结束后，自行执行 `pkill lldb`、`pkill debugserver`、`kill -9` 等任何清理命令；
    2. **禁止**为了“清理环境”而提前终止 lldb —— 必须等 `wechat-init` 主进程自行退出（`pgrep -f bin/wechat-init` 为空）后再做任何判断；
    3. 若发现残留 lldb 孤儿进程，**默认保持不动**。它只是惰性进程，不影响微信使用；如确需清理，仅可执行 `pkill -x lldb`（**绝不加 `-9`，绝不针对 debugserver**），且必须先确认用户已退出微信或明确授权；
    4. 若微信界面“意外消失”，先查证再下结论：
       ```bash
       # 判断是否为 SIGKILL 强杀（第 3 个字段为 9）
       log show --last 10m --predicate 'eventMessage CONTAINS "WeChat"' --style compact | grep "termination reported"
       ```
       `termination reported by launchd (2, 9, 9)` 表示 **SIGKILL 强杀**，且**不会产生崩溃报告**（`SIGKILL` 无法被捕获）。切勿把“无崩溃报告”误判为“未崩溃”。
  - **⚠️ 断点冻结防护机制（已内置，智能体无需干预）**：
    历史上 `wechat-init` 存在两个会导致微信卡死/崩溃的缺陷，现已修复：
    1. **断点冻结**：旧实现用 lldb 命令块执行 `memory read` + `process continue`。因 lldb 默认 `stop-command-source-on-error=true`，一旦 `memory read` 失败（微信在独立线程调用 PBKDF2，`$rsi` 指向栈上临时缓冲区），会中止命令块剩余命令，导致 `process continue` 永不执行，微信被永久冻结。**现改为 Python 断点回调 + `try/finally` 无条件 `proc.Continue()`**，并叠加 12 秒冻结看门狗。
    2. **收尾竞争**：旧实现在 lldb detach 尚未完成时就 `pkill`，会让 tracee 停在断点指令（`int3`）上并触发 `EXC_BREAKPOINT/SIGTRAP` 崩溃。**现改为 `_wait_lldb_exit()` 确认 detach 完成后再清理，并先 `_resume_tracee_if_stopped()` 恢复暂停态**。
    智能体遇到上述报错时应**直接重试或改用 `--passphrase`**，切勿自行修改源码或手动干预进程。
  - **稳定性验证结论（可复用）**：同一微信账号在同一台机器上，多次重新登录捕获到的 **passphrase 恒定不变**（实测 4 次完全一致）。因此**无需反复让用户退出登录来“验证稳定性”**：只要一次捕获通过 HMAC 自证（日志出现 `passphrase 捕获并自证通过`），即可确认成功并进入解密流程。**反复要求用户退出/登录微信存在账号风控风险，必须避免。**
  - **手动传参降级**：若用户已有 64 位十六进制 passphrase，支持直接传入跳过断点捕获：
    ```bash
    wechat-init --passphrase <64位十六进制passphrase>
    ```
  捕获或填入口令后，会自动持久化到 `~/.wcdb-key-tool/wechat-passphrase.json`，后续增量同步均无需重复抓取。工具会自动调用系统原生 `CommonCrypto`（`CCCrypt`）高效派生密钥并完成多库解密与 WAL 合并。
- **微信运行前置检查（禁止盲目扫描）**：
  - **重要约束**：**如果找不到微信进程或缺少密钥原料，智能体切勿自行尝试猜测扫描系统无关进程或遍历系统全局目录！**
  - **正确处置**：应主动向用户明确说明当前环境状态，引导用户准备环境或填入 passphrase。
- **权限判定**：若 Windows 报错 `Access Denied`，向用户说明原因并建议以管理员身份执行。
- **成功标志**：在用户数据目录 `~/.wechat_mcp/decrypted/` 下生成解密数据库，并在 `~/.wechat_mcp/app.db` 下完成 FTS5 索引清洗。若仅需重跑数据清洗与索引，可执行 `wechat-init --etl-only`。

---

### 2.2 任务执行中：MCP 工具调用规范

在初始化成功后，智能体应优先通过 MCP 工具检索，避免直接使用裸 SQL 查询：

#### 微信 (wechat) 推荐调用顺序：
1. `list_wechat_sessions(limit=20)`: 先列出最近活跃的会话/群聊，拿到关键的 `session_id`（如 `xxx@chatroom` 或微信号）。
2. `search_wechat_messages(query="关键词", limit=50)`: 全局全文检索聊天内容（支持 `offset` 翻页）。
3. `get_session_history(session_id="...", limit=100)`: 获取目标会话的上下文消息流。
4. `get_message_image(message_id=...)`: **聊天图片按需解密**。当搜索或历史消息中包含 `[图片]` 时，使用对应的消息 `id` 调用此工具。
   - 默认模式：返回解密后图片的本地物理绝对路径（缓存至 `~/.wechat_mcp/images/`），**杜绝因大图 Base64 直接撑爆 Agent 上下文**；
   - 多模态看图模式：如当前 Agent 需要视觉看图分析，可设置 `include_base64=True` 提取 Base64 字符串供模型理解。
5. `search_shared_files(keyword="...", limit=20)`: 定位群内或私聊传输的文件路径。

微信侧另提供：`get_messages_by_date` / `get_messages_by_range`（按日期或时间范围取数）、`search_contacts`（联系人反查）、`list_chatroom_members`（群成员名单）、`search_media_files`（图片/视频磁盘路径）、`list_favorites`（收藏）、`list_emoticons`（表情）、`get_chat_activity_stats`（活跃度统计）、`get_persona_style`（沟通风格蒸馏）、`get_sync_status`（索引新鲜度）。其中 `get_messages_by_date` / `get_messages_by_range` 与 `search_wechat_messages` 一样支持 `offset` 翻页。

#### 钉钉 (dingtalk) 推荐调用顺序：
1. `list_dingtalk_conversations(limit=20)`: 获取最近群聊与私聊列表（已自动映射最新实名及群名）。
2. `search_dingtalk_messages(keyword="关键词", limit=50)`: 跨 128 个分表聚合检索文本。
3. `get_dingtalk_messages(cid="...", limit=100)`: 查看特定钉钉群或私聊的对话细节。
4. `list_dingtalk_contacts(keyword="姓名/拼音")`: 快速反查联系人 UID 与真实姓名。

---

### 2.3 版本升级与更新维护 (Project Upgrade & Version Maintenance)

当用户提出“更新 im-mcp”、“升级微信/钉钉 MCP”、“拉取最新修复”或发现新特性/Bug 修复发布时，智能体应根据当前的安装方式执行平滑升级：

#### 1. 全局持久化安装模式下的升级（推荐）：
如果用户此前采用 `uv tool install` 进行安装，使用 `uv tool update` 或重新指定 Git 仓库升级：
```bash
# 标准全局一键更新
uv tool upgrade im-mcp

# 或强制以最新远程仓库版本重新覆盖安装（支持最新 Git HEAD）
uv tool install --force "git+https://github.com/stkoecn/im-mcp.git" --with "im-mcp[all]"

# 国内网络环境（Gitee 源）
uv tool install --force "git+https://gitee.com/ws4m/im-mcp.git" --with "im-mcp[all]"
```

#### 2. 即席运行模式 (`uvx`)：
如果是在沙箱中通过 `uvx` 运行，`uvx` 默认可能缓存旧版本 Wheel。执行升级或强制刷新版本缓存：
```bash
# 清理 uvx 临时工具缓存以确保拉取最新 Git 代码
uv cache clean im-mcp

# 或在下一次调用 uvx 时追加 --refresh 参数强制拉取最新版本
uvx --refresh --from "git+https://github.com/stkoecn/im-mcp.git[all]" <command>
```

#### 3. 升级后平滑重新加载：
升级完成后，智能体应主动提醒用户刷新或重新加载客户端，以加载最新版本的 MCP 服务定义与工具集：
- **Pi / Pebrel**：提示输入 `/reload`；
- **Claude Desktop / Cursor / WorkBuddy**：提示重启客户端或重新连接 MCP 服务器；
- 如本次升级涉及底层数据库清洗逻辑变更，可执行 `wechat-init --etl-only` 在 2 秒内无损重建最新 FTS 索引，无需耗时重新提取或解密。

---

### 2.4 任务结束后：数据隐私与存储清理 (Post-flight Cleanup)

在完成周报、工时汇总或用户明确要求“清理环境”、“删除导出的聊天记录”时，**智能体应主动调用 `im-clean`**：

#### 预览待清理文件（只读安全检查）：
```bash
im-clean --dry-run
```

#### 彻底执行清理：
```bash
# 使用 -y 参数静默清理所有导出的明文 SQLite、密钥及中间缓存（支持自动检测并处置常驻进程锁）
im-clean -y

# 若遇到残留句柄锁定，可追加 --force 强制终止 MCP 服务并完成彻底删除
im-clean -y --force
```

#### 仅清理特定平台数据：
```bash
# 仅清理微信数据，保留钉钉
im-clean --scope wechat -y

# 仅清理钉钉数据，保留微信
im-clean --scope dingtalk -y
```

### 2.5 完全卸载与客户端清理协同 (Full Uninstallation Guidance)

当用户明确提出“完全卸载这个 MCP”、“彻底删除聊天工具及配置”时，智能体应协助用户完成以下层级的标准顺序清理，并**主动告知用户哪些步骤需要其在客户端界面手动操作**：

1. **退出客户端或停止服务**：提醒用户先退出当前 AI 客户端，或由 `im-clean -y --force` 自动处理残留 MCP 进程；
2. **自动清理数据**：智能体执行 `im-clean -y --force` 彻底清除 `~/.wechat_mcp` 与 `~/.dingtalk_mcp`（确保返回码为 0，若非 0 严禁谎报成功）；
3. **自动卸载依赖**：智能体执行 `uv tool uninstall im-mcp` 或 `uv cache clean` 清理环境与缓存；
4. **主动向用户输出客户端手动清理提醒**：
   - 智能体应根据当前宿主客户端，提示用户手动清理配置文件（具体各环境常见路径可参考本技能文末的【附录：主流 Agent 宿主环境参考目录】）。

---

## 3. 常见故障处理与智能体行为准则 (Troubleshooting & Agent Rules)

1. **核心禁令：报错立即停下，严禁擅自分析代码或盲目折腾**：
   - 如果在安装、初始化或测试调用过程中遇到任何未知报错、环境冲突或执行异常：
   - **绝对禁止**：智能体**严禁擅自深入翻查源码、自行写 patch 补丁修代码、反复猜测重试或盲目排查系统环境**；
   - **必须执行**：**立刻停止在报错步骤**，原样展示核心错误摘要，并明确询问用户：
     > *“当前步骤遇到错误：`[错误简述]`。请问您是希望排查自行解决问题，还是停止操作不再使用？”*
   - 把控制权交给用户，用户确认继续或退出后再行动。

2. **安装初始化后即刻交付用户，严禁擅自编写代码模拟协议反复测试**：
   - 智能体在完成工具安装（如 `uv tool install`）、配置文件写入以及 `init` 初始化后，**整个部署流程就已经全部结束**。
   - **绝对禁止**：智能体**私自编写 Python/Node 测试脚本去模拟 JSON-RPC 2.0 握手、管道通信或探测协议内部细节**。这不仅徒增 Token 消耗与不可控的意外报错风险，还剥夺了用户的实时感知与控制权。
   - **必须执行**：在安装和初始化完成后，**直接交付并给出最简明友好的使用引导**：
     1. 明确提示用户在当前客户端中**启用/信任**该 MCP（例如提示 Pi 用户执行 `/reload`，Claude Desktop / Cursor 用户重启客户端生效）；
     2. 给出 1~2 个简单、直观的自然语言示例引导用户体验，例如：
        > *“安装与初始化已就绪！请您在客户端中确认信任/开启该 MCP（如执行 `/reload`），之后直接对我说类似这样的话即可开始使用：*
        > *👉 ‘帮我查一下最近项目群里聊了什么’*
        > *👉 ‘找一下钉钉里成鹏发过的报表统计结果’*
        > *👉 ‘汇总一下上周关于某某需求的沟通记录做周报’”*

3. **客户端与应用授权信任（宿主环境自主识别与启用指引）**：
   - Agent **必须通过当前会话特征自主识别出当前用户所使用的客户端宿主**；
   - 严禁含糊地输出“在您的客户端里启用”，而**应根据当前宿主给出对应客户端的具体生效方式**（如提示 Pi 输入 `/reload`，或提示桌面应用重启生效，具体特征可参考文末附录）。

4. **找不到微信/钉钉进程时的行为禁忌**：
   - **严禁**自行在宿主机上执行耗时且侵入式的全盘暴力扫描或猜测其他系统进程；
   - **必须**直接暂停，并友好向用户确认客户端登录状态。

5. **微信增量消息同步问题**：
   微信客户端在线时，新消息会写入原加密库。如需获取刚刚产生的新消息，智能体可提示用户“重新执行 `wechat-init`”以更新本地解密库。

6. **钉钉 WAL 锁问题**：
   `dingtalk-init` 已内置自动拷贝机制，即使钉钉正在运行且处于 WAL 事务中，也不会发生 SQLite 锁库冲突。

7. **隐私安全原则**：
   解密后的 SQLite 数据库属于敏感本地隐私，在公共机器或多租户环境下，工作结束后务必引导或自动执行 `im-clean -y`。

8. **部署反馈报告与社区自动开发机制 (Deployment Feedback & Auto-Development)**：
   - 无论部署与初始化是**成功**还是**遇到失败**，智能体在流程结束时，都**应主动整理并为用户生成一份结构化、简明扼要的《部署与运维反馈简报》**，并提示用户可一键复制并前往 GitHub 提交 Issue。
   - ⚠️ **核心高亮原则（必须重点标记，严禁含糊）**：
     - **在报告标题和第一行状态栏必须使用极其醒目的视觉标识（如 `【状态：🟢 部署成功 / SUCCESS】` 或 `【状态：🔴 部署受阻 / FAILED】`）**；
     - 坚决杜绝模棱两可、中立叙述，让提问者与排查智能体一眼就能看穿是成功还是失败。
   - **智能体自动化运维承诺**：提示用户：“*本项目已接入 GitHub Agent 自动化协作管道，您提交的 Issue 会有专职智能体自动进行复现排查、代码修复并合并提交！*”
   - **简报生成规范模板**：
     - **若成功 (SUCCESS)**：
       ```markdown
       ### 📋 im-mcp 部署反馈报告 【状态：🟢 部署成功 / SUCCESS】
       > **最终结论**：✅ 全部依赖安装成功，数据提取与索引同步完成，服务已具备即时调用能力！

       - **核心状态**：🟢 SUCCESS
       - **环境信息**：OS版本（如 Win11 23H2）、客户端宿主（如 WorkBuddy / Pi / Claude Desktop）、Python 及 uv 版本
       - **安装模式**：`uv tool install` / `uvx` / 本地源码
       - **初始化指标**：耗时（如 微信 12s / 钉钉 3s）、联系人/会话/消息同步条数
       - **使用建议**：冷启动体验、增量同步指引或下一步功能期望
       - **反馈入口**：可前往 [GitHub Issues](https://github.com/stkoecn/im-mcp/issues) 提交经验，社区 Agent 将自动跟踪优化！
       ```
     - **若失败或遇阻 (FAILED)**：
       ```markdown
       ### ⚠️ im-mcp 部署反馈报告 【状态：🔴 部署受阻 / FAILED】
       > **最终结论**：❌ 流程在中途受阻中断，核心服务暂无法正常启动，请参考以下故障定位。

       - **核心状态**：🔴 FAILED
       - **受阻阶段**：依赖安装 / 微信密钥提取 / 钉钉解密 / 数据库索引构建 / 客户端挂载
       - **环境特征**：OS版本、客户端宿主、微信版本（如 4.x / 3.x）、钉钉版本（如 7.x V3）
       - **关键报错摘要**：截取核心错误堆栈或系统提示（如 Access Denied / 进程未运行 / 文件锁拒绝）
       - **已尝试措施**：是否提权、是否尝试过 `--force` 或 `--etl-only`
       - **改进建议**：指出当前流程中不够顺畅或需工具自动容错的环节
       - **一键反馈**：请直接复制本报告前往 [GitHub Issues](https://github.com/stkoecn/im-mcp/issues) 提交，仓库专职智能体将自动分析日志并开展修复！
       ```

---

## 4. 附录：主流 Agent 宿主环境参考目录（仅供参考）

> 💡 **声明**：本目录收录当前各主流 AI 客户端/智能体环境的默认行为与配置特征，**仅供参考**。各平台版本更新迭代后可能存在调整，请以对应平台最新官方文档为准。

| 客户端环境 | 典型识别特征 | 配置 / 启用方式参考 | 卸载与清理方式参考 |
|---|---|---|---|
| **Pi / Pebrel** | 存在 `PI_*` 环境变量或 `~/.pi/` 目录 | 编辑 `~/.pi/agent/mcp.json`，在会话中输入 `/reload` 重新加载生效 | 从 `mcp.json` 移除条目，删除 `~/.agents/skills/im-mcp`，输入 `/reload` |
| **Claude Desktop** | 本地 AppData 存在 Claude 配置 | 编辑 `%APPDATA%\Claude\claude_desktop_config.json`，**完全重启客户端应用** | 从配置文件的 `mcpServers` 移除条目并重启软件，清理技能目录 |
| **Cursor** | VS Code 衍生，存在 Cursor 配置 | 在 `Settings` ➔ `Features` ➔ `MCP Servers` 添加，界面即时挂载 | 在 MCP 服务器列表点击对应条目的 **Delete / Remove** |
| **Cline / Roo Code** | VS Code 扩展面板 | 在扩展侧边栏 MCP 选项卡中添加配置，切换为“Enabled”激活并授权 | 在侧边栏 MCP 列表中关闭开关或点击垃圾桶图标移除 |
| **Cherry Studio** | 桌面端多模型管理工具 | 在 `设置` ➔ `MCP 服务器` 中添加 stdio 命令，保存即生效 | 在 MCP 服务器列表选中并点击“删除”，保存设置 |
| **WorkBuddy** | 存在 `~/.workbuddy/` 目录或相关配置 | 编辑 `~/.workbuddy/mcp.json`，在连接器管理界面手动点击“信任/启用” | 从 `mcp.json` 移除配置块并在界面移除连接器 |
