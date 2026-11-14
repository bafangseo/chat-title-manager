# WorkBuddy任务对话标题-聊天标题规范

一个 Agent Skill，统一处理两件事：**新聊天自动命名、历史聊天标题批量规范**。

```text
MMDD｜类型-核心事件
```

例如：

```text
1026｜写作-产品介绍文案
1026｜开发-用户登录功能
1026｜修复-登录页面白屏
1026｜插件-L4D2
1026｜MOD-单机游戏
```

类型严格限定为：**写作、开发、设计、研究、学习、规划、修复、插件、MOD、其他**。其中 `插件`、`MOD` 是**领域类，领域优先**——动 SourceMod 插件（`.sp`/`.smx`/spcomp/ConVar）用 `插件`，动游戏模组（MOD 玩法、人物与武器 vpk、模组美术、创意工坊内容）用 `MOD`，即使这次是修故障或调数值也归领域类。日期使用每个聊天在 `Asia/Shanghai` 时区的创建日期；核心事件简短、具体，避免“帮我看看”“继续”等模糊表达。

## 来源

本技能有两处来源，请按需区分：

| 来源 | 地址 | 内容 |
| --- | --- | --- |
| **上游原版** | https://github.com/cxcxy/chat-title-manager | 「MMDD｜类型-核心事件」命名规则、新聊天自动命名流程、历史批量规范六步法 |
| **本版 v2.1.0** | https://github.com/bafangseo/chat-title-manager | 在上游基础上新增 `scripts/title.py` 执行层：节流、同题跳过、手动改名保护；类型表新增 `插件`/`MOD` |

命名规则与工作流来自上游原版；执行层脚本、平台元数据与本版文档由本仓库维护。二次分发请同时保留本表。

## 设计参考

执行层的三道保护——**节流、同题跳过、手动改名保护**——问题清单与设计思路参考自
[weibaohui/dsh-smart-title](https://github.com/weibaohui/dsh-smart-title)：一个 DeepSeek Harness（`@deepseek-ai/dsh`）的会话智能标题插件，
它用 LLM 每轮总结刷新标题，内置 `refreshMinIntervalMs` 刷新节流、长会话冻结、同题静默跳过与手动改名保护。

本版**没有**引入它的插件机制——那是 dsh 宿主的进程内插件，依赖 `sessionTitle` 服务与 `ctx.llm`，换到别的 Agent 跑不起来。
这里只借用它验证过的问题清单，按本技能的 `MMDD｜类型-核心事件` 规范重新实现成 `scripts/title.py` 的三道闸。

## 安装

在 WorkBuddy 里：`专家·技能·连接器 → 技能 → 添加技能 → 上传技能`，选技能 ZIP 即可；从市场安装则直接搜「聊天标题管家」。

也可以直接克隆到用户技能目录：

```bash
git clone https://github.com/bafangseo/chat-title-manager.git ~/.workbuddy/skills/chat-title-manager
```

其他 Agent（以 Codex 为例），把目标路径换掉即可：

```bash
git clone https://github.com/bafangseo/chat-title-manager.git "${CODEX_HOME:-$HOME/.codex}/skills/chat-title-manager"
```

如果已有同名目录，先核对现有来源，不覆盖安装。安装后新开聊天，搜索“聊天标题规范”或输入 `$chat-title-manager`。

其他支持文件式 Skill 的 Agent，可将整个仓库目录安装到其 Skill 目录，入口为 [SKILL.md](SKILL.md)。实际改名需要应用提供原生聊天读取与标题修改工具。

## 1. 新聊天自动命名

对 Agent 说：

```text
使用 $chat-title-manager，让以后每个新聊天都按统一格式自动命名。
```

Agent 应保留已有全局指令，将本 Skill 的新聊天模式接入其启动入口。在首条实际任务明确后、首轮回复结束前，设置当前聊天的标题并读回核对。

安装 Skill 本身不等于已经启用全局规则。接入后需要新建一条真实聊天，确认是否按规则命名；没有全局启动入口的应用，需在新聊天中手动调用。继续旧聊天时不重置创建日期，也不因每次追问反复改名。

## 2. 历史记录批量规范

对 Agent 说：

```text
使用 $chat-title-manager，预览当前应用全部可访问历史聊天标题的规范化结果。
```

也可以限定范围：

```text
使用 $chat-title-manager，只整理当前项目的历史聊天，先给出完整新旧标题对照。
```

处理顺序：**完整枚举 → 完整预览 → 用户确认 → 保存快照 → 执行改名 → 逐项读回**。

- 默认覆盖当前应用内可访问的有项目、无项目、置顶与归档聊天；用户指定范围时按指定范围处理。
- 接口只能返回部分记录时，说明覆盖范围，不把最近若干条称作全部历史。
- 缺少可靠创建时间、主题不明确、原标题已发生变化或聊天不可访问时，跳过并说明。
- 快照保存在运行用户的 `~/.chat-title-manager/history/`，只保存恢复标题所需的元数据。
- 只修改标题，不改变项目、正文、归属、排序、置顶或归档状态。

首次要求“整理历史标题”会先生成预览；确认前不执行批量修改。

## 3. 执行层：节流 / 同题跳过 / 手动改名保护

`scripts/title.py` 是给 sqlite 会话库后端用的执行层（本机即 WorkBuddy）。改名不要手写 SQL，走它：

```bash
python scripts/title.py set --cwd "<工作区绝对路径>" --title "1113｜开发-某功能"
python scripts/title.py status        # 现标题 / 是否钉住 / 节流余量
python scripts/title.py pin --note "人工定稿"
python scripts/title.py unpin
```

三条保护由代码强制：

- **节流**：同一会话 120 秒内不重复写库（`--interval` 可调，`--force` 越过），返回 `skipped:throttled`。
- **同题跳过**：归一化后与现标题一致就不写，按成功处理，返回 `skipped:same-title`。
- **手动改名保护**：库中标题与上次本工具写入的值不一致，即判定为人工修改，自动钉住并拒绝覆盖，返回 `skipped:manual-edit`；钉住不会被 `--force` 越过，须 `unpin`。

写库前自动快照到 `~/.chat-title-manager/history/`，写后用独立只读连接读回核对，只有 `renamed` 才算成功。

## 能力边界

本仓库提供工作流程、命名规则，以及针对 sqlite 会话库的可选执行层（`scripts/title.py`）。其他应用取决于其是否提供原生改名工具：有则用原生工具，无则只输出建议标题或新旧对照，并明确说明未执行。

## 致谢

- [@cxcxy](https://github.com/cxcxy)——上游原版 [chat-title-manager](https://github.com/cxcxy/chat-title-manager)：`MMDD｜类型-核心事件` 命名规则、新聊天自动命名流程、历史批量规范六步法。
- [@weibaohui](https://github.com/weibaohui)——[dsh-smart-title](https://github.com/weibaohui/dsh-smart-title)：节流、同题跳过、手动改名保护三道闸的问题清单与设计思路来源。

本仓库的问题定位、脚本实现与文档由 [@bafangseo](https://github.com/bafangseo) 维护。

本地结构与安装入口校验不代表所有应用均已完成真实新聊天或历史批量操作验证。
