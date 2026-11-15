# 2026-11-08 Agent 改革：统一 function-calling 循环（DeepSeek-only）

## 背景
- 用户要求：输入框的**每一条**指令，奶龙都要能"先思考再执行"，不再需要猜动词。
- 硬约束：只能负担 DeepSeek 大模型（不能再依赖 Claude CLI 外包智能体）。
- 旧架构问题：`/chat` 先经 `_classify_sync` 意图分类（闲聊/任务/提醒/速记），只有"任务"才进 agent 回路；闲聊一律纯文本通道，**模型没有任何工具授权**，所以"输入指令→思考怎么做"的体验断了。

## 改动清单
1. **agent.py 重写**：删掉 Claude Code CLI spawn 实现，改成 DeepSeek function-calling 工具循环（异步生成器事件流）：
   - 工具集：list_files / read_file / write_file / move_file / rename_file / search_files / delete_file / add_reminder / add_note / get_current_time
   - 全部文件工具锁死在 root 沙箱内（`_resolve_in` 拒绝越界）
   - 事件流：agent_start → agent_thought(💭) → agent_action(🔧) → agent_result → agent_text(最终答复逐块流式) → agent_done / agent_error
   - 显式超时 90s（`AsyncOpenAI(timeout=...)`），修掉旧的"分类+流式 60s 无超时卡死"
2. **main.py /chat 统一入口**：删除意图分类分支；视觉意图（DASHSCOPE）保留老通道；其余一切交给 agent 循环，**给模型 10 个工具让它自己判断该聊还是该干**。
3. **/agent 端点**改为消费生成器事件流，同步推 SSE（面板的 agent 进度照旧）。
4. **pet.js**：
   - AGENT_TOOL_CN 补新工具中文映射
   - SSE 处理 agent_thought(💭)
   - sendMsg 全量走 /chat，剥掉"派活:"前缀；本地派活函数/正则删除
   - chatAgentRunning 标记避免聊天流进度与全局 SSE 气泡重复弹
5. 冒烟修复：
   - tool 消息顺序：assistant(tool_calls) 必须先于 tool 结果（400 报错根因）
   - 思考文本只在有 tool_calls 时才当 💭，避免纯闲聊"一句话说两遍"
   - move_file 目标目录自动 mkdir（WinError 3）

## 验证结果（冒烟）
- `你好`：单句回复不再重复 ✅
- `帮我把工作目录里的文件理一理`：💭思考→🔧list_files→💭规划→🔧move 真实移动文件 ✅（且能自行改路线：move 失败后改用 rename）

## 文件变动
- backend/agent.py（重写，核心变更）
- backend/main.py（/chat、/agent、_agent_task_stream、删除意图分类与挂起任务逻辑）
- desktop/renderer/pet.js（统一路由、事件处理、去重）

## 遗留项处理结果
- ✅ agent_root.txt 已清空 → 回到默认沙箱 `data/agent_workspace`
- ✅ worklog/docs、worklog/scripts 里被误移的文件已放回 worklog 根目录，空目录删除
- ✅ 冒烟脚本坑：Windows GBK 控制台打印 emoji 会崩 → 脚本头部加 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`

## 最终端到端验证（2026-11-08 15:47 · 新进程 PID 6132 · 默认沙箱）
- `你好` 纯闲聊：单句回复、无重复 ✅
- `帮我把工作目录里的文件理一理`：💭 思考 → 🔧 list_files → 💭 规划("文档/图片/表格记录"三类) → move_file ×9 全部成功（三个新子目录由代码自动创建，**WinError 3 修复得到端到端验证**）✅
- 事后检查沙箱：9 个文件 → 文档×4 / 图片×2 / 表格记录×3，无残留
- 验证后沙箱已还原为扁平 9 文件状态，便于演示反复复现

## 四肢对齐 + 确认闸门（2026-11-08 第二轮改造）

### 目标
用户要求桌宠拥有编辑器 Agent 一样的四肢能力（读全盘 / 搜内容 / 跑命令 / 改删文件），但大脑仍是 DeepSeek（不接官方模型、不 spawn Claude CLI）。

### 权限模型（用户拍板：中间档「读随便、动要过问」）
- 读类（read_file / list_files / search_files / content_search / get_current_time）：**全盘可达，不打扰主人**。
- 写类 + 命令类（write_file / edit_file / move_file / rename_file / delete_file / bash）：**动手前弹确认气泡**，主人点「放行」才执行。

### 实现
- `agent.py`：工具集 10 → **13**；新增 `content_search`（全盘搜文件内容）、`bash`（cmd，30s 超时返回截断）。读写/移动/删除解沙箱（默认工作目录是写操作根，绝对路径也认）。把危险工具做成 `agent_confirm` 事件 + `_confirm_futures` Future 注册表：yield 事件后 `await asyncio.wait_for(future)` 挂起，直到 main 的 `POST /agent/confirm` 调 `resolve_confirm` 灌回结果才继续。读类工具直接执行。
- `main.py`：新增 `ConfirmRequest` + `POST /agent/confirm`（调 `resolve_confirm`，返回 `{"ok":bool}`）；`_agent_task_stream` 处理 `agent_confirm` → 聊天流 yield「🙋 需要主人放行…」。
- `pet.js`：AGENT_TOOL_CN 补 bash/content_search/edit_file；SSE 收到 `agent_confirm` 弹 `confirm-bubble`（带「放行 / 算了」按钮），点击 POST `/agent/confirm` 后移除；该事件不受 `chatAgentRunning` 抑制。
- `pet.css`：新增 `.confirm-bubble / .confirm-btn`（奶龙黄底气泡 + 绿「放行」/红「算了」按钮，气泡 pointer-events:auto 可点）。

### 验证结果（后台冒烟 dfrog_smoke_full.py，exit 0）
- 能力问答：rule8 生效，模型列出完整 13 能力清单 ✅
- `/agent/confirm` 空确认（无挂起 future）→ `{'ok': False}` ✅
- confirm e2e（写→删）：SSE 收到 `write_file` 确认 → `/agent/confirm` 放行 `ok:true` → 执行写入 → `delete_file` 再确认 → 放行 → 删除；聊天流按序显示「💭 思考 → 🙋 需放行 → 🔧 执行 → 结果」，最后总结「建了又删，啥也没留下」✅，工作目录自清。
- 说明：冒烟验证的是 **API + SSE + 挂起恢复** 链路；桌面 Electron 的按钮点击 UI 未做真机验证（按钮 POST 逻辑封装在 `showConfirmBubble` 内）。

### 第三轮：确认气泡 UI 打磨 + 内容净化（修用户报"点了没反应 / 气泡框得优化 / 内容不适合摆在台面上"）
- **根因（"点了没反应"）**：旧 confirm-bubble 挂进 `bubble-stack`。气泡栈有两条会静默干掉确认卡的逻辑——①`MAX_BUBBLES=3` 堆满就把最老的 remove，②每个气泡 5 秒后自动 `fade-out`→remove。5 秒内主人没点（或点到时已被移除），卡片悄悄消失；且 `chatAgentRunning` 抑制逻辑下 🔧 弹、🙋 照弹但 5 秒后没了。加上 `bubble-stack` z-index 10、pet-body z-index 40，气泡被奶龙本体挡住 → 视觉上"点了没反应"。按钮 POST 逻辑本身是对的。
- **修法**：确认卡从气泡栈里拆出来，独立放到 `#confirm-layer`（新增、`pointer-events:none` 层，卡片 `.confirm-card pointer-events:auto` 可点、z-index 60 > pet-body 40）。卡片无 5 秒消失、不被气泡栈清走、不被奶龙挡。按钮点击 → 先禁用两个按钮 + 改描述文案即时反馈，再 `POST /agent/confirm`，成功/失败/连不上分别有明确话，`hideConfirmCard` 淡出移除。
- **内容净化**：对话框不再显示 `🔧 {detail}` 原始行（工具名+绝对路径+「（N 字）」内部参数）——`agent_action` 分支不再 yield 进聊天流，只由全局 `publish_event` 弹 5 秒消失的桌面 🔧 气泡。`agent_result` 摘要过滤掉带工具名前缀/绝对路径的行（保留 ✅ 人工摘要）。`agent.py` 的 `_tool_confirm_detail` 改为只露 `Path(...).name` 文件名，去掉绝对路径和「（N 字）」。dialog 最终效果：`🤔 思考 → 🙋 确认 → 最终答复`，第三方进度只在桌面短暂冒泡。
- **验证**：`node --check pet.js` ✅；回走 `dfrog_smoke_full.py` exit 0：对话框最终文本 `🙋 需要主人放行：写文件: confirm_probe.txt`（无绝对路径、无内部参数）✅。桌面端依赖 Electron 重启加载新 pet.js / pet.css。

### 第四轮：交互体验修复
- **输入框悬浮即出现**：`pet.js` 的 `document mousemove` 里新增——鼠标在奶龙附近 50px 内（`near`）时同时调用 `showInput()`，和工具栏同步显隐；不再只有点击容器才出现。
- **历史面板实时更新**：`panel.js` 历史面板打开时启动 `setInterval(refreshHistory, 1200)` 轮询奶龙窗口 localStorage（经主进程 IPC `get-history`），奶龙每发一句历史就同步刷新；`beforeunload` 时清定时器。
- **所有面板置顶**：`main.js` 的 `openPanelWindow` 面板窗口构造加 `alwaysOnTop:true`，并 `setAlwaysOnTop(true,'screen-saver')`，与奶龙窗口同级置顶，不再被其他窗口盖住。
- **长回复不再秒消失**：`sendMsg` 的占位气泡在回复到达后，按文本长度延长生存——`fadePlaceholder(ms)` 支持延迟参数，成功路径 `Math.max(10000, reply.length*40)` ms 才淡出（短 10 秒、每字 +40ms），不再是生成完立即 1.5 秒消失；90 秒卡死兜底保留。
- **接入我到底在说的正则修正**：前端 `VISION_INTENT_RE` 的裸 `干嘛/干啥/干什么` 会误中"你能干嘛" → 占位符显示"让我看看…"很突兀。改成与后端口径一致的整词短语（你在看/在干嘛/在干啥/干嘛呢/干啥呢 等），纯聊天不再误中。
- **后端去掉固定开场/收尾**：`main.py _agent_task_stream` 删掉无条件"收到主人～这就看看…"开场（纯聊天"你能干嘛"也先甩这句），`agent_start` 只 `publish_event` 给面板/桌面，不产出聊天文本；新增 `did_tool` 标志，`agent_action/agent_confirm` 置位，`agent_done` 只在真正用过工具时才补"✅ 搞定啦主人～还有事尽管吩咐～"收尾——纯闲聊不再被硬加"搞定"。
- **验证**：`node --check pet.js/panel.js` + `COMPILE OK`；冒烟 exit 0：纯聊天能力问答只出模型能力清单（无固定开场/收尾）、e2e 写→确认→删照常，用过工具时聊天流仍带 ✅ 收尾。工作目录自清。
- **遗留**：桌面端 UI（悬浮输入框、置顶面板、历史轮询）需重启 Electron 真机验证。

### 第五轮：确认按钮穿透修复 + 历史同步改事件驱动（修用户报"确认按钮还是点不了"和"实时同步按发送/接收各来一次"）
- **确认按钮点了没反应的真根**：上轮把确认卡放进 `#confirm-layer`（z-index 60、卡片 `pointer-events:auto`），但 pet.js 里「是否关闭鼠标穿透」的判断只认 `petBody / inputArea / toolbar` 三个矩形。确认卡在 petBody 正上方区域，鼠标移到卡片上时 `overConfirm=false`，窗口仍处于 `setIgnoreMouseEvents(true, {forward:true})` 穿透模式——**透明窗口穿透状态下 click 会被穿到身后的应用**，渲染进程根本收不到 mousedown/mouseup，按钮自然"点了没反应"。修复：mousemove 穿透判定里补 `overConfirm`（拿 `confirmLayer.getBoundingClientRect()`），鼠标移到确认层时 `setClickThrough(false)` 关掉穿透，按钮才真正可点。教训：透明窗口的穿透开关是整窗级的，判定区域必须覆盖所有可点元素。
- **历史同步改事件驱动**：用户要"发送命令和收到回复时各同步一次"。撤回轮询方案（panel.js `setInterval(refreshHistory, 1200)`），改成 ①pet.js 新增 `recordHistory(role, text)` = 原有 addHistory + `POST /history/ping`（fire-and-forget，后端没开静默跳过，历史仍已写本地）；②后端 `POST /history/ping` 端点 `publish_event({"type":"history"})` 广播给所有 SSE 订阅者；③panel.js 历史面板开窗口即订阅 `GET /events`（EventSource），收到 `type:"history"` 才 `refreshHistory()`，`beforeunload` 关闭。正好两个触发点：发送时一次（recordHistory('user', msg)）、收到回复时一次（recordHistory('pet', reply)，成功+失败两条路径都盖到）。
- **验证**：node --check pet.js/panel.js + COMPILE OK；临时脚本 dfrog_history_ping_test.py 验证 ping→SSE 广播 history 事件收到、空 confirm→ok:false；dfrog_smoke_full.py exit 0（e2e 写→确认→删照常，对话流干净）。

### 说明与遗留
- 桌面端新穿透判定 + 事件驱动历史需重启 Electron 真机验证。

### 第六轮：创作内容直接出稿 + 修一处语法雷（用户报"要生成小说他一直拒绝、反问、不干事"）
- **行为观察**：让奶龙写小说 → DeepSeek 反复反问（类型/长短/主角），说"都可以"后仍扯别的；且**确认气泡根本没弹** → 属纯聊天层"拒绝式反问"，不经过工具循环。
- **根因**：①SYSTEM_PROMPT 无"创作直接动笔"硬规则，模型默认用反问对齐需求；②max_tokens=2048 对一篇短篇输出太紧。
- **改动**：①SYSTEM_PROMPT 加规则 12：主人要小说/故事/诗/段子/文案/歌词/脚本等创作时**直接动笔**，绝不反问类型/长度/主角/风格；主人说"都行/随便/都可以"时自己随机挑题直接写（三个靠右放在 rules 10-11 后，字符串内一律中文引号/「」）；②`max_tokens` 2048 → **4096**（line 582，给长篇创作留空间）。
- **顺带修的语法雷（重要教训）**：此前轮次 rules 10 的 `大白话("文件写好了"…` 把 **ASCII 双引号嵌进了 Python 字符串字面量**——字符串在行内提前闭合，整个文件报 SyntaxError（错行 506）。排查路径：UTF-8/GBK 假设排除（文件实为合法 UTF-8，终端乱码仅为 GBK 控制台渲染）、零宽/BOM 扫描无果、tokenize+括号堆栈分析全通过、提取 SYSTEM_PROMPT 段隔离编译复现、逐行 ASCII 引号计数揪出第 526 行 6 个引号。修法：Python 脚本按码点把内部引号换成中文引号（chr(0x201C/0x201D)）。**教训：SYSTEM_PROMPT 字符串内部禁用 ASCII `"`，一律用中文引号/「」；Windows 控制台乱码不代表文件编码坏。**
- **验证**：后端重启；冒烟 `Temp/dfrog_creative_test.py`——"随便写一篇短篇小说"：**1234 字直接起稿**（48 句、首行即是标题《最后一个记得怎么关机的人》、叙事完整自然收尾），无"先问类型"式反问（正文里有一处"要不要我"是**小说人物对话台词**，非模型反问，正则误报）；"再来一首小诗"：直接成诗。`dfrog_smoke_full.py` 重跑 exit 0：能力问答自然枚举、空 confirm ok:false、写→确认→删 e2e 链路完好。
- **遗留**：本轮只动后端（agent.py），桌面 UI 无改动，重启 launcher 即生效。

### 第七轮：看屏幕评论不再句句"摸鱼"（用户报"每次看我屏幕之后说的话都差不多，不管我在干什么都说摸鱼"）
- **根因**（vision.py 的 `VISION_PROMPT`）：① 规则 `代码/网页/文档 → 调侃用户在写代码或摸鱼`——把**一切工作界面**都归成"摸鱼"，模型不看画面也出得来回；② 上限 30 字 + 强制带"哈哈"，输出模板化；③ `max_tokens=120` 偶尔把话掐断，更像套话。
- **修复**：① 重写 VISION_PROMPT——**先看画面里实际是什么，再基于真实内容发言**；代码/终端/文档/设计软件等是忙正事，**不许说"摸鱼"**；只有视频/剧/游戏/娱乐网页才有资格说摸鱼（兴奋或吐槽）；浏览器/聊天窗口如实描述"翻网页/查资料/聊天"；看不清就直说，不许编；画面里的小龙是奶龙本尊，忽略。② 注入当前时间（深夜还能换个说法）。③ `max_tokens` 120→200。④ main.py 聊天视觉通道补"看清就说看清，别瞎猜主人在干嘛"。
- **验证**：`Temp/dfrog_vision_test.py` 连做 3 次 `look_at_screen()`（引擎均为 qwen3.8-max，39s/22s/35s —— 真视觉推理慢但属既有行为），三条评论**截然不同、全部命中屏幕真实内容**：①"VS Code 跑 Python 服务器，转头 QQ 群算史莱姆银币，正事聊天两手抓" ②"敲 Python 启动器代码、终端 200 OK、还摆游戏天赋截图参考，群里千人围观" ③"代码终端冒着 200 OK，QQ 群顶在最前聊天，输入框 tao 候选词挑半天"。
- **遗留**：本轮只动后端，Electron 未改；桌宠连着新后端，点👀立即生效。

### 第八轮：聊天自然化——删全部固定回话 + 主动互动改提示词驱动（用户报"平时聊天说的话很固定很莫名其妙""把所有固定回话删掉，一段时间自动触发的对话改成向AI输入提示词")
- **聊天人设重写（main.py SYSTEM_PROMPT）**：删掉「最喜欢捧腹大笑」（每句哈哈的源头）和「每次回复不超过 30 个字」（过短导致模板感）；改成像熟人聊天——句子长短随内容、禁止模板开场/收尾、不要每句哈哈、禁止工程化总结。
- **输出上限放宽**：`_call_llm` 与 `_stream_channel` 的 `max_tokens` 80→300（流式通道也改，防首段掐断）。
- **主动互动不再写死台词（THOUGHTS/DEMANDS 池删除）**：`PROACTIVE_SCENARIOS` 四个场景提示词（主人很久没找你 / 主人正盯着电脑忙 / 忙了好久没理你 / 一天快过完 + {now} 注入）→ 调 `llm_naiwa_reply` 让 LLM 自由生成；约 15% 概率附带 dance/laugh 动作；LLM 挂了安静跳过不留兜底话（失败路径静默，不再弹固定句）。
- **后端兜底兜底 Return 空**：`llm_naiwa_reply`/`_stream` 的「哈哈……啊？后端好像坏了。"删除，返回空，由前端中性展示。
- **前端固定文案清零（pet.js）**：占位词「让我想想…/让我看看…」→「等等…/嗯…」；空回复兜底「哈哈……啊?」→「嗯？你刚才说啥？」；断线「啊？？后端没开？」→「嗯……信号断了？」；👀占位「让我看看…」→「嗯…」；vision 失败「看不太清楚…」→「嗯…眼睛没睁开。」；**戳的动作**缺反应兜底「齁齁齁」→「嗯？你戳我？」；**长按**缺反应兜底→「嘿，干嘛呢…」（楶 context-based，feed 音效另换）。功能结果反馈（提醒/速记成功失败、确认按钮文案）保留——那是 UI 反馈不是角色台词。
- **验证**：`Temp/dfrog_chat_natural_test.py`——主动 4 场景回复风格各异且接地气（想主人、拿主人屏幕内容开玩笑、吐槽"两点就收工？"）；普通聊天「你好/你平时都干啥/我有点无聊」怼"哟冒泡了"/长句唠嗑/反问要游戏，无一模板；注：场景里含"屏幕"字会触发 vision-chat（真看屏评论，慢但正常）。`dfrog_smoke_full.py` exit 0：能力问答自然枚举、空 confirm ok:false、写→确认→删链路完好。
- **遗留**：pet.js 本轮有改动，Electron 需重启加载。

### 第九轮:去掉回复末尾的模型自签名(用户报"为什么他说话最后会接句deepseek")
- **诊断**:全链路搜索前端所有展示路径(pet.js/panel.js/main.js 的字符串拼接、history 结构、`_agent_task_stream`/流式通道返回值),**零处**引用引擎名;再本地 curl `/chat` 实测管线输出仍然干净。结论:不是代码拼接,是 **DeepSeek 模型自身的角色扮演自签名习惯**(回复尾巴附"——DeepSeek"之类)。
- **修复(双向堵)**:
  - ① 后端 `agent.py` SYSTEM_PROMPT **规则 13**:永远不提任何模型名/引擎名/公司名(DeepSeek、Qwen、阿里、OpenAI 等一律不许出现),回复结尾不许带落款/签名;主人问起就答"我就是奶龙"。(改这条时第三次先踩 ASCII 双引号语法雷——`"AI 生成"` 嵌进字符串字面量,Python 脚本换中文引号后 `ast.parse` 复验 ✅)
  - ② 前端 `pet.js` **`cleanReply()` 展示清洗**(兜模型不听话时仍自签):四层判断——
    - a. **行内签名短语截断**:从签名记号(`——`/`(`/`「`/`, `等)开始到行尾整段含签名特征词 → 只删短语留正文(`"主人我会记住的, by DeepSeek"` → `"主人我会记住的"`);
    - b. **尾巴整行签名删行**(`(由 Qwen 生成)` / `——DeepSeek`);
    - c. **中段签名行删除**:正文说完签一句又补一句的签名行(`——DeepSeek` 夹在正文中间)也删;
    - d. **整条回复就是一条签名 → 置空**,交前端中性兜底(「嗯?你刚才说啥?」)。
    - 判定特征:含模型词(deepseek/qwen/gpt/claude/openai/大模型/由…生成/by xxx)且为**短行+句尾无标点**;**正文谈论模型用法(API/部署/文档/平台)不误伤**——单测 14 例全过(`"主人的电脑是 deepseek 吗?"` 保留、`"先帮我看看怎么用 DeepSeek API 部署个模型吧——DeepSeek"` 只剥落款)。
  - 应用点:`addBubble` 非用户侧统一清洗(主动互动 SSE、👀视觉评论、互动反应、创作文本全部盖到);`sendMsg` 响应收尾处 `reply = cleanReply(reply)` 先清洗再显示+入历史。
- **本次单测踩的两个正则坑(记下来)**:① `/^(—|~|\s*)/` 里 `\s*` 可空匹配 → "以签名记号开头"恒真 → filter 把正文行删光;② 行内签名截断必须先于整行删除判断,否则短正文句(如含 deepseek 词)被当整行签名整个删。
- **验证**:`node --check pet.js` ✅;后端规则 13 实例在岗(curl `/chat` 输出干净无落款);dfrog_smoke_full.py 重跑 exit 0(能力问答/空 confirm/写→确认→删链路完好);cleanReply 边界单测 14 例全过。**遗留:pet.js 本轮改动需重启 Electron 真机验证。**

### 第十轮：奶龙自己说的话也进历史 + 💬 大对话框面板（用户报"把他自己说的话也存到历史对话里方便随时观看，另外加个模式按钮，点击后可以切换成一整个大弹窗面板方便对话而不是桌宠"）
- **① 历史统一数据源（服务端为准，一份大家用）**：
  - 后端 `main.py`：历史持久化 `data/chat_history.json`（上限 300 条），新增 `GET /history/list`、`POST /history/add`（写一条 + 广播 `history` 事件，提醒面板/对话面板同步刷新；旧接口 `/history/ping` 保留兼容）；`/chat` 把前端 `role:"pet"` 映射成 `"assistant"` 做上下文，两窗口统一格式。
  - 前端 `pet.js` `recordHistory` 双写（服务端 + 本地 `localStorage(pet_history)` 兜底）；`bootHistory` 启动即拉 `/history/list` 合并本地缓存。
  - **全部奶龙出口入历史**：`/chat` 回复、提醒/速记口令反馈(`⏰…`/`📝…`)、SSE `speak`/`action` 主动台词、👀视觉评论(`👀 …` 前缀)、戳/长按反应；`addBubble` 非用户侧统一过 `cleanReply`（显示+入库一起清，第 9 轮四层版原样沿用）。
- **② 💬 大对话框面板（与桌宠并行的另一形态，不是替代）**：
  - `pet.html` 右侧工具栏 2×2 grid 加第 4 按钮 💬，点按 → `api.openChatWindow()` → IPC 开独立窗口（`main.js`：560×700、`titleBarStyle:'hidden'`、`alwaysOnTop:'screen-saver'`、置底右、可缩放可拖动——`chat.css` 标题栏全条 `-webkit-app-region: drag`），✕ → `closeChatWindow` 收起来，奶龙不变。
  - 新建 `chat.html`/`chat.css`/`chat.js`：左🐲右🙋气泡流（最多显示最近 120 条）；启动拉 `/history/list` 渲染（空则提示）；输入支持全部口令(提醒/速记)+派活前缀；`/chat` **流式**显示（边收边填气泡，收尾 `cleanReply` 再入历史）；智能体确认 → 深色确认卡「放行/算了」→ `POST /agent/confirm {confirm_id, approved}`；SSE 订阅同一事件流：`history`→整窗重拉、`agent_confirm`→确认卡、`speak`/`action`/`reminder`→本地补显示（历史由桌宠窗口负责写，对话窗不重复记）、`agent_action`/`agent_thought` 对话流进行中时抑制避免重复。
  - **公共抽取**：`stripMd`/`cleanReply` 移入 `utils.js`（pet.html + chat.html 都先引它）——之前 pet.js 自含，抽走后两窗口共用同一版本，14 例单测改从 utils.js 取函数体仍全过。
- **③ 验证**：`node --check` utils/chat/pet/main/preload/panel 全 ✅；后端直起（python main.py 1s 就绪），`POST /history/add` + `GET /history/list` 读写闭环 ✅；`dfrog_smoke_full.py` 重跑（/chat 能力问答/空 confirm/写→确认→删）✅；cleanReply 14 例单测 ✅。**遗留：用户 Electron 真机验证**（💬 开窗、流式对话、主动台词入历史、面板同步）。

### 第十轮事后修复：pet.js 整脚本 ReferenceError（用户报「点不了也不显示按钮」）
- **症状**：桌宠窗口在，但点击/拖动/悬停输入框全部失效、工具栏按钮不响应。
- **根因**：第十轮给 💬 按钮加绑定写了 `chatBtn.addEventListener(...)`，**忘了定义 `const chatBtn = document.getElementById('chat-btn')`**——`node --check` 只查语法不查未定义变量，渲染脚本顶层执行到该行抛 ReferenceError，其后所有绑定（点击/长按/拖动/mousemove 工具栏显隐/👀/close）**全未执行**。
- **修法**：补 `const chatBtn` 定义（pet.js 第 15 行，与其他按钮元素同区）；后端直起于后台（本环境 launcher 每次重跑会 kill 8000 旧实例，属预期）。
- **新增防复发工具（重要）**：`Temp/dfrog_render_check.js`——DOM 桩（document/window/EventSource/fetch/localStorage…）+ 间接 `(0, eval)` 按浏览器多 `<script>` 全局语义依次干跑 utils/pet/chat/panel 的顶层代码，任何 ReferenceError 当场打 ❌。**以后改任何 renderer 脚本，先跑干跑检查再谈 node --check。** 该脚本首轮就把「桩缺 window.location」这类环境问题和「chatBtn 未定义」同款坑当场抓出。
- **验证**：干跑检查 4/4 ✅；后端 /health /history/list /chat 流式实测 ✅；smoke exit 0（写→确认→删 e2e 完好）。仍待用户重启 Electron 真机验收。

### 第十一轮：工具栏收编 + 工具栏/输入框同显隐 + 💬 对话模式大窗口（用户 3 点反馈）
- **反馈原文要点**：① 按钮太多超出窗口边框；② 输入框应与按钮「同一个代码」同时显示/隐藏；③ 💬 不是新开的并行面板，而是**模式切换**——进模式就不显示桌宠，改为大弹窗：左侧边栏（最上方奶龙图片、和桌宠一样的交互，下方按钮切换右侧区域），本模式下所有对话进窗口不冒桌面气泡；两模式共用同一份消息数据，历史同步一致。
- **① 工具栏 3×3 单列网格**：`pet.html` 左右两列 6 按钮（×/💬/≡/🤖/⏰/📝/👀）改为 `#btn-col` 单一 `34px×34px` 3列×3行居中网格（7 按钮 + 2 空位），彻底不越窗（原来是两侧各 38px 挤两边）。`pet.css` 只留 `.pet-btn-col/.pet-btn/.hidden`。
- **② 工具栏与输入框同套显隐**：`pet.js` 顶部新增 `showToolbar()`= 移除 hidden + `showInput()`；`hideToolbarSoon()`= `hideInput()` + 5 秒后加 hidden。所有触点（容器点击、mousemove near、blur/mouseleave、5 秒轮询）都走这条路；`showInput` 只剩隐藏定时器管理。**顺手修一处遗留雷**：mousemove 穿透判定（第 666 行）仍写 `[btnColLeft, btnColRight].some(...)`——这两个常量在 ① 里已不存在，等于 ReferenceError（鼠标一移到那里就炸、桌宠再死一次的同款），改成单一 `btnCol` 判定。
- **③ 对话模式（mode switch，不是并行窗口）**：
  - **开**：`main.js openChatWindow()` → `petWindow.hide()`（桌宠隐身；渲染进程照常跑，奶龙的台词生产/入库全部还由它负责）→ 560×700 置顶大窗。
  - **关**：`chatWindow.on('closed')` → `petWindow.show()` + `webContents.send('pet-refresh')` → `pet.js refreshDesktopBubbles()`：清气泡栈，从服务端 `/history/list` 取最近 3 条**奶龙**发言重放一屏（两模式数据同一份）。`preload.js` 暴露 `onPetRefresh`。
  - **chat.html 重写**：`#sidebar`（上方奶龙图片、下方 💬对话/≡历史/🤖智能体/⏰提醒/📝速记/👀看屏幕/↩返回桌宠模式）+ `#stage`（`#view-chat` 对话视图 + 4 个 iframe 载 `panel.html#{type}` 复用面板）。`chat.css` 重写（侧栏 + 气泡/确认卡保留原样式，奶龙图片动画关键帧与 pet.css 同款）。
  - **侧栏奶龙交互同桌面**：点击=戳（/action poke，跳舞冒泡）、双击=大笑、长按 500ms=变形；台词双投（侧栏小气泡 + 对话视图）并 `recordAdd` 入服务端历史。冷却 3 秒同 pet.js。👉 本轮只投显示的：SSE speak/action/reminder（桌宠隐藏窗口照常监听+入历史，这边只补显示、不重复写）。
  - **视图切换** `showView()` 切 active 类；iframe 面板 ✕ → `panel.js` 检测 `window.parent !== window` 时 `postMessage({type:'dfrog-panel-close'})` → 大窗收到后切回对话视图；独立面板窗口路径不变。
  - **panel.js 历史数据源改服务端为准**：`refreshHistory()` 先 `GET /history/list`，拉不开回退 IPC 读奶龙窗口 localStorage（兼容）。从此 iframe/独立窗口看到的是同一份历史，`/history/ping`/SSE `history` 事件刷新机制不变。
- **④ 验证**：`node --check` 六个文件全 ✅；`dfrog_render_check.js` 干跑 4/4 ✅（chat/panel 新 DOM 全部兼容）；后端冒烟（能力问答/空 confirm/写→确认→删 e2e）通过 + `/history/add`↔`/history/list` 闭环 ✅。**遗留：用户重启 Electron 真机验收**（💬 进模式、桌宠隐身、对话/历史/面板同步、退出模式桌宠带着新台词回归、工具栏不越界）。

### 第十一轮事后修正（用户 5 点反馈，继续打磨）
- **① 按钮不许压奶龙**：上一轮 3×3 网格顶在奶龙头上改动了（用户原话「谁叫你把按钮挡在奶龙上面的」）。还原思路：按钮放**奶龙左右两侧**——`pet.html` 拆成 `#btn-col`/`#btn-col-right` 两个 2×2 网格、每格 28px 贴窗边（左列 x2~59 右列 x161~218；奶龙本体 x62~158 宽 96px，完全不相交），`top:238px` 与奶龙顶部齐平。**边界核对：220×340 窗口内一格不越界、一件不压龙。**
- **② 对话窗拖拽/关闭/缩放**：chat 头版丢了标题栏（旧 3×3 版删了 header 条）→ 恢复 `#chat-head`（整条 `-webkit-app-region: drag` + 右下角 ✕→closeChatWindow→桌宠回归）。缩放靠 BrowserWindow `resizable:true` + Windows 原生 frame。
- **③ 按钮语义适配新模式**：侧栏按钮 = 右侧**区域切换器**（智能体/提醒/速记 = iframe 全功能面板；👀 = 直接动作投对话视图；💬 = 返回对话视图）——不是桌宠工具栏的硬搬。
- **④ 删掉「≡历史」**：对话视图本来就加载完整历史流（最近 120 条）——桌宠模式加 ≡ 是因为桌宠气泡看不到历史；对话模式不需要。
- **⑤ 互动+对话一个整体**：架构原则再确认——桌宠模式（气泡展示）与对话模式（对话框展示）共用同一消息数据集（服务端 `history`），互动（戳/长按/👀）在哪一轮都走同一套事件、同一份入库责任（生产方是谁谁入库，对方只补显示）。本模式奶龙左侧图片的交互保持桌面同款（点击戳/双击笑/长按变形 + 侧栏小泡 + 双投对话视图 + 入历史）。
- **验证**：`node --check` 6 文件全 ✅；`dfrog_render_check.js` 干跑 4/4 ✅（chat 新 viewEls 适配：对话视图 + 3 iframe，无 history 视图）。**遗留：重启 Electron 真机验收（含工具栏几何核对：按钮列不压龙、不出窗）。**
- **后端备注**：本轮只动前端 + README；`backend/main.py` 未改。冒烟重启时发现 127.0.0.1 与 localhost 分离——`localhost` 走 IPv6(::1) 可连通但 smoke 脚本用 `127.0.0.1` 连不上，需确认后端监听绑定（flag：新 nohup 进程可能被 Git Bash 会话杀掉，用 run_in_background 持久启动）。

### 即时修复：工具栏按钮掉出窗口（用户报「看不到按钮了」）
- **症状**：重启 Electron 后桌宠上没有任何按钮。
- **根因**：两列 `.pet-btn-col` 写在了 `#pet-body` **内部**，但 CSS 的 `top:238px` 是按**窗口**（340px）坐标算的。定位参照变成 pet-body（96px 高、窗口 y236 起）→ 按钮实际被放到 y≈474，落在窗口外不可见；左列 left:2px 相对 pet-body（x62）变成 x64~122 压在龙身上、右列同理错位。即「层级 + 坐标参照」双错位。
- **修法**：两列挪到 `#pet-container`（`position:relative`，220×340 = 窗口本身）下做直接子元素，CSS 坐标恢复为窗口坐标。手核几何：左列 x2~60（龙左缘 62）、右列 x160~218（龙右缘 158）、y238~294（窗口 340）——三都不越界不压龙。
- **教训（第三轮同类）**：改 HTML 层级动定位参照会静默把元素送进不可见区域；`node --check`/干跑都抓不到，**必须对着窗口尺寸手工推一遍元素坐标**。为此在 worklog 记下窗口内坐标表（pet 96 宽、顶 y236；窗口 220×340），后续布局改动先查表。
- **验证**：语法 + 干跑复跑全 ✅（本轮 pet.js 未动，仅 HTML 移动节点）。用户重启 Electron 确认按钮回归。



