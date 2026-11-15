// 奶龙面板窗口逻辑（独立窗口，通过 URL hash 决定显示哪个面板）

// 兼容非 Electron 环境
const api = window.electronAPI || {
    closePanel: () => {},
    setPetTop: () => {},
};

// 根据 URL hash 确定面板类型
const hash = window.location.hash.slice(1);
const views = {
    history: document.getElementById('history-view'),
    reminder: document.getElementById('reminder-view'),
    notes: document.getElementById('notes-view'),
    settings: document.getElementById('settings-view'),
};

const currentView = views[hash];
if (currentView) {
    currentView.classList.add('active');
}

// ==================== 设置面板 ====================
if (hash === 'settings') {
    const SETTING_KEYS = ['auto_start', 'mute', 'stream_output', 'proactive', 'always_on_top'];
    fetch(`${API_BASE}/settings`).then(r => r.json()).then(s => {
        SETTING_KEYS.forEach(k => {
            const el = document.getElementById('set-' + k);
            if (el) el.checked = !!s[k];
        });
    }).catch(() => {});
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
                if (k === 'always_on_top') { try { api.setPetTop(el.checked); } catch (e) {} }
            } catch (e) {}
        });
    });
    renderModelConfigList(document.getElementById('model-config-list'));
}

// ==================== 关闭按钮 ====================
// 独立面板窗口 → main 关窗口（panel.html 不再被对话模式嵌入，无需 postMessage 通知父窗）
document.querySelectorAll('.panel-close').forEach(btn => {
    btn.addEventListener('click', () => {
        api.closePanel(btn.dataset.type);
    });
});

// ==================== 工具函数 ====================
function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ==================== 历史面板 ====================
// 历史记录在奶龙窗口的 localStorage 里，由奶龙端每次「发送消息 / 收到回复」时
// ping 后端 /history/ping → 广播 history 事件 → 面板收到就刷新一次（事件驱动，
// 不是轮询：正好是用户要的「发送和接收各同步一次）。面板常驻订阅 SSE。
let historyES = null;

if (hash === 'history') {
    refreshHistory();
    historyES = new EventSource(`${API_BASE}/events`);
    historyES.onmessage = (e) => {
        try {
            const ev = JSON.parse(e.data);
            if (ev.type === 'history') refreshHistory();
        } catch (err) {}
    };
}

// 关闭窗口时断开 SSE
window.addEventListener('beforeunload', () => {
    if (historyES) historyES.close();
});

async function refreshHistory() {
    let history = [];
    try {
        // 服务端为准（桌宠模式/对话模式共用一份历史）；拉不开再退回奶龙窗口 localStorage
        const res = await fetch(`${API_BASE}/history/list`, { cache: 'no-store' });
        const data = await res.json();
        history = (data.items || []).map(it => {
            const role = (it && it.role === 'user') ? 'user' : 'pet';
            return { role, text: (it.text || '').slice(0, 500) };
        }).filter(it => it.text);
    } catch (e) {
        history = [];
    }
    try {
        const list = document.getElementById('history-list');
        if (!history.length) {
            list.innerHTML = '<div class="history-empty">还没有对话</div>';
            return;
        }
        list.innerHTML = '';
        history.forEach(item => {
            const div = document.createElement('div');
            div.className = 'history-item ' + (item.role === 'user' ? 'user' : 'pet');
            div.textContent = item.text;
            list.appendChild(div);
        });
        list.scrollTop = list.scrollHeight;
    } catch (e) {}
}

// ==================== 提醒面板 ====================
if (hash === 'reminder') {
    refreshReminders();
    document.getElementById('reminder-text').focus();
}

async function refreshReminders() {
    try {
        const res = await fetch(`${API_BASE}/reminders`);
        const items = await res.json();
        const list = document.getElementById('reminder-list');
        list.innerHTML = '';
        if (!items.length) {
            list.innerHTML = '<div class="reminder-empty">还没有提醒</div>';
            return;
        }
        items.forEach(it => {
            const div = document.createElement('div');
            div.className = 'reminder-item';
            div.innerHTML = `<span>${escapeHtml(it.text)}<br><span class="due">${it.due_at}</span></span>`;
            const del = document.createElement('button');
            del.className = 'del';
            del.textContent = '✕';
            del.title = '删除';
            del.addEventListener('click', async () => {
                await fetch(`${API_BASE}/reminders/${it.id}`, {method: 'DELETE'});
                refreshReminders();
            });
            div.appendChild(del);
            list.appendChild(div);
        });
        list.scrollTop = list.scrollHeight;
    } catch (e) {}
}

document.getElementById('reminder-add').addEventListener('click', async () => {
    const text = document.getElementById('reminder-text').value.trim();
    if (!text) return;
    document.getElementById('reminder-text').value = '';
    const delay = parseInt(document.getElementById('reminder-delay').value, 10) || 300;
    try {
        await fetch(`${API_BASE}/reminders`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text, delay_seconds: delay})
        });
        refreshReminders();
    } catch (e) {}
});

document.getElementById('reminder-text').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') document.getElementById('reminder-add').click();
});

// ==================== 速记面板 ====================
if (hash === 'notes') {
    refreshNotes();
    document.getElementById('notes-text').focus();
}

async function refreshNotes() {
    try {
        const res = await fetch(`${API_BASE}/notes`);
        const items = await res.json();
        const list = document.getElementById('notes-list');
        list.innerHTML = '';
        if (!items.length) {
            list.innerHTML = '<div class="notes-empty">还没有速记</div>';
            return;
        }
        items.forEach(it => {
            const div = document.createElement('div');
            div.className = 'note-item';
            div.innerHTML = `<span>${escapeHtml(it.text)}<br><span class="time">${it.created_at}</span></span>`;
            const del = document.createElement('button');
            del.className = 'del';
            del.textContent = '✕';
            del.title = '删除';
            del.addEventListener('click', async () => {
                await fetch(`${API_BASE}/notes/${it.id}`, {method: 'DELETE'});
                refreshNotes();
            });
            div.appendChild(del);
            list.appendChild(div);
        });
        list.scrollTop = list.scrollHeight;
    } catch (e) {}
}

document.getElementById('notes-add').addEventListener('click', async () => {
    const text = document.getElementById('notes-text').value.trim();
    if (!text) return;
    document.getElementById('notes-text').value = '';
    try {
        await fetch(`${API_BASE}/notes`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text})
        });
        refreshNotes();
    } catch (e) {}
});

document.getElementById('notes-text').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') document.getElementById('notes-add').click();
});

document.getElementById('notes-summarize').addEventListener('click', async () => {
    const btn = document.getElementById('notes-summarize');
    btn.disabled = true;
    btn.textContent = '✨ 整理中…';
    try {
        const res = await fetch(`${API_BASE}/notes/summarize`, {method: 'POST'});
        const list = document.getElementById('notes-list');
        const div = document.createElement('div');
        div.className = 'note-summary';
        div.textContent = '✨ ';
        list.appendChild(div);
        list.scrollTop = list.scrollHeight;
        if (res.body) {
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            while (true) {
                const {done, value} = await reader.read();
                if (done) break;
                div.textContent = '✨ ' + decoder.decode(value, {stream: true});
                list.scrollTop = list.scrollHeight;
            }
        }
    } catch (e) {}
    finally {
        btn.disabled = false;
        btn.textContent = '✨ AI整理';
    }
});
