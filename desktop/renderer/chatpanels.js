// ==================== 对话模式功能视图：智能体控制台 / 提醒 / 速记 ====================
// 对话窗口内的原生实现（chat.html 里的 section），不复用桌宠的 panel.html/panel.js——
// 不搞窗中窗。数据与桌宠模式一致：提醒/速记/agent 都打同一套后端 API。
// 视图切换由 chat.js 派发 dfrog-view-shown 事件，这里收到后刷新对应列表。
// 注意：本文件包在 IIFE 里——chat.js 顶层已声明 API_BASE/api/AGENT_TOOL_CN，同页重复声明会直接报错。

(function () {
'use strict';

// ==================== 模型配置（每个功能页面右上角：模型名+Key，可改）====================
// Key 两种模式：环境变量名（运行时读环境变量）/ 直接填Key。保存后后端即时生效。
let modelConfigCache = {};

async function loadModelConfig() {
    try {
        const res = await fetch(`${API_BASE}/model/config`);
        const data = await res.json();
        modelConfigCache = data.items || {};
    } catch (e) {}
}

function renderModelConfig(feature) {
    const host = document.querySelector(`.pv-config[data-feature="${feature}"]`);
    if (!host) return;
    const cfg = modelConfigCache[feature] || {model:'', key_mode:'env', key_value:''};
    host.innerHTML = '';
    const { modelIn, modeSel, keyIn, saveBtn } = modelConfigFields(feature, cfg);
    host.append(modelIn, modeSel, keyIn, saveBtn);
}

async function refreshModelConfig() {
    await loadModelConfig();
    ['agent', 'reminder', 'notes', 'petimg'].forEach(renderModelConfig);
}

// ==================== 视图切换通知（chat.js 派发）→ 刷新对应列表 ====================
window.addEventListener('dfrog-view-shown', (e) => {
    const v = e.detail && e.detail.view;
    if (v === 'reminder') refreshReminders();
    else if (v === 'notes') refreshNotes();
    else if (v === 'petimg') refreshPetImages();
    if (v === 'agent' || v === 'reminder' || v === 'notes' || v === 'petimg') refreshModelConfig();
});

// ==================== 工具台：填参数点执行 ====================
function fmtDelay(sec) {
    if (sec < 60) return `${sec}秒后`;
    return `${Math.round(sec / 60)}分钟后`;
}

document.querySelectorAll('.tool-card').forEach(card => {
    const btn = card.querySelector('.tc-btn');
    const resultEl = card.querySelector('.tc-result');
    if (!btn || !resultEl) return;
    btn.addEventListener('click', async () => {
        const tool = card.dataset.tool;
        const args = {};
        card.querySelectorAll('[data-field]').forEach(inp => {
            args[inp.dataset.field] = inp.value;
        });
        btn.disabled = true;
        resultEl.textContent = '执行中…';
        try {
            let text;
            if (tool === 'add_reminder') {
                const delay = parseInt(args.delay_seconds, 10) || 300;
                const res = await fetch(`${API_BASE}/reminders`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({text: args.text, delay_seconds: delay})
                });
                text = res.ok ? `✅ 已设提醒：${args.text}（${fmtDelay(delay)}）` : '设提醒失败，请检查后端';
            } else if (tool === 'add_note') {
                const res = await fetch(`${API_BASE}/notes`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({text: args.text})
                });
                text = res.ok ? `✅ 已记下：${args.text}` : '记速记失败，请检查后端';
            } else if (tool === 'look_at_screen') {
                const res = await fetch(`${API_BASE}/vision`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({})
                });
                if (!res.ok || !res.body) {
                    text = '👀 嗯…眼睛没睁开。';
                } else {
                    const reader = res.body.getReader();
                    const decoder = new TextDecoder();
                    let s = '';
                    while (true) {
                        const {done, value} = await reader.read();
                        if (done) break;
                        s += decoder.decode(value, {stream: true});
                        resultEl.textContent = '👀 ' + s;
                    }
                    text = '👀 ' + s.trim();
                }
            } else {
                // 读类 / 查时间工具：直接调 /agent/tool，不经大模型、不耗 token
                const res = await fetch(`${API_BASE}/agent/tool`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({tool, args})
                });
                const data = await res.json().catch(() => ({}));
                text = data.result || data.error || '(无结果)';
            }
            resultEl.textContent = text;
        } catch (e) {
            resultEl.textContent = '❌ 出错：' + (e.message || e);
        } finally {
            btn.disabled = false;
        }
    });
});

// ==================== 提醒 ====================
async function refreshReminders() {
    try {
        const res = await fetch(`${API_BASE}/reminders`);
        const items = await res.json();
        const list = document.getElementById('reminder-list');
        list.innerHTML = '';
        if (!items.length) {
            list.innerHTML = '<div class="pv-empty">还没有提醒</div>';
            return;
        }
        items.forEach(it => {
            const div = document.createElement('div');
            div.className = 'pv-item';
            const body = document.createElement('div');
            body.className = 'pi-body';
            const text = document.createElement('div');
            text.textContent = it.text;
            const meta = document.createElement('span');
            meta.className = 'pi-meta';
            meta.textContent = '⏰ ' + it.due_at;
            body.appendChild(text);
            body.appendChild(meta);
            const del = document.createElement('button');
            del.className = 'pi-del';
            del.textContent = '✕';
            del.title = '删除';
            del.addEventListener('click', async () => {
                await fetch(`${API_BASE}/reminders/${it.id}`, {method: 'DELETE'});
                refreshReminders();
            });
            div.appendChild(body);
            div.appendChild(del);
            list.appendChild(div);
        });
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

document.getElementById('reminder-text').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') document.getElementById('reminder-add').click();
});

// ==================== 速记 ====================
async function refreshNotes() {
    try {
        const res = await fetch(`${API_BASE}/notes`);
        const items = await res.json();
        const list = document.getElementById('notes-list');
        list.innerHTML = '';
        if (!items.length) {
            list.innerHTML = '<div class="pv-empty">还没有速记</div>';
            return;
        }
        items.forEach(it => {
            const div = document.createElement('div');
            div.className = 'pv-item';
            const body = document.createElement('div');
            body.className = 'pi-body';
            const text = document.createElement('div');
            text.textContent = it.text;
            const meta = document.createElement('span');
            meta.className = 'pi-meta';
            meta.textContent = it.created_at;
            body.appendChild(text);
            body.appendChild(meta);
            const del = document.createElement('button');
            del.className = 'pi-del';
            del.textContent = '✕';
            del.title = '删除';
            del.addEventListener('click', async () => {
                await fetch(`${API_BASE}/notes/${it.id}`, {method: 'DELETE'});
                refreshNotes();
            });
            div.appendChild(body);
            div.appendChild(del);
            list.appendChild(div);
        });
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

document.getElementById('notes-text').addEventListener('keydown', (e) => {
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
        list.insertBefore(div, list.firstChild);   // 整理结果置顶，原始条目留在下面
        list.scrollTop = 0;
        if (res.body) {
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            while (true) {
                const {done, value} = await reader.read();
                if (done) break;
                div.textContent = '✨ ' + decoder.decode(value, {stream: true});
                list.scrollTop = 0;
            }
        }
    } catch (e) {}
    finally {
        btn.disabled = false;
        btn.textContent = '✨ AI整理';
    }
});

// ==================== 桌宠形象（形象 = 素材文件夹，里面文件 = 动作，全部持久化）====================
let petImgGen = false;          // AI 生成动图中
let currentAvatarId = null;     // 当前打开的形象详情

async function refreshPetImages() {
    if (currentAvatarId) refreshAvatarDetail();
    else refreshAvatarList();
}

async function refreshAvatarList() {
    try {
        const res = await fetch(`${API_BASE}/pet/avatars`);
        const data = await res.json();
        const items = data.items || [];
        const list = document.getElementById('petimg-list');
        list.innerHTML = '';
        document.getElementById('petimg-detail').hidden = true;
        document.getElementById('petimg-list').hidden = false;
        if (!items.length) {
            list.innerHTML = '<div class="pv-empty">还没有形象，上传一个素材文件夹或单张图开始</div>';
        } else {
            items.forEach(it => {
                const div = document.createElement('div');
                div.className = 'pv-item avatar-card';
                const body = document.createElement('div');
                body.className = 'pi-body';
                const name = document.createElement('div');
                name.textContent = it.name + (it.active ? '  🟢当前' : '');
                const meta = document.createElement('span');
                meta.className = 'pi-meta';
                meta.textContent = `${it.anim_count} 个动作`;
                body.appendChild(name);
                body.appendChild(meta);
                div.appendChild(body);
                // 按钮组：设为当前 / 删除（点击不触发卡片进入详情）
                const btns = document.createElement('div');
                btns.className = 'pi-btns';
                if (!it.active) {
                    const act = document.createElement('button');
                    act.className = 'pi-btn';
                    act.textContent = '设为当前';
                    act.addEventListener('click', (e) => { e.stopPropagation(); activateAvatar(it.id); });
                    btns.appendChild(act);
                }
                const del = document.createElement('button');
                del.className = 'pi-del';
                del.textContent = '✕';
                del.title = '删除形象';
                del.addEventListener('click', (e) => { e.stopPropagation(); deleteAvatar(it.id); });
                btns.appendChild(del);
                div.appendChild(btns);
                div.addEventListener('click', () => openAvatar(it.id));
                list.appendChild(div);
            });
        }
        setPetImgGenBtn(items.some(it => it.anim_count > 0));
        await loadPetPreview();
    } catch (e) {}
}

function openAvatar(aid) {
    currentAvatarId = aid;
    refreshAvatarDetail();
}

function closeAvatar() {
    currentAvatarId = null;
    refreshAvatarList();
}

async function refreshAvatarDetail() {
    if (!currentAvatarId) return;
    try {
        const res = await fetch(`${API_BASE}/pet/avatar/${currentAvatarId}`);
        if (!res.ok) { closeAvatar(); return; }
        const av = await res.json();
        document.getElementById('petimg-list').hidden = true;
        document.getElementById('petimg-detail').hidden = false;
        document.getElementById('petimg-rename-input').value = av.name;
        document.getElementById('petimg-role-input').value = av.role_name || '';
        document.getElementById('petimg-prompt-input').value = av.prompt || '';
        const list = document.getElementById('petimg-actions');
        list.innerHTML = '';
        if (!av.manifest.length) {
            list.innerHTML = '<div class="pv-empty">还没有动作，点「追加动作」上传 gif/图</div>';
        } else {
            av.manifest.forEach((m, i) => list.appendChild(buildActionCard(av, m, i)));
        }
        setPetImgGenBtn(av.manifest.length > 0);
        // 激活按钮状态
        const actBtn = document.getElementById('petimg-activate');
        if (actBtn) {
            actBtn.disabled = !!av.active;
            actBtn.textContent = av.active ? '当前中' : '设为当前';
        }
        await loadPetPreview();
    } catch (e) {}
}

function buildActionCard(av, m, i) {
    const div = document.createElement('div');
    div.className = 'pv-item action-card';
    const thumb = document.createElement('img');
    thumb.className = 'av-thumb';
    thumb.src = `${API_BASE}${m.url}`;
    thumb.alt = m.name;
    thumb.title = m.name + '（点击预览）';
    thumb.addEventListener('click', () => previewAction(m));
    div.appendChild(thumb);

    const info = document.createElement('div');
    info.className = 'ac-info';
    const slot = document.createElement('span');
    slot.className = 'ac-slot' + (i < 4 ? ' slot-main' : '');
    slot.textContent = m.slot;
    const name = document.createElement('span');
    name.className = 'ac-name';
    name.textContent = m.name;
    info.appendChild(slot);
    info.appendChild(name);
    div.appendChild(info);

    const btns = document.createElement('div');
    btns.className = 'pi-btns';
    const up = document.createElement('button');
    up.className = 'pi-btn';
    up.textContent = '↑';
    up.title = '上移（提前槽位）';
    up.disabled = i === 0;
    up.addEventListener('click', () => moveAction(m.file, 'up'));
    btns.appendChild(up);
    const down = document.createElement('button');
    down.className = 'pi-btn';
    down.textContent = '↓';
    down.title = '下移';
    down.disabled = i === av.manifest.length - 1;
    down.addEventListener('click', () => moveAction(m.file, 'down'));
    btns.appendChild(down);
    const del = document.createElement('button');
    del.className = 'pi-del';
    del.textContent = '✕';
    del.title = '删除动作';
    del.addEventListener('click', () => deleteAction(m.file));
    btns.appendChild(del);
    div.appendChild(btns);
    return div;
}

function previewAction(m) {
    const img = document.getElementById('petimg-preview');
    const name = document.getElementById('petimg-preview-name');
    img.src = `${API_BASE}${m.url}`;
    img.style.display = 'block';
    name.textContent = m.name + '（' + m.slot + '）';
}

async function moveAction(file, direction) {
    await fetch(`${API_BASE}/pet/avatar/action/move`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({avatar_id: currentAvatarId, file, direction})
    });
    refreshAvatarDetail();
}

async function deleteAction(file) {
    await fetch(`${API_BASE}/pet/avatar/action/delete`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({avatar_id: currentAvatarId, file})
    });
    refreshAvatarDetail();
}

async function activateAvatar(aid) {
    await fetch(`${API_BASE}/pet/avatar/activate`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({avatar_id: aid})
    });
    await loadPetPreview();
    refreshAvatarList();
}

async function deleteAvatar(aid) {
    if (!confirm('确定删除这个形象？里面的所有动作都会删除。')) return;
    await fetch(`${API_BASE}/pet/avatar/delete`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({avatar_id: aid})
    });
    if (currentAvatarId === aid) closeAvatar();
    else refreshAvatarList();
}

async function generateForAvatar() {
    if (!currentAvatarId) return;
    petImgGen = true;
    setPetImgHint('AI 生成动作动图中（调用大模型，约 10-20 秒，会覆盖手动动作）…');
    setPetImgGenBtn(true);
    try {
        const res = await fetch(`${API_BASE}/pet/avatar/generate`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({avatar_id: currentAvatarId})
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            setPetImgHint('生成失败: ' + (err.error || ''));
            return;
        }
        const data = await res.json();
        setPetImgHint(`生成完成，${data.anim_count} 个动作动图`);
        refreshAvatarDetail();
    } catch (err) {
        setPetImgHint('生成失败: ' + err.message);
    } finally {
        petImgGen = false;
        setPetImgGenBtn(false);
        refreshAvatarDetail();
    }
}

async function loadPetPreview() {
    const img = document.getElementById('petimg-preview');
    const name = document.getElementById('petimg-preview-name');
    if (!img) return;
    try {
        const res = await fetch(`${API_BASE}/pet/avatar/current`, { cache: 'no-store' });
        const cd = await res.json();
        if (!cd || !cd.avatar_id) {
            img.style.display = 'none';
            name.textContent = '';
            return;
        }
        const idle = (cd.manifest && cd.manifest[0]) ? cd.manifest[0] : null;
        if (idle) {
            img.src = `${API_BASE}${idle.url}`;
            img.style.display = 'block';
            name.textContent = cd.name + '（' + idle.slot + '预览）';
        } else {
            img.style.display = 'none';
            name.textContent = cd.name + '（暂无动作）';
        }
    } catch (e) {}
}

function setPetImgHint(text) {
    document.getElementById('petimg-hint').textContent = text;
}

function setPetImgGenBtn(on) {
    const btn = document.getElementById('petimg-gen');
    if (btn) {
        btn.disabled = !on || petImgGen;
        btn.textContent = petImgGen ? '✨ 生成中…' : '✨ AI生成动作';
    }
}

// 上传文件夹 → 一个形象，里面文件各成一个动作（原样保留 gif 动画）
document.getElementById('petimg-folder-upload').addEventListener('click', () => {
    document.getElementById('petimg-folder-input').click();
});

document.getElementById('petimg-folder-input').addEventListener('change', async (e) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    const fd = new FormData();
    files.forEach(f => fd.append('files', f));
    setPetImgHint(`上传 ${files.length} 个文件…`);
    try {
        const res = await fetch(`${API_BASE}/pet/folder/upload`, {method: 'POST', body: fd});
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            setPetImgHint('上传失败: ' + (err.error || ''));
            return;
        }
        const data = await res.json();
        setPetImgHint(`上传成功：形象「${data.name}」含 ${data.anim_count} 个动作，按文件名排序对应槽位`);
        refreshPetImages();
    } catch (err) {
        setPetImgHint('上传失败: ' + err.message);
    }
    e.target.value = '';
});

// 单图上传 → 一个形象 + 1 个动作
document.getElementById('petimg-upload').addEventListener('click', () => {
    document.getElementById('petimg-file').click();
});

document.getElementById('petimg-file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    setPetImgHint('上传中…');
    try {
        const res = await fetch(`${API_BASE}/pet/avatar/upload`, {method: 'POST', body: fd});
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            setPetImgHint('上传失败: ' + (err.error || ''));
            return;
        }
        setPetImgHint('上传成功，已建形象 + 1 个动作');
        refreshPetImages();
    } catch (err) {
        setPetImgHint('上传失败: ' + err.message);
    }
    e.target.value = '';
});

// 新建空形象
document.getElementById('petimg-new').addEventListener('click', async () => {
    const name = prompt('形象名字：', '新形象');
    if (name === null) return;
    try {
        const res = await fetch(`${API_BASE}/pet/avatar/create`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: name})
        });
        const data = await res.json();
        setPetImgHint(`已新建形象「${data.name}」，点进去追加动作`);
        refreshPetImages();
    } catch (err) {
        setPetImgHint('新建失败: ' + err.message);
    }
});

// AI 生成动作（针对当前打开的形象，否则当前激活形象）
document.getElementById('petimg-gen').addEventListener('click', async () => {
    let aid = currentAvatarId;
    if (!aid) {
        try {
            const res = await fetch(`${API_BASE}/pet/avatar/current`);
            const cd = await res.json();
            aid = cd.avatar_id;
        } catch (e) {}
    }
    if (!aid) { setPetImgHint('请先打开或激活一个形象'); return; }
    if (currentAvatarId !== aid) { currentAvatarId = aid; }
    generateForAvatar();
});

// 追加动作（给当前打开的形象）
document.getElementById('petimg-action-add').addEventListener('click', () => {
    if (!currentAvatarId) { setPetImgHint('请先点开一个形象'); return; }
    document.getElementById('petimg-action-input').click();
});

document.getElementById('petimg-action-input').addEventListener('change', async (e) => {
    const files = Array.from(e.target.files || []);
    if (!files.length || !currentAvatarId) return;
    for (const file of files) {
        const fd = new FormData();
        fd.append('file', file);
        fd.append('avatar_id', currentAvatarId);
        const res = await fetch(`${API_BASE}/pet/avatar/action/add`, {method: 'POST', body: fd});
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            setPetImgHint('追加失败: ' + (err.error || ''));
            return;
        }
    }
    setPetImgHint('已追加动作');
    refreshAvatarDetail();
    e.target.value = '';
});

// 重命名形象
document.getElementById('petimg-rename-btn').addEventListener('click', async () => {
    if (!currentAvatarId) return;
    const name = document.getElementById('petimg-rename-input').value.trim();
    if (!name) return;
    await fetch(`${API_BASE}/pet/avatar/rename`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({avatar_id: currentAvatarId, name: name})
    });
    refreshAvatarDetail();
});

// 保存角色名 + 提示词
document.getElementById('petimg-meta-save').addEventListener('click', async () => {
    if (!currentAvatarId) return;
    const role_name = document.getElementById('petimg-role-input').value;
    const prompt = document.getElementById('petimg-prompt-input').value;
    try {
        const res = await fetch(`${API_BASE}/pet/avatar/meta`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({avatar_id: currentAvatarId, role_name, prompt})
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            setPetImgHint('保存失败: ' + (err.error || ''));
            return;
        }
        setPetImgHint('人设已保存，设为当前后对话按此人设生效');
    } catch (err) {
        setPetImgHint('保存失败: ' + err.message);
    }
});

// 设为当前形象
document.getElementById('petimg-activate').addEventListener('click', async () => {
    if (!currentAvatarId) return;
    await activateAvatar(currentAvatarId);
    refreshAvatarDetail();
});

// 删除形象
document.getElementById('petimg-del').addEventListener('click', async () => {
    if (!currentAvatarId) return;
    await deleteAvatar(currentAvatarId);
});

// 返回形象列表
document.getElementById('petimg-back').addEventListener('click', closeAvatar);

// ==================== SSE：提醒到点 → 刷新提醒列表 ====================
const reminderES = new EventSource(`${API_BASE}/events`);
reminderES.onmessage = (e) => {
    try {
        const ev = JSON.parse(e.data);
        if (ev.type === 'reminder') refreshReminders();
        else if (ev.type === 'pet_avatar') refreshPetImages();  // 形象切换/生成 → 刷新列表
    } catch (err) {}
};


// 对外暴露（调试/CDP 测试用）
window.dfrogPanels = {
    refreshReminders,
    refreshNotes,
    refreshPetImages,
};
})();
