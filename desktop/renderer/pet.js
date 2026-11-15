// 桌宠交互逻辑（奶龙：气泡在头顶，输入框在下方，抓住奶龙可拖动窗口）
// 原则：奶龙侧的每一个说话气泡都是奶龙的一次说话 → 必须同步 recordHistory('pet', ...) 进历史。
//       桌宠模式把说话展示成气泡，对话模式展示成对话框消息，历史只有一份。

const pet = document.getElementById('pet');
const petBody = document.getElementById('pet-body');
const bubbleStack = document.getElementById('bubble-stack');
const confirmLayer = document.getElementById('confirm-layer');
const inputArea = document.getElementById('input-area');
const input = document.getElementById('input');
const closeBtn = document.getElementById('close-btn');
const historyBtn = document.getElementById('history-btn');
const eyeBtn = document.getElementById('eye-btn');
const chatBtn = document.getElementById('chat-btn');

const btnCol = document.getElementById('btn-col');
const btnColRight = document.getElementById('btn-col-right');

// ==================== 工具栏 + 输入框：同一套显隐（同时显示、同时隐藏） ====================
// 鼠标离开奶龙附近 5 秒后自动隐藏；独立轮询 + mouseleave/blur 兜底。
// 输入框不再有独立的显隐状态：任何显示本区域的操作都会拉起输入框，任何隐藏都会把它们一起藏。
let btnHideTimer = null;
let lastSeenAt = 0;   // 上次在窗口内看到鼠标的时间戳

function showToolbar() {
    lastSeenAt = Date.now();      // 鼠标还在奶龙附近：刷新活跃时间
    btnCol.classList.remove('hidden');
    btnColRight.classList.remove('hidden');
    showInput();
}

function hideToolbarSoon() {
    clearTimeout(btnHideTimer);
    hideInput();
    btnHideTimer = setTimeout(() => {
        btnCol.classList.add('hidden');
        btnColRight.classList.add('hidden');
    }, 5000);
}

// 鼠标离开整个奶龙窗口（去点别的程序、点开面板等）→ 当作“不活跃”，安排隐藏
document.addEventListener('mouseleave', hideToolbarSoon);
window.addEventListener('blur', hideToolbarSoon);

// 独立轮询：按钮可见且超过 5 秒没看到鼠标 → 隐藏（不依赖 mousemove 一定能触发）
setInterval(() => {
    if (btnCol.classList.contains('hidden') && btnColRight.classList.contains('hidden')) return;
    if (Date.now() - lastSeenAt > 3000) {
        btnCol.classList.add('hidden');
        btnColRight.classList.add('hidden');
        hideInput();
    }
}, 2000);

// 历史对话记录 {role: 'user'|'pet', text}，持久化到 localStorage
const HISTORY_KEY = 'pet_history';
const MAX_HISTORY = 100;
const MAX_CONTEXT = 10;  // 传给 LLM 的最近对话条数
const history = [];

function saveHistory() {
    try {
        localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(-MAX_HISTORY)));
    } catch (e) {}
}

function addHistory(role, text) {
    history.push({role, text});
    if (history.length > MAX_HISTORY) history.splice(0, history.length - MAX_HISTORY);
    saveHistory();
}

// 写一条历史:本地记一份(下次开桌宠还在),同时写服务端
// (POST /history/add 保存 + 广播 history 事件,历史面板与大对话框面板都靠它刷新)。
// 后端没开时静默跳过,本地历史仍完整。
// 注意:奶龙侧说话气泡一律走这里入库(见文件头原则),别只 addBubble 不 recordHistory。
async function recordHistory(role, text) {
    text = String(text || '').trim();
    if (!text) return;
    addHistory(role, text);
    try {
        await fetch(`${API_BASE}/history/add`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({role, text})
        });
    } catch (e) { /* 后端没开时静默跳过,历史仍已写本地 */ }
}

// 启动时加载历史（服务端一份为准：大对话框面板和历史面板共用它；
// 后端没开/没数据时回退本地缓存）
(async function bootHistory() {
    const loadLocal = () => {
        try {
            const data = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
            if (Array.isArray(data) && data.length) {
                history.length = 0;
                history.push(...data);
                return true;
            }
        } catch (e) {}
        return false;
    };
    try {
        const res = await fetch(`${API_BASE}/history/list`, {signal: AbortSignal.timeout(2000)});
        const items = (await res.json()).items || [];
        if (Array.isArray(items) && items.length) {
            history.length = 0;
            history.push(...items.slice(-MAX_HISTORY));
        } else if (!loadLocal()) {
            /* 两边都没档，正常 | */
        }
    } catch (e) {
        loadLocal();
    }
})();

// 后端不可用 → 本地旧档兜底

// 兼容非 Electron 环境
const api = window.electronAPI || {
    movePet: () => {},
    setClickThrough: () => {},
    closePet: () => {},
    openPanel: () => {},
    closePanel: () => {},
    openChatWindow: () => {},
    closeChatWindow: () => {},
    onPetChatMode: () => {},
};

// ==================== 后端主动互动（SSE 推送） ====================

// /chat 统一循环跑任务时进度会从聊天流里回来，此时全局 SSE 的 🔧/💭 气泡
// 不再重复弹，免得两只气泡说同一件事。
// 对话模式大窗口的聊天流也会经主进程转发置这个标志：期间 🔧/💭 既不显示也不入库，
// 避免和聊天流重复记录（聊天流自己会把用户+完整回复记进历史）。
let chatAgentRunning = false;

if (api.onPetChatRunning) api.onPetChatRunning((running) => { chatAgentRunning = !!running; });

// 对话模式开关信号（主进程开/关对话窗时发）：对话模式期间确认卡由对话窗弹，
// 桌宠窗口（隐藏但照常收 SSE）不再叠确认卡——否则切回桌宠时一堆已过期 confirm_id 的卡片残留还能点。
let chatModeOn = false;
if (api.onPetChatMode) api.onPetChatMode((on) => { chatModeOn = !!on; });

const es = new EventSource(`${API_BASE}/events`);

es.onmessage = (e) => {
    try {
        const ev = JSON.parse(e.data);
        if (ev.type === 'speak') {
            addBubble(ev.text, false);
            if (ev.text && ev.text.trim()) recordHistory('pet', ev.text);  // 主动唠的也要进历史
            if (ev.text.includes('哈哈')) {
                setState('laugh', 2000);
                playHouHouHou();
            }
        } else if (ev.type === 'action') {
            if (ev.action === 'laugh') setState('laugh', 2000);
            else if (ev.action === 'dance') setState('dance', 2000);
            if (ev.text) {
                addBubble(ev.text, false);
                recordHistory('pet', ev.text);  // 主动动作带的台词同样进历史
            }
            playHouHouHou();
        } else if (ev.type === 'agent_action') {
            if (chatAgentRunning) return;   // 聊天流里已经在显示进度，不重复气泡/入库
            const cn = AGENT_TOOL_CN[ev.tool] || '忙起来';
            const text = `🔧 ${cn}…`;
            addBubble(text, false);
            recordHistory('pet', text);  // 进度气泡也是奶龙说的话 → 进历史
        } else if (ev.type === 'agent_confirm') {
            if (chatModeOn) return;   // 对话模式：确认卡由对话窗弹，桌宠不叠（切回时不留残留可点卡）
            showConfirmBubble(ev.confirm_id, ev.tool, ev.detail);
        } else if (ev.type === 'agent_thought') {
            if (chatAgentRunning) return;
            const t = String(ev.text || '').slice(0, 40);
            if (t) {
                const text = `💭 ${t}`;
                addBubble(text, false);
                recordHistory('pet', text);
            }
        } else if (ev.type === 'reminder') {
            const text = '⏰ ' + ev.text;
            addBubble(text, false);
            recordHistory('pet', text);  // 提醒播报也是奶龙说的话 → 进历史
        } else if (ev.type === 'pet_avatar') {
            loadAnimManifest();  // 桌宠形象切换/生成 → 刷新动图
        } else if (ev.type === 'settings') {
            fetch(`${API_BASE}/settings`).then(r => r.json()).then(s => { MUTED = !!s.mute; }).catch(() => {});
        }
    } catch (err) { /* 忽略坏消息 */ }
};

// EventSource 断线会自动重连，这里不用管

// 形态图片
const STATES = {
    idle: '../assets/common.jpg',
    dance: '../assets/dance.jpg',
    laugh: '../assets/happy.jpg',
    deform: '../assets/doi.jpg',
};

// 当前状态
let currentState = 'idle';

// ==================== 动图动作（图生多动作动图，优先于静态图+CSS动画）====================
// 加载 manifest.json，按顺序映射到动作槽位：idle/dance/laugh/deform。
// 有动图时 setState 切换播放 APNG（浏览器原生循环），无动图回退静态图+CSS动画，不破坏原行为。
const ANIM_DIR = '../assets/anim/';
const ANIM_SLOTS = ['idle', 'dance', 'laugh', 'deform'];
const animMap = {};  // state -> 动图路径

async function loadAnimManifest() {
    // 启动时先隐藏，等选中形象的图片真正加载完（onload）再显示。
    // 不能设 src 后立刻显示：APNG 还没加载完会先露出旧的默认奶龙图。
    pet.onload = () => { pet.style.visibility = 'visible'; };
    let useAvatar = false;
    try {
        const res = await fetch(`${API_BASE}/pet/avatar/current`, { cache: 'no-store' });
        const data = await res.json();
        const anims = data.manifest || [];
        anims.forEach((a, i) => {
            if (i < ANIM_SLOTS.length && a.url) {
                animMap[ANIM_SLOTS[i]] = API_BASE + a.url;  // 加 API_BASE：file:// 下相对路径会解析错
            }
        });
        console.log('[桌宠] 加载动图:', animMap);
        // 有 idle 动图：设 src，等 onload 加载完再显示（APNG 自动循环播放）
        if (animMap.idle) {
            useAvatar = true;
            pet.src = animMap.idle;
            const faint = pet.classList.contains('faint') ? ' faint' : '';
            pet.className = 'anim-playing' + faint;
        }
    } catch (e) {
        console.log('[桌宠] 无桌宠动图，使用静态图+CSS动画');
    }
    if (!useAvatar) {
        // 回退默认图（HTML 已加载好），直接显示
        pet.src = '../assets/common.jpg';
        pet.className = '';
        pet.style.visibility = 'visible';
    }
}
loadAnimManifest();

// 启动时拉一次设置，初始化静音状态
fetch(`${API_BASE}/settings`).then(r => r.json()).then(s => { MUTED = !!s.mute; }).catch(() => {});

// ==================== 设置（独立窗口，同历史对话面板，不塞在桌宠里）====================
document.getElementById('settings-btn').addEventListener('click', () => {
    if (api.openPanel) api.openPanel('settings');
});

// 播放齁齁齁音效
let MUTED = false;
function playHouHouHou() {
    if (MUTED) return;
    const audio = new Audio('../assets/houhouhou.wav');
    audio.volume = 0.6;
    audio.play().catch(e => console.log('音效播放失败:', e));
}

// 切换形态：有动图则播放 APNG，无动图回退静态图+CSS动画
function setState(state, duration = 0) {
    if (currentState === state) return;
    const faint = pet.classList.contains('faint') ? ' faint' : '';
    currentState = state;
    const animPath = animMap[state];
    if (animPath) {
        // 动图模式：切换 APNG 播放，不叠加 CSS 动画
        pet.src = animPath;
        pet.className = 'anim-playing' + faint;
    } else {
        // 兜底：静态图 + CSS 动画 class
        pet.src = STATES[state];
        pet.className = (state === 'idle' ? '' : state) + faint;
    }

    if (duration > 0) {
        setTimeout(() => {
            if (currentState === state) {
                setState('idle');
            }
        }, duration);
    }
}

// ==================== 气泡消息（往上叠，5 秒后消失） ====================
const MAX_BUBBLES = 3;

function addBubble(text, isUser) {
    // 奶龙侧文本统一清洗尾巴落款（用户原话不动）
    text = isUser ? text : cleanReply(text);
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble' + (isUser ? ' user' : '');
    bubble.textContent = text;
    bubbleStack.appendChild(bubble);

    // 超过上限移除最老的（在最上方）
    while (bubbleStack.children.length > MAX_BUBBLES) {
        bubbleStack.removeChild(bubbleStack.firstChild);
    }

    // 5 秒后自动消失
    setTimeout(() => {
        bubble.classList.add('fade-out');
        setTimeout(() => bubble.remove(), 1500);
    }, 5000);
}

// 流式取文本并更新气泡：逐字显示，完成后入库（historyPrefix 给历史加前缀，如 👀）
async function streamBubble(url, body, fallback, historyPrefix = '') {
    const bubble = addBubble('', false);
    let text = '';
    try {
        const res = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        if (!res.ok || !res.body) {
            bubble.textContent = fallback;
            recordHistory('pet', historyPrefix + fallback);
            return;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            text += decoder.decode(value, {stream: true});
            bubble.textContent = text;
        }
        const final = text.trim() || fallback;
        bubble.textContent = final;
        recordHistory('pet', historyPrefix + final);
        if (final.includes('哈哈')) { setState('laugh', 2000); playHouHouHou(); }
    } catch (e) {
        bubble.textContent = fallback;
        recordHistory('pet', historyPrefix + fallback);
    }
}

// ==================== 模式切换：对话模式退出后回到桌宠 → 重建桌面气泡 ====================
// 对话模式期间桌宠窗口是隐藏的（渲染进程照常跑，SSE/poke 等奶龙的台词生产仍由本窗口录入历史）。
// 回来时从服务端历史（和对话模式同一份数据）取最近奶龙发言重放一屏。
async function refreshDesktopBubbles() {
    // 切回桌宠 = 清场：气泡栈重建，确认层清空（对话模式期间的确认卡 confirm_id 早已过期/已放行，不能留）
    bubbleStack.innerHTML = '';
    confirmLayer.innerHTML = '';
    try {
        const res = await fetch(`${API_BASE}/history/list`, { cache: 'no-store' });
        const items = (await res.json()).items || [];
        let n = 0;
        for (let i = items.length - 1; i >= 0 && n < MAX_BUBBLES; i--) {
            const it = items[i];
            if (it && it.role === 'pet' && it.text) {
                addBubble(it.text, false);
                n++;
            }
        }
    } catch (e) {}
}

if (api.onPetRefresh) api.onPetRefresh(refreshDesktopBubbles);

// 确认卡片：写/命令类动手前问过主人，带「放行 / 算了」按钮。
// 独立放在 confirm-layer（不被气泡栈清走/挡住），点完立即有反应（禁用+改文案+发 POST）。
function showConfirmBubble(id, tool, detail) {
    const cn = AGENT_TOOL_CN[tool] || '一个操作';
    const card = document.createElement('div');
    card.className = 'confirm-card';

    const title = document.createElement('div');
    title.className = 'cc-title';
    title.textContent = `🙋 ${cn}，可以吗？`;
    const desc = document.createElement('div');
    desc.className = 'cc-desc';
    desc.textContent = detail || '需要动一下你的电脑，行不行？';

    const btns = document.createElement('div');
    btns.className = 'cc-btns';
    const yes = document.createElement('button');
    yes.className = 'confirm-btn yes';
    yes.textContent = '放行';
    const no = document.createElement('button');
    no.className = 'confirm-btn no';
    no.textContent = '算了';
    btns.append(yes, no);
    card.append(title, desc, btns);
    confirmLayer.appendChild(card);

    // 点了立刻有反应：禁用按钮、给反馈文案、发 POST 请求后端解冻
    const decide = async (approved) => {
        yes.disabled = no.disabled = true;
        let ok = false;
        try {
            const res = await fetch(`${API_BASE}/agent/confirm`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({confirm_id: id, approved}),
            });
            ok = res.ok;
        } catch (e) {
            desc.textContent = '⚠️ 没连上后端，稍后重试。';
            setTimeout(() => hideConfirmCard(card), 900);
            return;
        }
        if (ok) {
            desc.textContent = approved ? '✅ 已放行，我接着办…' : '🙅 你选了「算了」，我跳过这一步。';
            setTimeout(() => hideConfirmCard(card), ok ? 400 : 900);
        } else {
            desc.textContent = '⚠️ 这步有点超时了，我再想想别的招。';
            setTimeout(() => hideConfirmCard(card), ok ? 400 : 900);
        }
    };
    yes.addEventListener('click', () => decide(true));
    no.addEventListener('click', () => decide(false));
}

function hideConfirmCard(card) {
    card.classList.add('fading');
    setTimeout(() => card.remove(), 300);
}

// ==================== 输入框：点击显现，5 秒不操作自动隐藏 ====================
let inputHideTimer = null;

function showInput() {
    inputArea.classList.add('show');
    input.focus();
    clearTimeout(inputHideTimer);
    inputHideTimer = setTimeout(hideInput, 5000);
}

function hideInput() {
    if (input.value.trim()) return; // 有内容不隐藏
    inputArea.classList.remove('show');
}

// 点击容器内空白区域显示输入框；点工具栏按钮或任何面板不触发
document.getElementById('pet-container').addEventListener('click', (e) => {
    if (e.target.closest('.pet-btn') || e.target.closest('.pet-panel')) return;
    showToolbar();  // 点击容器 = 鼠标在场 → 工具栏与输入框一起浮出
}, { once: false });

input.addEventListener('input', () => {
    clearTimeout(inputHideTimer);
    inputHideTimer = setTimeout(hideInput, 5000);
});

// ==================== 发送消息 ====================

// 视觉意图正则（与后端关键词一致）：问"看见/在干嘛/屏幕是什么"这类问题时显示"让我看看…"。
// 注意：必须用整词短语（你在看/在干嘛），别用裸"干嘛/干啥/干什么"，否则"你能干嘛"迎匹配。
const VISION_INTENT_RE = /看见|看到|屏幕|截图|画面|你在看|看什么|在干嘛|在干啥|干嘛呢|干啥呢/;
// 注意：整理/统计/派活等任务指令不再本地拦截，全部交给后端统一 /chat 循环，
// 由 DeepSeek 自己判断"光聊"还是"先想再调工具"。"派活:"前缀会剥掉再发。

async function sendMsg() {
    let msg = input.value.trim();
    if (!msg) return;

    // "派活:"前缀只是路由信号，剥掉再发（后端统一循环自己判断光聊还是调工具）
    msg = msg.replace(/^派活[:：]\s*/, '') || msg;

    // 小任务口令：说一声就办，不走对话（用户口令同样进历史）
    // 1) "10秒后提醒我喝水" / "5分钟后提醒我开会"
    let m = msg.match(/^(\d+)\s*(分钟|秒)(?:后)?提醒我(.+)$/);
    if (m) {
        const delay = m[1] * (m[2] === '分钟' ? 60 : 1);
        recordHistory('user', msg);
        addBubble(msg, true);
        await addReminder(m[3].trim(), delay);
        return;
    }
    // 2) "提醒我5分钟后喝水" / "提醒我喝水"（默认 5 分钟）
    m = msg.match(/^提醒我(?:(\d+)\s*(分钟|秒))?后?(.+)$/);
    if (m && m[3].trim()) {
        const delay = m[1] ? m[1] * (m[2] === '分钟' ? 60 : 1) : 300;
        recordHistory('user', msg);
        addBubble(msg, true);
        await addReminder(m[3].trim(), delay);
        return;
    }
    // 3) "帮我记一下xxx" / "记住:xxx" → 速记
    m = msg.match(/^(?:帮我记一下|记一下|帮我记住|记住)[:：]?\s*(.+)$/);
    if (m && m[1].trim()) {
        recordHistory('user', msg);
        addBubble(msg, true);
        await addNote(m[1].trim());
        return;
    }
    // 4) 其余全部走统一 /chat：后端 DeepSeek 循环决定「光聊」还是「调工具」
    chatAgentRunning = true;  // 聊天流里会带 🤔/🔧/✅ 进度，全局 SSE 的 🔧 不再重复弹

    addBubble(msg, true);
    recordHistory('user', msg);
    input.value = '';
    showInput();  // 保持显示，方便继续对话（5 秒不操作才隐藏）

    // 占位气泡：先弹出来，回复边生成边往里填字
    // 注意：视觉调用可能要十几秒，气泡必须等到回复回来才消失，
    // 否则回复写进气泡时气泡已被移除，回复就整个看不到了
    const placeholder = document.createElement('div');
    placeholder.className = 'msg-bubble';
    placeholder.textContent = VISION_INTENT_RE.test(msg) ? '嗯…' : '等等…';
    bubbleStack.appendChild(placeholder);
    while (bubbleStack.children.length > MAX_BUBBLES) {
        bubbleStack.removeChild(bubbleStack.firstChild);
    }

    // 90 秒兜底：请求卡死也不至于气泡永远占位
    const hideTimer = setTimeout(fadePlaceholder, 90000);

    // ms 留空 = 立即消失；给数字 = 过那么久再淡出（用于长回复让人读完）
    function fadePlaceholder(ms) {
        clearTimeout(hideTimer);
        setTimeout(() => {
            placeholder.classList.add('fade-out');
            setTimeout(() => placeholder.remove(), 1500);
        }, ms || 0);
    }

    let reply = '';
    try {
        // 传最近对话给后端，让奶龙有上下文记忆
        const historyContext = history.slice(-MAX_CONTEXT);
        const res = await fetch(`${API_BASE}/chat`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message: msg, messages: historyContext})
        });
        if (!res.ok) throw new Error('bad response');

        // 兼容旧后端（整段 JSON 返回）；新后端是 text/plain 流式
        if ((res.headers.get('content-type') || '').includes('application/json')) {
            const data = await res.json();
            reply = data.reply || '';
            } else {
            // 流式读取：边收边显示，不用等整段回复
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            while (true) {
                const {done, value} = await reader.read();
                if (done) break;
                reply += decoder.decode(value, {stream: true});
                placeholder.textContent = stripMd(reply);  // 字一个一个长出来（且去掉 markdown 符号）
            }
        }
        reply = cleanReply(reply);  // 清掉模型顺手自签的"——DeepSeek"之类落款行（显示+历史一起清）
        placeholder.textContent = stripMd(reply);
        if (!reply) reply = '嗯？你刚才说啥？';
        recordHistory('pet', reply);

        // 回复里出现"哈哈"才触发大笑
        if (reply.includes('哈哈')) {
            setState('laugh', 2000);
            playHouHouHou();
        }
        // 回复到了：长文本给足阅读时间再淡出（短 10s，每多一个字多留 40ms），
        // 否则几十字的回复刚出现就消失，根本来不及读
        fadePlaceholder(Math.max(10000, reply.length * 40));
    } catch (e) {
        fadePlaceholder(8000);
        placeholder.textContent = '嗯……信号断了？';
        reply = '嗯……信号断了？';
        recordHistory('pet', reply);
    } finally {
        chatAgentRunning = false;  // 让 SSE 的 🔧/💭 气泡恢复弹出
    }
}

input.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') sendMsg();
});

// ==================== 打开独立面板窗口（桌宠模式只保留：历史对话） ====================
// 智能体/提醒/速记面板按钮已撤下：功能区由对话模式内的原生视图提供
historyBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    e.preventDefault();
    api.openPanel('history');
});

chatBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    e.preventDefault();
    api.openChatWindow();
});

// ==================== 提醒/速记快捷指令（从输入框触发） ====================
function fmtDelay(sec) {
    if (sec < 60) return `${sec}秒后`;
    return `${Math.round(sec / 60)}分钟后`;
}

async function addReminder(text, delaySeconds) {
    try {
        await fetch(`${API_BASE}/reminders`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text, delay_seconds: delaySeconds})
        });
        addBubble(`⏰ 好的，${fmtDelay(delaySeconds)}提醒你：${text}`, false);
        recordHistory('pet', `⏰ ${fmtDelay(delaySeconds)}提醒你：${text}`);
    } catch (e) {
        addBubble('提醒没设成…', false);  // 失败话是 UI 反馈，不入角色历史
    }
}

async function addNote(text) {
    try {
        await fetch(`${API_BASE}/notes`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text})
        });
        addBubble(`📝 记下了：${text}`, false);
        recordHistory('pet', `📝 记下了：${text}`);
    } catch (e) {
        addBubble('速记没存上…', false);
    }
}

// ==================== 互动冷却：触发互动后几秒内不再响应点击，防止连点出多个气泡 ====================
let lastInteractionAt = 0;
const INTERACTION_COOLDOWN = 3000;  // 毫秒

function allowInteraction() {
    const now = Date.now();
    if (now - lastInteractionAt < INTERACTION_COOLDOWN) return false;
    lastInteractionAt = now;
    return true;
}

// ==================== 看屏幕按钮：奶龙看一眼你的屏幕 ====================
// 等待回复期间禁用：防连点刷屏，直到拿到回复（或失败重试）才解禁
let eyeBusy = false;
eyeBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    e.preventDefault();
    if (eyeBusy) return;
    eyeBusy = true;
    eyeBtn.disabled = true;
    eyeBtn.classList.add('busy');
    addBubble('嗯…', false);
    await streamBubble(`${API_BASE}/vision`, {}, '嗯…眼睛没睁开。', '👀 ');
    eyeBusy = false;
    eyeBtn.disabled = false;
    eyeBtn.classList.remove('busy');
});

// ==================== 关闭 ====================
closeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    api.closePet();
});

// ==================== 单击：跳舞 + 显示输入框 ====================
let suppressClick = false;

petBody.addEventListener('click', async (e) => {
    if (e.target.closest('#close-btn')) return;
    if (suppressClick) { suppressClick = false; return; }
    if (!allowInteraction()) return;  // 冷却中：不响应点击（防连点出多个气泡）
    e.stopPropagation();

    playHouHouHou();
    setState('dance', 2000);
    showInput();

    await streamBubble(`${API_BASE}/action`, {action: 'poke'}, '嗯？你戳我？');
});

// ==================== 双击：大笑 ====================
petBody.addEventListener('dblclick', (e) => {
    if (e.target.closest('.pet-btn')) return;
    e.stopPropagation();
    setState('laugh', 2000);
    playHouHouHou();
});

// ==================== 长按：变形 ====================
let longPressTimer = null;

petBody.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if (e.target.closest('.pet-btn')) return;  // 点工具栏按钮不算戳/拖
    longPressTimer = setTimeout(async () => {
        if (!allowInteraction()) return;  // 冷却中：不触发长按反应
        setState('deform', 2000);
        suppressClick = true;
        // 长按也不再用固定台词，交给后端 LLM 现场流式生成反应
        await streamBubble(`${API_BASE}/action`, {action: 'long_press'}, '嘿，干嘛呢…');
    }, 500);
});

petBody.addEventListener('mouseup', () => {
    clearTimeout(longPressTimer);
    if (didDrag) suppressClick = true;
});

petBody.addEventListener('mouseleave', () => {
    clearTimeout(longPressTimer);
});

// ==================== 拖动窗口（抓住奶龙拖动，不是拖图片文件） ====================
let isDragging = false;
let didDrag = false;
let dragStartX, dragStartY;

petBody.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if (e.target.closest('.pet-btn')) return;  // 点工具栏按钮不算拖
    isDragging = true;
    didDrag = false;
    dragStartX = e.screenX;
    dragStartY = e.screenY;
});

document.addEventListener('mousemove', (e) => {
    // 拖动时保持接收鼠标事件，并移动窗口
    if (isDragging) {
        const dx = e.screenX - dragStartX;
        const dy = e.screenY - dragStartY;
        dragStartX = e.screenX;
        dragStartY = e.screenY;
        api.movePet(dx, dy);
        if (Math.abs(dx) > 3 || Math.abs(dy) > 3) didDrag = true;
        api.setClickThrough(false);
        return;
    }

    // 根据鼠标位置决定透明区域是否穿透（奶龙/输入框/工具栏/确认卡不穿透）
    // 注意：mousemove 即使窗口穿透也会继续收到（forward:true），所以鼠标移到
    // confirm-layer 时必须关闭穿透，否则 click 被穿到身后的窗口、按钮永远点不了。
    function pointInRect(x, y, r) {
        return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
    }
    const overPet = pointInRect(e.clientX, e.clientY, petBody.getBoundingClientRect());
    const inputVisible = inputArea.classList.contains('show');
    const overInput = inputVisible && pointInRect(e.clientX, e.clientY, inputArea.getBoundingClientRect());
    const overToolbar = [btnCol, btnColRight].some(c => !c.classList.contains('hidden') && pointInRect(e.clientX, e.clientY, c.getBoundingClientRect()));
    const overConfirm = pointInRect(e.clientX, e.clientY, confirmLayer.getBoundingClientRect());
    api.setClickThrough(!(overPet || overInput || overToolbar || overConfirm));

    // 工具栏显隐：鼠标在奶龙附近（含两侧工具栏、下方输入框）时显示。
    // 隐藏不再靠「非 near 时安排」，而是靠下面的独立轮询 + mouseleave/blur：
    // 只要鼠标停在奶龙附近不动，按钮就一直显示；离开/太久没活动 → 5 秒后隐藏。
    const pr = petBody.getBoundingClientRect();
    const m = 50;
    const near = e.clientX >= pr.left - m && e.clientX <= pr.right + m
               && e.clientY >= pr.top - m && e.clientY <= pr.bottom + m;
    lastSeenAt = Date.now();  // 只要鼠标在本窗口内移动过，就刷新「活跃时间」
    if (near) {
        showToolbar();
        showInput();   // 鼠标悬停奶龙附近 → 输入框也出现（和 toolbar 同步显隐）
    }
});

document.addEventListener('mouseup', () => {
    isDragging = false;
});

// ==================== 状态轮询 ====================
async function pollStatus() {
    try {
        const res = await fetch(`${API_BASE}/status`);
        const data = await res.json();
        if (data.presence < 30) {
            pet.classList.add('faint');
        } else {
            pet.classList.remove('faint');
        }
    } catch (e) {
        // 后端没开，静默失败
    }
}
setInterval(pollStatus, 5000);
pollStatus();
