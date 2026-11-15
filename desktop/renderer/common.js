// 渲染进程公共常量与工具 —— 桌宠窗口(pet.html) / 对话窗口(chat.html) / 面板窗口(panel.html) 共用。
// 在各自页面脚本之前加载，避免重复定义。

const API_BASE = 'http://localhost:8000';

// Agent 工具调用名 → 中文（桌面气泡 / 对话流 / 工具台共用一套）
const AGENT_TOOL_CN = {
    Bash: '跑命令', Read: '读文件', Write: '写文件', Edit: '改文件',
    Glob: '找文件', Grep: '搜内容', WebFetch: '开网页', WebSearch: '搜索',
    Task: '派活', Skill: '调技能', TodoWrite: '规划', NotebookEdit: '改笔记',
    list_files: '看目录', read_file: '读文件', write_file: '写文件',
    move_file: '移动', rename_file: '改名', search_files: '搜文件',
    delete_file: '删除', add_reminder: '设提醒', add_note: '记速记',
    get_current_time: '看时间',
    bash: '跑命令', content_search: '搜内容', edit_file: '改文件',
};

// 模型配置字段（模型名输入 + Key 模式选择 + Key 输入 + 保存按钮），保存按钮已接好 /model/config
function modelConfigFields(feature, cfg) {
    cfg = cfg || {model: '', key_mode: 'env', key_value: ''};
    const modelIn = document.createElement('input');
    modelIn.className = 'cfg-model';
    modelIn.value = cfg.model || '';
    modelIn.placeholder = '模型名';
    const modeSel = document.createElement('select');
    modeSel.className = 'cfg-mode';
    modeSel.innerHTML = '<option value="env">环境变量名</option><option value="direct">直接填Key</option>';
    modeSel.value = cfg.key_mode || 'env';
    const keyIn = document.createElement('input');
    keyIn.className = 'cfg-key';
    keyIn.value = cfg.key_value || '';
    keyIn.placeholder = modeSel.value === 'direct' ? 'Key 值' : '环境变量名';
    modeSel.addEventListener('change', () => {
        keyIn.placeholder = modeSel.value === 'direct' ? 'Key 值' : '环境变量名';
    });
    const saveBtn = document.createElement('button');
    saveBtn.className = 'cfg-save';
    saveBtn.textContent = '保存';
    saveBtn.addEventListener('click', async () => {
        saveBtn.disabled = true;
        saveBtn.textContent = '保存中…';
        try {
            await fetch(`${API_BASE}/model/config`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({feature, model: modelIn.value, key_mode: modeSel.value, key_value: keyIn.value})
            });
            saveBtn.textContent = '✓';
            setTimeout(() => { saveBtn.textContent = '保存'; saveBtn.disabled = false; }, 1200);
        } catch (e) {
            saveBtn.textContent = '保存';
            saveBtn.disabled = false;
        }
    });
    return { modelIn, modeSel, keyIn, saveBtn };
}

// 模型配置列表：拉取配置，为每个功能渲染一张「模型名 + Key」卡片到容器
async function renderModelConfigList(container) {
    if (!container) return;
    try {
        const res = await fetch(`${API_BASE}/model/config`);
        const data = await res.json();
        const items = data.items || {};
        const FEATURES = [
            ['chat', '对话模型'],
            ['agent', '智能体模型'],
            ['vision', '视觉模型'],
            ['petimg', '桌宠形象模型'],
            ['notes', '速记整理模型'],
        ];
        container.innerHTML = '';
        FEATURES.forEach(([key, label]) => {
            const cfg = items[key] || {model: '', key_mode: 'env', key_value: ''};
            const card = document.createElement('div');
            card.className = 'model-card';
            const title = document.createElement('div');
            title.className = 'mc-title';
            title.textContent = label;
            card.appendChild(title);
            const { modelIn, modeSel, keyIn, saveBtn } = modelConfigFields(key, cfg);
            const modelRow = document.createElement('div');
            modelRow.className = 'mc-row';
            const modelLab = document.createElement('span');
            modelLab.className = 'mc-field-label';
            modelLab.textContent = '模型名';
            modelRow.append(modelLab, modelIn);
            card.appendChild(modelRow);
            const keyRow = document.createElement('div');
            keyRow.className = 'mc-row';
            const keyLab = document.createElement('span');
            keyLab.className = 'mc-field-label';
            keyLab.textContent = 'Key';
            keyRow.append(keyLab, modeSel, keyIn, saveBtn);
            card.appendChild(keyRow);
            container.appendChild(card);
        });
    } catch (e) {}
}
