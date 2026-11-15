# AI桌宠助手

基于 LLM 的桌面宠物：FastAPI 后端 + Electron 桌宠 + Python 启动器。形象可自由切换，对话、干活、看屏幕、文档问答一体。

## 功能

- **桌宠互动**：戳 / 拖 / 喂食，四个性格数值（混乱 / 魔性 / 存在感 / 抽象），反应由 LLM 按场景生成，无固定台词
- **流式聊天**：DeepSeek 主 + 千问备用双通道，边生成边显示；问"看见 / 屏幕 / 在干嘛"自动带截图走多模态视觉
- **主动互动**：定时随机推送台词 / 动作（SSE 实时推送），场景提示词交给 AI 自由生成
- **统一智能体循环**：输入框每条消息都进 DeepSeek function-calling 循环——纯聊直接回，要干活先 💭思考 再 🔧调工具，写类 / 命令类动手前弹确认气泡，主人点「放行」才执行。13 个工具（看目录 / 读文件 / 搜文件 / 搜内容 / 写 / 改 / 移动 / 改名 / 删 / 跑命令 / 提醒 / 速记 / 查时间），读类全盘可达、写类先确认
- **工具台**：对话模式 🤖 页可直接调用读类工具与生活助手（看目录 / 读文件 / 搜文件 / 搜内容 / 查时间 / 设提醒 / 记速记 / 看屏幕），填参数点执行、结果直接显示，不经大模型、不耗 token
- **多会话历史**：像聊天软件，新建 / 切换 / 删除会话，上下文互不串；老扁平历史自动迁移
- **定时提醒 / 速记**：面板添加、聊天口令，或对智能体说一声；速记可 AI 一键整理
- **RAG 文档问答**：上传 PDF / TXT / MD，基于文档问答或出题。扫描版 PDF 千问视觉 OCR（4 路并行 + 断点续传），文档按内容哈希缓存，检索按页去重
- **桌宠形象**：素材文件夹即形象，文件即动作（gif/apng 原样保留），支持 AI 生成动作动图、角色名 / 提示词塑造人设
- **对话模式**：桌宠工具栏点 💬 开大窗，左侧边栏桌宠交互 + 视图切换（对话 / 工具台 / 提醒 / 速记 / 形象 / 设置），与桌宠模式共用同一份历史

## 聊天口令

| 口令 | 效果 |
|---|---|
| 提醒我5分钟后喝水 / 10秒后提醒我xxx | 设定时提醒 |
| 帮我记一下xxx / 记住:xxx | 存入速记 |
| 几点了 / 现在几点 | 时间注入，桌宠能答 |

## 配置

环境变量（放 `backend/.env` 或系统环境，`python-dotenv` 自动加载）：

| 变量 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek 聊天（主通道） |
| `DASHSCOPE_API_KEY` | 百炼一把 Key 通吃（视觉 / 聊天备用 / 嵌入） |
| `DASHSCOPE_VISION_MODEL` | 视觉模型名，默认 `qwen3.8-max-0902` |
| `DASHSCOPE_CHAT_MODEL` | 聊天备用模型名，默认 `qwen3.8-max-0902` |
| `PROACTIVE_MIN_SECONDS` / `PROACTIVE_MAX_SECONDS` | 主动互动间隔，默认 180-420 秒 |
| `AGENT_ROOT` | Agent 默认工作目录（相对路径基准），默认 `data/agent_workspace` |

聊天双通道：优先 DeepSeek，失败自动切百炼 Qwen。没配 `DASHSCOPE_API_KEY` 时，看屏幕降级为图像分析。

## 运行

- 开发：`python launcher.py`（自动拉后端 + Electron，改代码重跑即生效，会先杀掉 8000 端口旧后端）
- 打包：`desktop/` 下 `npm run build`
- 测试：`.venv/Scripts/python.exe -m unittest tests.test_rag tests.test_history tests.test_agent -v`（核心模块单测，离线可跑）

## 文档

- [ARCHITECTURE.md](ARCHITECTURE.md)：架构图、模块说明、关键数据流、性能数据

## 结构

```
backend/
  main.py       FastAPI 入口：聊天 / SSE / 主动互动 / 提醒 / 速记 / 多会话 / 工具台端点 / 形象管理
  agent.py      统一智能体循环：DeepSeek function-calling 13 工具调度（读类免费 / 写命令类先确认）
  vision.py     截屏（mss）+ 视觉模型评论 + 降级分析
  rag.py        RAG 文档问答：PDF 解析（扫描页 OCR）+ 切块 + 向量化 + 检索 + 缓存 + 按页去重
tests/
  test_rag.py        rag 核心逻辑单测（切块/页码/去重/缓存）
  test_history.py    多会话历史单测（迁移/隔离/截断）
  test_agent.py      agent 工具循环单测（路径解析/工具执行/确认闸门）
desktop/
  main.js       Electron 主进程（窗口 220×280）
  renderer/     pet.html / chat.html（对话模式）/ panel.html 渲染层
data/           运行时数据（数据库 / 历史 / 向量库 / 形象素材 / 工作目录），不入库
```
