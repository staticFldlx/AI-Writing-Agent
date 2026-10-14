# DiceBot 写作助手

一个面向小说 / 安科（掷骰推进剧情）创作的桌面工具：把世界观设定存进结构化知识库，
让 AI 在写作讨论中自动调用这些设定，并可在你批准后直接维护知识库。附带一个骰子面板。

Python 3.13 + CustomTkinter + SQLite，仅用标准库访问 AI 接口（无 requests / 无向量库），
PyInstaller 打包为单个 exe，目标机器无需安装 Python。

## 功能

**📚 设定库** — 结构化词条（名称/类别/正文/标签/别名/来源/日期）存于 SQLite。
按类别分组的可折叠侧栏；关键字检索基于 FTS5 trigram 分词（对中文友好），
支持类别与日期筛选、按名称/别名精确检索。

**📥 录入** — 从 txt / md / docx 或粘贴的对话记录中，让 AI 提取成结构化词条提案。
长文自动分批（可配置每批字数、失败可从断点续传）；提案逐条可编辑，
与已有词条重名时标记为「疑似已有」并可选择 AI 辅助增补合并，确认后才入库。

**✍️ 写作讨论** — 多轮对话，自动为每一轮注入相关上下文：

- **自动召回**：把词条目录（标题+摘要）交给 AI 挑出与本轮相关的条目，
  因此「那个红头发的女孩」也能命中，无需手动勾选（勾选降级为强制包含）。
- **设定总览**：AI 归纳的世界观总览，开启「整体讨论」时注入以替代逐条召回；
  词条变动后按指纹自动判定过期并提示刷新。
- **前情提要**：导入前文 AI 浓缩，可在既有提要上增量更新。
- **长期记忆**：AI 自动记录跨会话值得保留的信息，与设定库分开存放，可手动增删改。
- **对话存档**：会话可新建 / 重命名 / 删除 / 切换续聊，消息持久化。

**🤖 Agent 工具调用** — AI 通过 function calling 直接操作设定库：
读工具（检索 / 读取 / 列类别）自动执行；**写工具（新建 / 修改 / 删除）一律先弹审批框**，
由你逐条勾选批准后才落库，拒绝的结果会回填给模型让它调整措辞。
关窗或超时按全部拒绝处理，后台线程不会挂死；可一键关闭 AI 的写权限。

**🎲 骰子** — 1d1～1d100，支持"仅范围判定"与"随机选项"（区间起点自动衔接，
杜绝重叠与缺口），命中项高亮，可写掷骰日志，并按人格生成 AI 点评。

## 运行

```bash
pip install customtkinter
python app.py
```

首次运行需在「AI 设置」中填写 OpenAI 兼容接口的地址、密钥与模型名，例如：

| 服务商 | API 地址 | 模型名示例 |
|---|---|---|
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-5.2` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-3.5-flash` |

> API 地址只填到基址即可，程序会自动拼接 `/chat/completions`。

其他入口：`python main.py`（仅骰子）、`python writer_app.py`（仅写作助手）。

## 打包

```bash
pip install pyinstaller
powershell -ExecutionPolicy Bypass -File build.ps1
```

产物 `dist/DiceBot.exe` 约 30 MB，单文件分发。

## 数据存放

所有用户数据都在 `%APPDATA%\DiceBot\`，不随程序分发：

| 文件 | 内容 |
|---|---|
| `config.json` | 接口地址、**API 密钥**、模型、人格、各项开关 |
| `knowledge.db` | 设定库词条（含前情提要、设定总览） |
| `chat.db` | 对话会话、消息、长期记忆 |
| `context_cache.json` | 长文档浓缩稿缓存 |
| `ai_error.log` | AI 请求失败的完整诊断日志 |

## 模块结构

| 文件 | 职责 |
|---|---|
| `ai_client.py` | OpenAI 兼容接口封装：重试与退避、JSON 输出模式、function calling、浓缩、配置与密钥管理 |
| `knowledge.py` | 设定库数据层（SQLite + FTS5，线程安全） |
| `chatstore.py` | 对话会话、消息、长期记忆的持久化（独立 `chat.db`） |
| `ingest.py` | AI 提取词条、去重分类、增补合并、分批续传 |
| `retrieve.py` | 自动召回：目录构造、AI 选条、粗筛与字面兜底 |
| `memory.py` | 从对话中提取长期记忆条目 |
| `agent_tools.py` | Agent 工具定义与执行（读写分离，写操作交调用方审批） |
| `dice.py` / `log_manager.py` | 掷骰逻辑与掷骰日志 |
| `theme.py` | 共享配色与控件工厂 |
| `ui.py` / `writer_ui.py` | 骰子面板与写作助手界面 |

数据层与 AI 逻辑均无 GUI 依赖，可脱网单测。

## 测试

```bash
python test_knowledge.py   # 设定库：CRUD、中文检索、去重、总览指纹
python test_logic.py       # 掷骰、日志、AI 客户端、录入、召回、记忆、Agent 工具
python smoke_ui.py         # 骰子界面冒烟（mock AI）
python smoke_writer.py     # 写作助手全链路冒烟（mock AI）
```

测试全部使用临时目录，不会读写 `%APPDATA%` 中的真实配置与数据。

## 设计要点

- **AI 调用一律在后台线程**，经队列回主线程更新界面；任何异常都会被捕获并写入日志，
  不会让界面卡在忙碌状态。
- **写操作 human-in-the-loop**：agent 线程用 `threading.Event` 阻塞等待用户审批，
  超时按拒绝处理；工具执行结果（含被拒绝）回填给模型形成闭环。
- **对思考型模型的适配**：给足 `max_tokens`、空内容检测、JSON 被截断时用括号栈
  抢救已完整的对象、5xx 指数退避重试。
- **端点能力自适应**：JSON 输出模式或 tools 遇到 400 时自动降级，功能不中断。
