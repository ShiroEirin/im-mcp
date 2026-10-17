---
name: im-mcp
description: Windows 本地微信 (WeChat) 与钉钉 (DingTalk) 聊天记录、通讯录、群聊与文件检索 MCP 服务运维管理指南。当需要初始化离线数据库、提取微信解密密钥 (wechat-init)、离线同步钉钉数据库 (dingtalk-init)、检索聊天历史/周报/工时，或在一键彻底清理导出的解密数据与缓存 (im-clean) 时使用。
---

# IM-MCP 智能体运维与数据生命周期管理指南

本技能指导智能体在 Windows 环境下管理 `im-mcp` 工具集（包含微信 MCP 与钉钉 MCP），涵盖**前置环境检查、离线数据库初始化与解密、聊天数据检索以及任务完成后的安全清理**。

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
| **`wechat-init`** | `wechat-init` | `uvx --from "git+https://github.com/stkoecn/im-mcp.git[all]" wechat-init` | 提取微信 DBKey 并离线解密为 SQLite 数据库 | **需管理员权限** (读取微信内存) | `~/.wechat_mcp/decrypted/`, `~/.wechat_mcp/app.db` |
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
微信需要从正在运行的客户端进程内存中提取密钥并执行校验：
```bash
# 智能体执行初始化与全量解密
wechat-init
```
- **微信运行前置检查（禁止盲目扫描）**：**微信客户端必须处于启动并已登录状态**。若微信未启动，`wechat-init` 会报错 `RuntimeError: Weixin.exe 未运行`。
  - **重要约束**：**如果找不到微信进程，智能体切勿自行尝试猜测扫描系统无关进程或遍历目录！**
  - **正确处置**：应立即主动向用户明确说明：“未能检测到正在运行的微信进程，请确认 Windows 微信客户端是否已登录就绪”。待用户准备好环境后再重新执行 `wechat-init`。
- **权限判定**：若报错 `Access Denied` 或无法扫描内存，向用户说明原因并建议：“提取微信密钥需要扫描进程，请允许我以管理员身份执行终端命令，或请您在管理员 PowerShell 中运行一次 `wechat-init`”。
- **成功标志**：在用户数据目录 `~/.wechat_mcp/decrypted/` 下生成解密数据库，并在 `~/.wechat_mcp/app.db` 下完成 FTS5 索引清洗。若仅需重跑数据清洗与索引，可执行 `wechat-init --etl-only`。

---

### 2.2 任务执行中：MCP 工具调用规范

在初始化成功后，智能体应优先通过 MCP 工具检索，避免直接使用裸 SQL 查询：

#### 微信 (wechat) 推荐调用顺序：
1. `list_wechat_sessions(limit=20)`: 先列出最近活跃的会话/群聊，拿到关键的 `session_id`（如 `xxx@chatroom` 或微信号）。
2. `search_wechat_messages(query="关键词", limit=50)`: 全局全文检索聊天内容（支持 `offset` 翻页）。
3. `get_session_history(session_id="...", limit=100)`: 获取目标会话的上下文消息流。
4. `search_shared_files(keyword="...", limit=20)`: 定位群内或私聊传输的文件路径。

微信侧另提供：`get_messages_by_date` / `get_messages_by_range`（按日期或时间范围取数）、`search_contacts`（联系人反查）、`list_chatroom_members`（群成员名单）、`search_media_files`（图片/视频磁盘路径）、`list_favorites`（收藏）、`list_emoticons`（表情）、`get_chat_activity_stats`（活跃度统计）、`get_persona_style`（沟通风格蒸馏）、`get_sync_status`（索引新鲜度）。其中 `get_messages_by_date` / `get_messages_by_range` 与 `search_wechat_messages` 一样支持 `offset` 翻页。

#### 钉钉 (dingtalk) 推荐调用顺序：
1. `list_dingtalk_conversations(limit=20)`: 获取最近群聊与私聊列表（已自动映射最新实名及群名）。
2. `search_dingtalk_messages(keyword="关键词", limit=50)`: 跨 128 个分表聚合检索文本。
3. `get_dingtalk_messages(cid="...", limit=100)`: 查看特定钉钉群或私聊的对话细节。
4. `list_dingtalk_contacts(keyword="姓名/拼音")`: 快速反查联系人 UID 与真实姓名。

---

### 2.3 任务结束后：数据隐私与存储清理 (Post-flight Cleanup)

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

### 2.4 完全卸载与客户端清理协同 (Full Uninstallation Guidance)

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
