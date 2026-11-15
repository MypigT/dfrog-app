// ==================== 奶龙 · 对话模式（大窗口） ====================
// 布局：左 sidebar（奶龙图片，点击/双击/长按同桌面交互 + 切换右侧区域的按钮）；
// 右 stage（💬 对话视图 默认 / 🤖 智能体、⏰ 提醒、📝 速记 = 对话窗口内原生实现的功能视图
//          / 👀 看屏幕 = 立即动作投到对话视图）。
// 数据统一：服务端 /history/list 一份，桌宠模式与本模式共用，只是显示方式不同。
//   「生产」奶龙台词的动作（/chat 回复、戳/长按反应、👀 评论、口令反馈）由本窗口入库；
//   「搬运」的历史事件（SSE speak/action/reminder/🔧💭）本窗口只补显示，入库由桌宠窗口（隐藏也照常）负责。
// 对话模式约定：奶龙说话不弹桌宠气泡，全部进对话视图；奶龙互动与桌面同一套形态切换（换图 + 动画 + 音效）。

const HIST_DISPLAY = 120;

// 兼容非 Electron 环境（浏览器直接打开调试用）
const api = window.electronAPI || {
    closeChatWindow: () => {},
    onPetRefresh: () => {},
    setPetChatRunning: () => {},
    setChatTop: () => {},
    onChatTopState: () => {},
};

const messagesEl = document.getElementById('chat-messages');
const inputEl = document.getElementById('chat-input');
const sendBtn = document.getElementById('chat-send-btn');
const exitBtn = document.getElementById('exit-mode');
const closeBtn = document.getElementById('chat-close-btn');

const petImg = document.getElementById('pet-img');
const petStage = document.getElementById('pet-stage');

const navBtns = document.querySelectorAll('.nav-btn');

// ==================== RAG 文档问答：工具条 ====================
const ragUploadBtn = document.getElementById('rag-upload-btn');
const ragFileInput = document.getElementById('rag-file-input');
const ragStatusEl = document.getElementById('rag-doc-status');
const ragProgressEl = document.getElementById('rag-progress');
const ragModeBtns = document.querySelectorAll('.rag-mode-btn');
const ragClearBtn = document.getElementById('rag-clear-btn');

let ragDoc = null;   // 当前加载的文档 {doc_id, doc_name, chunks}
let ragMode = 'ask'; // 'ask' 问答 | 'quiz' 出题

// ==================== 多会话 ====================
const sessionBar = document.getElementById('session-bar');
const sessionSelect = document.getElementById('session-select');
const sessionNewBtn = document.getElementById('session-new-btn');
const sessionDelBtn = document.getElementById('session-del-btn');

let currentSessionId = null;  // 当前会话 id（与后端 active_id 一致）
let sessions = [];            // 会话列表 [{id,title,count,updated_at,active}]

function focusInput() { try { inputEl.focus(); } catch (e) {} }

// ==================== 渲染 ====================
function appendMsg(role, text) {
    const row = document.createElement('div');
    row.className = 'msg-row ' + role;
    const avatar = document.createElement('div');
    avatar.className = 'msg-avatar';
    avatar.textContent = role === 'user' ? '🙋' : '🐲';
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    bubble.textContent = String(text || '').trim();
    row.appendChild(avatar);
    row.appendChild(bubble);
    messagesEl.appendChild(row);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return bubble;
}

function sysMsg(text) {
    const st = document.createElement('span');
    st.className = 'msg-system';
    st.textContent = text;
    messagesEl.appendChild(st);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function clearMessages() { messagesEl.innerHTML = ''; }

// ==================== 视图切换：对话 / 功能视图（原生 section，非 iframe） ====================
// （对话视图本身就是完整历史流，不再单设「历史」视图）
const viewEls = {
    chat: document.getElementById('view-chat'),
    agent: document.getElementById('view-agent'),
    reminder: document.getElementById('view-reminder'),
    notes: document.getElementById('view-notes'),
    petimg: document.getElementById('view-petimg'),
    settings: document.getElementById('view-settings'),
};

function showView(name) {
    Object.entries(viewEls).forEach(([k, el]) => {
        if (el) el.classList.toggle('active', k === name);
    });
    navBtns.forEach((b) => b.classList.toggle('active', b.dataset.view === name));
    if (settingsBtn) settingsBtn.classList.toggle('active', name === 'settings');
    if (name === 'chat') focusInput();
    else window.dispatchEvent(new CustomEvent('dfrog-view-shown', { detail: { view: name } }));  // 通知 chatpanels 刷新列表
}

navBtns.forEach((b) => {
    b.addEventListener('click', () => {
        const v = b.dataset.view;
        showView(v);
        if (v === 'chat') {
            // 切回对话视图 = 已经看过：清红点 + 重建最新历史
            unreadMsgs = 0;
            updateBadge();
            rebuildHistory();
        }
    });
});

// 👀 看屏幕：独立小按钮（非视图），点一下立即看一眼
document.getElementById('eye-btn').addEventListener('click', doLook);

exitBtn.addEventListener('click', () => api.closeChatWindow());
closeBtn.addEventListener('click', () => api.closeChatWindow());

// ==================== 标题栏 📌 置顶开关 ====================
// 点击切换窗口置顶（主进程 setAlwaysOnTop）；开关状态随存档恢复（主进程 did-finish-load 回推）。
const topBtn = document.getElementById('chat-top-btn');

function syncTopBtn(on) {
    topBtn.classList.toggle('on', !!on);
    topBtn.title = on ? '置顶中（点击取消）' : '置顶开关';
}

topBtn.addEventListener('click', () => {
    const on = !topBtn.classList.contains('on');
    syncTopBtn(on);
    syncTopSwitch();
    if (api.setChatTop) api.setChatTop(on);
});
if (api.onChatTopState) api.onChatTopState((on) => { syncTopBtn(on); syncTopSwitch(); });

// ==================== 未读新消息红点 ====================
// 有新消息（服务端历史条数变多）且当前不在对话视图查看时，💬 按钮右上角显示条数。
let knownHistCount = 0;
let unreadMsgs = 0;
let localHistory = [];  // 本地缓存的历史，发送时用作上下文，避免每次重新拉取

function updateBadge() {
    const badge = document.getElementById('chat-badge');
    if (!badge) return;
    if (unreadMsgs > 0) {
        badge.textContent = unreadMsgs > 99 ? '99+' : String(unreadMsgs);
        badge.classList.add('show');
    } else {
        badge.classList.remove('show');
    }
}

// ==================== 历史（服务端为准，本地兜底） ====================
async function fetchHistory() {
    try {
        const q = currentSessionId ? `?session_id=${encodeURIComponent(currentSessionId)}` : '';
        const res = await fetch(`${API_BASE}/history/list${q}`, { cache: 'no-store' });
        if (!res.ok) throw new Error('bad history');
        const data = await res.json();
        return (data.items || []).map((it) => {
            const role = (it && it.role === 'user') ? 'user' : 'pet';
            return { role, text: (it.text || '').slice(0, 500) };
        }).filter((it) => it.text);
    } catch (e) {
        return loadLocalHistory();
    }
}

async function recordAdd(role, text) {
    text = String(text || '').trim();
    if (!text) return;
    try {
        const body = { role, text };
        if (currentSessionId) body.session_id = currentSessionId;
        await fetch(`${API_BASE}/history/add`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        knownHistCount++;   // 本窗口生产的消息也入库：同步计数防红点误增
    } catch (e) { addLocalHistory(role, text); }
}

function loadLocalHistory() {
    try {
        const raw = localStorage.getItem('pet_history');
        if (!raw) return [];
        return JSON.parse(raw).map((it) => {
            const role = (it && it.role === 'user') ? 'user' : 'pet';
            return { role, text: (it.text || '').slice(0, 500) };
        }).filter((it) => it.text);
    } catch (e) { return []; }
}

function addLocalHistory(role, text) {
    try {
        const items = JSON.parse(localStorage.getItem('pet_history') || '[]');
        items.push({ role, text });
        while (items.length > 300) items.shift();
        localStorage.setItem('pet_history', JSON.stringify(items));
    } catch (e) {}
}

// ==================== 多会话：列表 / 切换 / 新建 / 删除 ====================
async function fetchSessions() {
    try {
        const res = await fetch(`${API_BASE}/history/sessions`, { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        sessions = data.items || [];
    } catch (e) {}
}

function renderSessionBar() {
    sessionSelect.innerHTML = '';
    if (!sessions.length) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '无会话';
        sessionSelect.appendChild(opt);
        sessionSelect.value = '';
        return;
    }
    sessions.forEach((s) => {
        const opt = document.createElement('option');
        opt.value = s.id;
        opt.textContent = `${s.title}（${s.count}）`;
        sessionSelect.appendChild(opt);
    });
    sessionSelect.value = currentSessionId || (sessions[0] && sessions[0].id) || '';
}

// 切换会话：通知后端置为当前 + 重载该会话消息
async function switchSession(sid) {
    if (sid === currentSessionId) return;
    currentSessionId = sid;
    try {
        await fetch(`${API_BASE}/history/sessions/set-active`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sid }),
        });
    } catch (e) {}
    await rebuildHistory();
    renderSessionBar();
}

sessionSelect.addEventListener('change', () => { switchSession(sessionSelect.value); });

sessionNewBtn.addEventListener('click', async () => {
    try {
        const res = await fetch(`${API_BASE}/history/sessions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        const data = await res.json();
        if (!data.id) return;
        currentSessionId = data.id;
        await fetchSessions();
        renderSessionBar();
        clearMessages();
        sysMsg('新的会话，跟桌宠说点什么吧');
        focusInput();
    } catch (e) {}
});

sessionDelBtn.addEventListener('click', async () => {
    if (!currentSessionId) return;
    try {
        const res = await fetch(`${API_BASE}/history/sessions/delete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: currentSessionId }),
        });
        const data = await res.json();
        if (!data.deleted) return;
        await fetchSessions();
        // 删的是当前会话：后端已自动切到另一个，校正前端当前会话
        const active = sessions.find((s) => s.active) || sessions[0];
        currentSessionId = active ? active.id : null;
        renderSessionBar();
        await rebuildHistory();
    } catch (e) {}
});

let chatting = false;

async function rebuildHistory() {
    if (chatting) return;   // 正在流式回复：别冲掉对话框
    const items = await fetchHistory();
    localHistory = items.slice();
    knownHistCount = items.length;
    unreadMsgs = 0;
    updateBadge();
    const shown = items.slice(-HIST_DISPLAY);
    clearMessages();
    if (!shown.length) {
        sysMsg('最近还没有对话，跟桌宠说点什么吧');
        return;
    }
    for (const it of shown) appendMsg(it.role, it.text);
}

// ==================== 智能体确认卡（写/命令类动手前问过主人） ====================
async function showConfirmCard(confirmId, tool, detail) {
    const cn = AGENT_TOOL_CN[tool] || '一个操作';
    const row = document.createElement('div');
    row.className = 'confirm-row';
    const card = document.createElement('div');
    card.className = 'confirm-card';
    const title = document.createElement('div');
    title.className = 'cc-title';
    title.textContent = `「${cn}」动手前问我一声：`;
    const desc = document.createElement('div');
    desc.className = 'cc-desc';
    desc.textContent = String(detail || '').slice(0, 300);
    const btns = document.createElement('div');
    btns.className = 'cc-btns';
    const yes = document.createElement('button');
    yes.className = 'confirm-btn yes';
    yes.textContent = '放行';
    const no = document.createElement('button');
    no.className = 'confirm-btn no';
    no.textContent = '算了';
    btns.appendChild(yes);
    btns.appendChild(no);
    card.appendChild(title);
    card.appendChild(desc);
    card.appendChild(btns);
    row.appendChild(card);
    const decide = async (approved) => {
        yes.disabled = no.disabled = true;
        await apiConfirm(confirmId, approved);
        card.classList.add('done');
        sysMsg(approved ? '已放行，操作继续' : '已拒绝，操作取消');
        setTimeout(() => row.remove(), 6000);
    };
    yes.addEventListener('click', () => decide(true));
    no.addEventListener('click', () => decide(false));
    messagesEl.appendChild(row);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function apiConfirm(confirmId, approved) {
    try {
        await fetch(`${API_BASE}/agent/confirm`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ confirm_id: confirmId, approved }),
        });
    } catch (e) {}
}

// ==================== 侧栏奶龙：和桌面同一套交互 ====================
// 点一下戳 → 跳舞；双击 → 大笑；长按 500ms → 变形。
// 形态切换与桌宠一致：换图 + CSS 动画 + 音效（桌宠模式是切 dance.jpg 等图，这里同样要"变"）。
// 台词不弹桌宠气泡，全部进对话视图 + 历史（本模式奶龙的生产动作由本窗口入库，
// 桌宠窗口此时是隐藏的、不会重复写）。
const INTERACTION_COOLDOWN = 3000;
let lastInteract = 0;

function allowInteract() {
    const now = Date.now();
    if (now - lastInteract < INTERACTION_COOLDOWN) return false;
    lastInteract = now;
    return true;
}

// 形态切换：有桌宠形象动图则播放 APNG，无则回退静态图+CSS动画（同 pet.js）
const STATES = {
    idle: '../assets/common.jpg',
    dance: '../assets/dance.jpg',
    laugh: '../assets/happy.jpg',
    deform: '../assets/doi.jpg',
};
const STATE_CLASS = { dance: 'dancing', laugh: 'laughing', deform: 'deformed' };
const ANIM_SLOTS = ['idle', 'dance', 'laugh', 'deform'];
const animMap = {};  // state -> 动图 url（从后端 /pet/avatar/current 加载）
let currentState = 'idle';

async function loadPetAnimations() {
    // 启动时先隐藏，等选中形象加载完（onload）再显示，避免先闪默认图
    petImg.onload = () => { petImg.style.visibility = 'visible'; };
    let useAvatar = false;
    try {
        const res = await fetch(`${API_BASE}/pet/avatar/current`, { cache: 'no-store' });
        const data = await res.json();
        (data.manifest || []).forEach((a, i) => {
            if (i < ANIM_SLOTS.length && a.url) {
                animMap[ANIM_SLOTS[i]] = API_BASE + a.url;  // 加 API_BASE：file:// 下相对路径会解析错
            }
        });
        if (animMap.idle) {
            useAvatar = true;
            petImg.src = animMap.idle;
            petImg.className = 'anim-playing';
        }
        console.log('[对话] 加载桌宠动图:', animMap);
    } catch (e) {
        console.log('[对话] 无桌宠动图，使用静态图+CSS动画');
    }
    if (!useAvatar) {
        petImg.src = '../assets/common.jpg';
        petImg.className = '';
        petImg.style.visibility = 'visible';
    }
}
loadPetAnimations();

function setState(state, duration = 0) {
    if (currentState === state) return;
    currentState = state;
    const animPath = animMap[state];
    if (animPath) {
        petImg.src = animPath;
        petImg.className = 'anim-playing';
    } else {
        petImg.src = STATES[state];
        petImg.className = STATE_CLASS[state] || '';
    }

    if (duration > 0) {
        setTimeout(() => {
            if (currentState === state) setState('idle');
        }, duration);
    }
}

// 播放齁齁齁音效（同桌面）
let MUTED = false;
function playHouHouHou() {
    if (MUTED) return;
    const audio = new Audio('../assets/houhouhou.wav');
    audio.volume = 0.6;
    audio.play().catch(e => console.log('音效播放失败:', e));
}

// ==================== 设置（右侧视图，与右上角小按钮同步）====================
const SETTING_KEYS = ['auto_start', 'mute', 'stream_output', 'proactive'];

// 右上角静音小按钮
const muteBtn = document.getElementById('mute-btn');
function syncMuteBtn() {
    if (!muteBtn) return;
    muteBtn.textContent = MUTED ? '🔇' : '🔊';
    muteBtn.classList.toggle('on', MUTED);
    muteBtn.title = MUTED ? '已静音（点击开声）' : '静音开关';
}
function syncMuteSwitch() {
    const el = document.getElementById('set-mute');
    if (el) el.checked = MUTED;
}
function syncTopSwitch() {
    const el = document.getElementById('set-always_on_top');
    if (el) el.checked = topBtn.classList.contains('on');
}

if (muteBtn) {
    muteBtn.addEventListener('click', async () => {
        try {
            const res = await fetch(`${API_BASE}/settings`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({settings: {mute: !MUTED}}),
            });
            const data = await res.json();
            MUTED = !!data.settings.mute;
            syncMuteBtn();
            syncMuteSwitch();
        } catch (e) {}
    });
}

// 启动时拉一次设置，初始化静音
fetch(`${API_BASE}/settings`).then(r => r.json()).then(s => { MUTED = !!s.mute; syncMuteBtn(); }).catch(() => {});

// 设置视图：进入时加载设置 + 同步置顶/静音开关
async function loadSettingsView() {
    try {
        const res = await fetch(`${API_BASE}/settings`);
        const s = await res.json();
        SETTING_KEYS.forEach(k => {
            const el = document.getElementById('set-' + k);
            if (el) el.checked = !!s[k];
        });
    } catch (e) {}
    syncTopSwitch();
    syncMuteSwitch();
    renderModelConfigList(document.getElementById('model-config-list'));
}

// 齿轮按钮 → 切到设置视图
const settingsBtn = document.getElementById('settings-btn');
if (settingsBtn) {
    settingsBtn.addEventListener('click', () => {
        showView('settings');
        loadSettingsView();
    });
}

// 常规开关（写 settings.json）
SETTING_KEYS.forEach(k => {
    const el = document.getElementById('set-' + k);
    if (!el) return;
    el.addEventListener('change', async () => {
        try {
            const res = await fetch(`${API_BASE}/settings`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({settings: {[k]: el.checked}}),
            });
            await res.json();
            if (k === 'mute') { MUTED = el.checked; syncMuteBtn(); }
        } catch (e) {}
    });
});

// 置顶开关（对话窗置顶，走 IPC 与右上角 📌 同步，不写 settings.json）
const topSwitch = document.getElementById('set-always_on_top');
if (topSwitch) {
    topSwitch.addEventListener('change', () => {
        syncTopBtn(topSwitch.checked);
        if (api.setChatTop) api.setChatTop(topSwitch.checked);
    });
}

async function petReply(actionName, fallback) {
    // 流式生成反应 → 对话视图逐字显示 + 历史 一条链路（单入口，避免双写）
    const bubble = appendMsg('pet', '');
    let text = '';
    try {
        const res = await fetch(`${API_BASE}/action`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: actionName }),
        });
        if (!res.ok || !res.body) {
            const fb = cleanReply(fallback) || fallback;
            bubble.textContent = fb;
            recordAdd('pet', fb);
            return;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            text += decoder.decode(value, {stream: true});
            bubble.textContent = text;
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }
        const final = cleanReply(text) || fallback;
        bubble.textContent = final;
        recordAdd('pet', final);
        if (final.includes('哈哈')) {
            setState('laugh', 2000);
            playHouHouHou();
        }
    } catch (e) {
        const fb = cleanReply(fallback) || fallback;
        bubble.textContent = fb;
        recordAdd('pet', fb);
    }
}

petStage.addEventListener('click', () => {
    if (!allowInteract()) return;
    setState('dance', 2000);
    playHouHouHou();
    petReply('poke', '嗯？你戳我？');
});

petStage.addEventListener('dblclick', () => {
    setState('laugh', 2000);
    playHouHouHou();
});

let longPressTimer = null;
petStage.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if (!allowInteract()) return;
    longPressTimer = setTimeout(() => {
        longPressTimer = null;
        setState('deform', 2000);
        petReply('long_press', '嘿，干嘛呢…');
    }, 500);
});
petStage.addEventListener('mouseup', () => clearTimeout(longPressTimer));
petStage.addEventListener('mouseleave', () => clearTimeout(longPressTimer));

// ==================== 👀 看屏幕：不是视图，点击立即看一眼，结果投到对话视图 ====================
let eyeOpen = false;
async function doLook() {
    if (eyeOpen) return;
    eyeOpen = true;
    const btn = document.getElementById('eye-btn');
    if (btn) { btn.classList.add('active'); btn.disabled = true; }
    sysMsg('👀 让我看看…');   // 等待反馈走对话里的系统消息，不弹桌宠气泡
    const bubble = appendMsg('pet', '');
    let text = '';
    try {
        const res = await fetch(`${API_BASE}/vision`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        if (!res.ok || !res.body) {
            const fb = '👀 嗯…眼睛没睁开。';
            bubble.textContent = fb;
            recordAdd('pet', fb);
        } else {
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            while (true) {
                const {done, value} = await reader.read();
                if (done) break;
                text += decoder.decode(value, {stream: true});
                bubble.textContent = '👀 ' + text;
                messagesEl.scrollTop = messagesEl.scrollHeight;
            }
            const reply = cleanReply('👀 ' + text.trim()) || '👀 嗯…眼睛没睁开。';
            bubble.textContent = reply;
            recordAdd('pet', reply);
            if (reply.includes('哈哈')) {
                setState('laugh', 2000);
                playHouHouHou();
            }
        }
    } catch (e) {
        const fb = '👀 嗯…眼睛没睁开。';
        bubble.textContent = fb;
        recordAdd('pet', fb);
    } finally {
        eyeOpen = false;
        if (btn) { btn.classList.remove('active'); btn.disabled = false; }
    }
}

// ==================== 发送 ====================
const WAIT = '等待回复中…';
const ERR = '嗯…信号断了？';
const EMPTY = '嗯？你刚才说啥？';

function setBusy(b) {
    chatting = b;
    sendBtn.disabled = b;
    inputEl.disabled = b;
}

async function sendMsg() {
    const msg = inputEl.value.trim();
    if (!msg || chatting) return;
    inputEl.value = '';
    appendMsg('user', msg);
    localHistory.push({role: 'user', text: msg});
    try { await recordAdd('user', msg); } catch (e) {}

    // 命令口令：提醒 / 速记（和桌宠窗口同一套正则）
    const setRem = msg.match(/^提醒我(?:(\d+)\s*(分钟|秒))?后?(.+)$/);
    if (setRem && setRem[3].trim()) {
        await doReminder(setRem[3].trim(), setRem[1] ? setRem[1] * (setRem[2] === '分钟' ? 60 : 1) : 300);
        return;
    }
    const rec = msg.match(/^(\d+)\s*(分钟|秒)(?:后)?提醒我(.+)$/);
    if (rec) {
        await doReminder(rec[4].trim(), rec[1] * (rec[2] === '分钟' ? 60 : 1));
        return;
    }
    const not = msg.match(/^(?:帮我记一下|记一下|帮我记住|记住)[:：]?\s*(.+)$/);
    if (not && not[1].trim()) {
        await doNote(not[1].trim());
        return;
    }

    setBusy(true);
    // RAG 文档问答：有文档加载时路由到 /rag/ask 或 /rag/quiz，否则走普通 /chat
    const ragEndpoint = ragDoc ? (ragMode === 'quiz' ? '/rag/quiz' : '/rag/ask') : null;
    // 通知桌宠「聊天流在跑」：期间 🔧/💭 进度气泡不再入库，避免和本条流重复记录
    if (api.setPetChatRunning) api.setPetChatRunning(true);
    const placeholder = appendMsg('pet', WAIT);
    let reply = '';
    try {
        const body = ragEndpoint
            ? { message: msg }
            : { message: msg, messages: localHistory.slice(-10) };
        const data = await fetch(`${API_BASE}${ragEndpoint || '/chat'}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const reader = data.body.getReader();
        const decoder = new TextDecoder();
        for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            reply += decoder.decode(value, { stream: true });
            placeholder.textContent = stripMd(reply);
        }
        reply = cleanReply(reply);
        if (!reply) reply = EMPTY;
        placeholder.textContent = reply;
        localHistory.push({role: 'pet', text: reply});
        await recordAdd('pet', reply);
        if (reply.includes('哈哈')) {
            setState('laugh', 2000);
            playHouHouHou();
        }
    } catch (e) {
        if (placeholder) placeholder.textContent = ERR;
        reply = ERR;
        try { await recordAdd('pet', reply); } catch (e2) {}
    } finally {
        setBusy(false);
        if (api.setPetChatRunning) api.setPetChatRunning(false);
        focusInput();
    }
}

sendBtn.addEventListener('click', sendMsg);
inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendMsg(); }
});

// ==================== RAG 文档问答：上传 / 模式 / 清除 ====================
function updateRagStatus() {
    if (ragDoc) {
        ragStatusEl.textContent = `📄 ${ragDoc.doc_name}（${ragDoc.chunks} 块）`;
        ragStatusEl.classList.add('loaded');
        ragClearBtn.hidden = false;
    } else {
        ragStatusEl.textContent = '未加载文档';
        ragStatusEl.classList.remove('loaded');
        ragClearBtn.hidden = true;
    }
}

function setRagMode(mode) {
    ragMode = mode;
    ragModeBtns.forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
}

ragModeBtns.forEach((b) => b.addEventListener('click', () => setRagMode(b.dataset.mode)));

ragUploadBtn.addEventListener('click', () => ragFileInput.click());

ragFileInput.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    e.target.value = '';  // 允许重复选同一文件
    if (!file) return;
    if (chatting) { sysMsg('正在回复中，等一下再上传～'); return; }
    // 换文档覆盖：先删旧的
    if (ragDoc) {
        try {
            await fetch(`${API_BASE}/rag/delete`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ doc_id: ragDoc.doc_id }),
            });
        } catch (err) {}
        ragDoc = null;
    }
    const fd = new FormData();
    fd.append('file', file);
    ragProgressEl.hidden = false;
    ragProgressEl.textContent = `📄 正在处理「${file.name}」…`;
    sysMsg(`📄 正在处理「${file.name}」…`);

    // 启动上传；同时轮询 /rag/upload/status，大扫描 PDF 会逐页显示「第 X/Y 页」
    let uploadDone = false;
    const uploadP = fetch(`${API_BASE}/rag/upload`, { method: 'POST', body: fd })
        .then(async (res) => { const d = await res.json(); uploadDone = true; return d; })
        .catch((err) => { uploadDone = true; throw err; });

    (async () => {
        while (!uploadDone) {
            try {
                const sr = await fetch(`${API_BASE}/rag/upload/status`, { cache: 'no-store' });
                const st = await sr.json();
                if (st.stage === 'processing' && st.total > 0) {
                    ragProgressEl.textContent = `📄 正在识别「${file.name}」… 第 ${st.done}/${st.total} 页`;
                }
            } catch (err) {}
            await new Promise((r) => setTimeout(r, 2000));
        }
    })();

    try {
        const data = await uploadP;
        if (data.error) throw new Error(data.error);
        ragDoc = { doc_id: data.doc_id, doc_name: data.doc_name, chunks: data.chunks };
        updateRagStatus();
        ragProgressEl.hidden = true;
        if (data.cached) {
            sysMsg(`✅ 复用缓存：${data.doc_name}（${data.chunks} 块），未再调用模型识别`);
        } else {
            sysMsg(`✅ 文档已入库：${data.doc_name}（${data.chunks} 块），可以问它了`);
        }
    } catch (err) {
        ragProgressEl.hidden = true;
        sysMsg(`❌ 文档处理失败：${err.message || err}`);
    }
});

ragClearBtn.addEventListener('click', async () => {
    if (!ragDoc) return;
    try {
        await fetch(`${API_BASE}/rag/delete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ doc_id: ragDoc.doc_id }),
        });
    } catch (err) {}
    ragDoc = null;
    updateRagStatus();
    sysMsg('文档已清除，回到普通对话');
});

// 启动时恢复已入库的文档（保持一个活跃文档）
async function restoreRagDoc() {
    try {
        const res = await fetch(`${API_BASE}/rag/list`, { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        const items = data.items || [];
        if (items.length) {
            ragDoc = items[0];
            updateRagStatus();
        }
    } catch (e) {}
}

// ==================== 命令动作 ====================
async function doReminder(text, delaySeconds) {
    try {
        await fetch(`${API_BASE}/reminders`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, delay_seconds: delaySeconds }),
        });
        const reply = `⏰ 好的，${fmtDelay(delaySeconds)}提醒你：${text}`;
        appendMsg('pet', reply);
        recordAdd('pet', reply);
    } catch (e) {
        appendMsg('pet', '提醒没设成…');
    }
}

async function doNote(text) {
    try {
        await fetch(`${API_BASE}/notes`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
        });
        const reply = `📝 记下了：${text}`;
        appendMsg('pet', reply);
        recordAdd('pet', reply);
    } catch (e) {
        appendMsg('pet', '速记没存上…');
    }
}

function fmtDelay(sec) {
    if (sec < 60) return `${sec}秒后`;
    return `${Math.round(sec / 60)}分钟后`;
}

// ==================== SSE：与桌宠窗口订阅同一事件流 ====================
// history  → 有新消息：在对话视图则同步重建，不在则计未读红点
// speak/action/reminder → 桌宠窗口已入历史，这里只补显示
// agent_confirm → 确认卡
// agent_action/thought → 对话流内已在显示，跳过
async function onHistoryEvent() {
    // 会话列表可能变（桌宠窗口新建/删除/切换等），先刷新；当前会话被切走/删掉则校正
    await fetchSessions();
    renderSessionBar();
    const active = sessions.find((s) => s.active) || sessions[0];
    if (active && active.id !== currentSessionId) {
        currentSessionId = active.id;
        sessionSelect.value = currentSessionId;
    }
    let n;
    try {
        const q = currentSessionId ? `?session_id=${encodeURIComponent(currentSessionId)}` : '';
        const res = await fetch(`${API_BASE}/history/list${q}`, { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        n = (data.items || []).length;
    } catch (e) { return; }
    if (document.getElementById('view-chat').classList.contains('active')) {
        // 正在查看对话视图：同步计数；不在流式回复中则重建显示
        knownHistCount = n;
        unreadMsgs = 0;
        updateBadge();
        if (!chatting) await rebuildHistory();
    } else {
        // 没在对话视图查看：增量计入未读
        unreadMsgs = Math.max(0, n - knownHistCount);
        updateBadge();
    }
}

new EventSource(`${API_BASE}/events`)
    .addEventListener('message', (e) => {
        try {
            const ev = JSON.parse(e.data);
            if (ev.type === 'history') {
                onHistoryEvent();
            } else if (ev.type === 'agent_confirm') {
                showConfirmCard(ev.confirm_id, ev.tool, ev.detail);
            } else if (ev.type === 'speak' && ev.text && ev.text.trim()) {
                appendMsg('pet', cleanReply(ev.text));
            } else if (ev.type === 'action' && ev.text) {
                appendMsg('pet', cleanReply(ev.text));
            } else if (ev.type === 'reminder') {
                appendMsg('pet', `⏰ ${ev.text}`);
            } else if (ev.type === 'settings') {
                fetch(`${API_BASE}/settings`).then(r => r.json()).then(s => {
                    MUTED = !!s.mute;
                    syncMuteBtn();
                    syncMuteSwitch();
                }).catch(() => {});
            } else if (ev.type === 'agent_action') {
                if (!chatting) appendMsg('pet', `🔧 ${AGENT_TOOL_CN[ev.tool] || '忙起来'}…`);
            } else if (ev.type === 'agent_thought' && !chatting) {
                const t = String(ev.text || '').slice(0, 40);
                if (t) appendMsg('pet', `💭 ${t}`);
            } else if (ev.type === 'pet_avatar') {
                loadPetAnimations();  // 桌宠形象切换/生成 → 刷新侧栏动图
            }
        } catch (err) {}
    });

// ==================== 启动 ====================
(async function boot() {
    showView('chat');   // 进入对话视图：清红点（unreadMsgs=0）
    try {
        await fetchSessions();
        const active = sessions.find((s) => s.active) || sessions[0];
        currentSessionId = active ? active.id : null;
        renderSessionBar();
        await rebuildHistory();
    } catch (e) {
        clearMessages();
        sysMsg('先启动后端再试试（python main.py）');
    }
    await restoreRagDoc();
    focusInput();
})();
