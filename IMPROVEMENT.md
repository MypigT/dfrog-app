# AI桌宠助手 · 改进完成记录

> 本文件记录上传 Git / 简历前完成的改进。原 14 项清单已全部处理完毕，另附期间完成的额外改进。

## 一、原清单（14 项）— 全部完成

### P0
- ✅ **`.gitignore` 补全**：隐私数据（聊天历史/数据库/形象素材/Agent 工作目录）、构建产物（`release/` 521M）、日志、临时文件全部挡住；`data/` 仅保留目录占位与默认人设。
- ✅ **`naiwa_dict.md` 死数据清理**：删除文件 + 删除 [main.py](backend/main.py) 里从不读取的 `PERSONA_PATH` 死代码。

### P1
- ✅ **文档与实现不符修正**：`.env.example` 修正"奶蛙"笔误与不存在的"本地规则引擎"说法；README 把工作目录的"沙箱"表述改为准确的"相对路径基准"（读类工具实际可读全盘）。
- ✅ **重复代码抽取**：新建 [common.js](desktop/renderer/common.js)，统一 `API_BASE`、`AGENT_TOOL_CN`、模型配置渲染（`modelConfigFields` / `renderModelConfigList`），pet/chat/chatpanels/panel 四个渲染文件共用，消除三份拷贝。
- ✅ **测试覆盖补齐**：新增 [tests/test_agent.py](tests/test_agent.py)（28 例），覆盖路径解析、13 个工具执行、确认闸门、run_agent 确认放行/拒绝流程。全量测试 56 例通过。
- ✅ **全局变量定义顺序整理**：`agent_running` / `agent_default_root` 集中定义到 [main.py](backend/main.py) 顶部「全局状态」区。
- ✅ **死代码清理**：删除 `get-history` IPC（历史已改服务端为准）、无效的 `window-all-closed` `preventDefault`、tool 台改版后无人调用的 `pick-folder` IPC 及其引用。

### P2
- ✅ **bash 安全边界**：[agent.py](backend/agent.py) 加危险命令黑名单（`rm -rf /`、`format`、`shutdown` 等拦截），仍以主人确认为第一道闸门。
- ✅ **依赖版本钉死**：[requirements.txt](backend/requirements.txt) 钉全版本（`openai==3.42.1`、`pymupdf==1.28.2`）。
- ✅ **logging 替代 print**：5 个后端文件的 `[Dfrog]`/`[启动器]` 打印统一改为 `logging`，[main.py](backend/main.py) 配置 basicConfig。
- ✅ **API_BASE 去硬编码**：随 common.js 抽取统一。
- ✅ **chat.js 不再每次发送重复拉历史**：[chat.js](desktop/renderer/chat.js) 加本地 `localHistory` 缓存，发送时用本地上下文。
- ✅ **`_text_like` 异常保护**：[agent.py](backend/agent.py) 对 `p.stat()` 包 `try/except`，单个异常文件不再拖垮整次搜索。
- ✅ **README 精简**：重写为简洁版，去掉重复描述。
- ✅ **LICENSE**：新增 MIT LICENSE。

## 二、期间额外改进

- **形象名动态化**：原写死"奶龙"的弹窗名、系统提示词、看屏幕提示词全部改为跟随当前形象（[pet.py](backend/pet.py) 新增 `get_pet_name()`，main/agent/vision 提示词用 `{name}` 占位）；窗口标题、`productName` 等改为通用"桌宠 / AI桌宠"。
- **智能体页面重构**：删除与对话能力重复的独立 Agent 控制台（含 `/agent` 端点、panel 死代码、孤儿 CSS），对话内 🤖 页改为**工具台**——8 个可真实调用的工具（看目录/读文件/搜文件/搜内容/查时间/设提醒/记速记/看屏幕），填参数点执行、结果直接显示，读类工具不经大模型、不耗 token；新增 `/agent/tool` 直接调用端点。
- **数据清理**：删除测试残留与隐私文件（Agent 工作目录里的简历/截图、测试 RAG 数据、日志、临时文件、死数据人设文件）。

## 三、验证

```bash
.venv/Scripts/python.exe -m unittest tests.test_rag tests.test_history tests.test_agent -v
# Ran 56 tests ... OK
```

后端全部文件 `py_compile` 通过，渲染层 JS `node --check` 通过。
