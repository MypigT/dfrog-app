// Electron 主进程
const { app, BrowserWindow, ipcMain, screen } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

let petWindow;
const panelWindows = {};  // type -> BrowserWindow

// ==================== 窗口状态持久化（位置/大小/置顶） ====================
// 桌宠窗口位置、对话窗口大小+位置+置顶状态，重启/重进都不丢。
const STATE_FILE = path.join(__dirname, '..', 'data', 'window_state.json');

function loadWindowState() {
    try {
        return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
    } catch (e) { return {}; }
}

let stateSaveTimer = null;
function saveWindowState() {
    // 防抖：拖动/缩放过程中高频触发，停 400ms 再落盘
    if (stateSaveTimer) clearTimeout(stateSaveTimer);
    stateSaveTimer = setTimeout(() => {
        stateSaveTimer = null;
        const st = loadWindowState();
        try {
            if (petWindow) {
                const [x, y] = petWindow.getPosition();
                st.pet = { x, y };
            }
            if (chatWindow) {
                const [x, y] = chatWindow.getPosition();
                const [w, h] = chatWindow.getSize();
                st.chat = { x, y, w, h, top: chatWindow.isAlwaysOnTop() };
            }
            fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
            fs.writeFileSync(STATE_FILE, JSON.stringify(st));
        } catch (e) { /* 落盘失败无所谓 */ }
    }, 400);
}

// 后端没开时自动启动（打包版启动 resources/backend 里的 exe，开发模式启动 python）
async function ensureBackend() {
    try {
        const res = await fetch('http://localhost:8000/health', { signal: AbortSignal.timeout(2000) });
        if (res.ok) return;
    } catch (e) {
        // 后端没开，尝试拉起
    }
    const exe = path.join(process.resourcesPath, 'backend', 'dfrog-backend.exe');
    if (fs.existsSync(exe)) {
        spawn(exe, { detached: true, stdio: 'ignore' }).unref();
        console.log('[Dfrog] 已启动打包后端 dfrog-backend.exe');
        return;
    }
    const py = path.join(__dirname, '..', 'backend', 'main.py');
    if (fs.existsSync(py)) {
        spawn('python', [py], { detached: true, stdio: 'ignore' }).unref();
        console.log('[Dfrog] 已启动开发后端 python main.py');
    }
}

function createPetWindow() {
    const { width, height } = screen.getPrimaryDisplay().workAreaSize;
    petWindow = new BrowserWindow({
        width: 220,
        height: 340,
        transparent: true,
        frame: false,
        alwaysOnTop: true,
        skipTaskbar: true,
        resizable: false,
        hasShadow: false,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
        },
    });

    petWindow.loadFile(path.join(__dirname, 'renderer', 'pet.html'));
    petWindow.setAlwaysOnTop(true, 'screen-saver');
    // 初始位置：上次拖到的位置；没有存档则默认右下角
    const savedPet = loadWindowState().pet;
    petWindow.setPosition(
        savedPet ? savedPet.x : width - 240,
        savedPet ? savedPet.y : height - 360
    );
    petWindow.on('move', saveWindowState);

    // 透明区域鼠标穿透；奶龙本体/输入框区域可交互
    petWindow.setIgnoreMouseEvents(true, { forward: true });
}

// ==================== 面板独立窗口管理 ====================
const PANEL_W = 340;
const PANEL_H = 420;

function openPanelWindow(type) {
    // 已打开则聚焦
    if (panelWindows[type]) {
        panelWindows[type].show();
        panelWindows[type].focus();
        return;
    }

    const petPos = petWindow ? petWindow.getPosition() : { x: 100, y: 100 };
    const [px, py] = petPos;
    const sw = screen.getPrimaryDisplay().workAreaSize.width;
    const sh = screen.getPrimaryDisplay().workAreaSize.height;

    // 默认出现在奶龙左侧；不够空间则放右侧
    let nx = px - PANEL_W - 20;
    let ny = py;
    if (nx < 0) nx = px + 220 + 20;
    nx = Math.max(0, Math.min(nx, sw - PANEL_W));
    ny = Math.max(0, Math.min(ny, sh - PANEL_H));

    const win = new BrowserWindow({
        width: PANEL_W,
        height: PANEL_H,
        titleBarStyle: 'hidden',
        resizable: true,
        hasShadow: true,
        alwaysOnTop: true,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
        },
    });

    win.loadFile(path.join(__dirname, 'renderer', 'panel.html'), { hash: type });
    win.setAlwaysOnTop(true, 'screen-saver');  // 和奶龙同级置顶，不被任何窗口盖住
    win.setPosition(Math.round(nx), Math.round(ny));

    panelWindows[type] = win;

    win.on('closed', () => {
        delete panelWindows[type];
    });
}

function closePanelWindow(type) {
    if (panelWindows[type]) {
        panelWindows[type].close();
    }
}

// ==================== 大对话框窗口 ====================
let chatWindow = null;

function openChatWindow() {
    // 已打开则聚焦
    if (chatWindow) {
        chatWindow.show();
        chatWindow.focus();
        return;
    }

    // 对话模式 = 桌宠换上大个子：桌宠窗口隐身（渲染进程照常跑，历史照写）
    if (petWindow) {
        petWindow.hide();
        petWindow.webContents.send('pet-chat-mode', true);  // 告诉桌宠：对话模式开着，确认卡由对话窗弹，你别再叠
    }

    const { width, height } = screen.getPrimaryDisplay().workAreaSize;
    const CW = 560;
    const CH = 700;

    chatWindow = new BrowserWindow({
        width: CW,
        height: CH,
        titleBarStyle: 'hidden',
        resizable: true,
        hasShadow: true,
        // 不做置顶：普通窗口层级，点一下标题栏就能赢过别的窗
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
        },
    });

    chatWindow.loadFile(path.join(__dirname, 'renderer', 'chat.html'));

    // 恢复上次的大小/位置（clamp 到可视区）；没有存档则首次居中在桌宠原区域
    const savedChat = loadWindowState().chat;
    if (savedChat && savedChat.w >= 320 && savedChat.h >= 300) {
        const sw = screen.getPrimaryDisplay().workAreaSize.width;
        const sh = screen.getPrimaryDisplay().workAreaSize.height;
        chatWindow.setBounds({
            x: Math.max(0, Math.min(savedChat.x, sw - savedChat.w)),
            y: Math.max(0, Math.min(savedChat.y, sh - savedChat.h)),
            width: savedChat.w,
            height: savedChat.h,
        });
    } else {
        // 桌宠窗口 220×340，取中心点 (petPos+110/170)；petWindow 被 hide 后 getPosition 仍可用
        const petPos = petWindow ? petWindow.getPosition() : [width - 240, height - 360];
        const pcx = petPos[0] + 110;
        const pcy = petPos[1] + 170;
        chatWindow.setPosition(
            Math.round(Math.max(0, Math.min(pcx - CW / 2, width - CW))),
            Math.round(Math.max(0, Math.min(pcy - CH / 2, height - CH)))
        );
    }

    // 恢复上次的置顶状态
    if (savedChat && savedChat.top) {
        chatWindow.setAlwaysOnTop(true, 'screen-saver');
    }
    // 通知渲染进程恢复后的置顶状态（📌 按钮同步外观）
    chatWindow.webContents.on('did-finish-load', () => {
        chatWindow.webContents.send('chat-top-state', chatWindow.isAlwaysOnTop());
    });
    // 位置/大小变化 → 落盘（下次重进记住）
    chatWindow.on('resize', saveWindowState);
    chatWindow.on('move', saveWindowState);

    chatWindow.on('closed', () => {
        chatWindow = null;
        saveWindowState();
        // 退出对话模式 = 桌宠回来，并从服务端历史重建气泡显示（数据一份、显示同步）
        if (petWindow) {
            petWindow.show();
            petWindow.webContents.send('pet-refresh');          // 清残留气泡/确认卡 + 重建桌面气泡
            petWindow.webContents.send('pet-chat-mode', false); // 回到桌宠模式：确认卡恢复由桌宠弹
            petWindow.webContents.send('pet-chat-running', false);  // 聊天窗关了=聊天流结束，恢复桌宠进度气泡的显示/入库
        }
    });
}

function closeChatWindow() {
    if (chatWindow) {
        chatWindow.close();
    }
}

app.whenReady().then(async () => {
    await ensureBackend();
    createPetWindow();

    // 拖动奶龙 → 移动窗口（增量模式）
    ipcMain.on('move-pet', (e, { dx, dy }) => {
        if (petWindow) {
            const [x, y] = petWindow.getPosition();
            petWindow.setPosition(Math.round(x + dx), Math.round(y + dy));
        }
    });

    // 切换鼠标穿透
    ipcMain.on('set-click-through', (e, ignore) => {
        if (!petWindow) return;
        if (ignore) {
            petWindow.setIgnoreMouseEvents(true, { forward: true });
        } else {
            petWindow.setIgnoreMouseEvents(false);
        }
    });

    // 对话模式大窗口告知「聊天流在跑」（转发给桌宠渲染进程：
    // 期间 🔧/💭 进度气泡既不显示也不入库，避免和聊天流重复记录）
    ipcMain.on('pet-chat-running', (e, running) => {
        if (petWindow) petWindow.webContents.send('pet-chat-running', !!running);
    });

    // 关闭桌宠
    ipcMain.on('close-pet', () => {
        app.quit();
    });

    // 打开面板独立窗口
    ipcMain.on('open-panel', (e, type) => {
        openPanelWindow(type);
    });

    // 关闭面板独立窗口
    ipcMain.on('close-panel', (e, type) => {
        closePanelWindow(type);
    });

    // 打开大对话框窗口
    ipcMain.on('open-chat-window', () => {
        openChatWindow();
    });

    // 关闭大对话框窗口
    ipcMain.on('close-chat-window', () => {
        closeChatWindow();
    });

    // 对话窗顶栏 📌 置顶开关
    ipcMain.on('chat-set-top', (e, on) => {
        if (chatWindow) {
            chatWindow.setAlwaysOnTop(!!on, 'screen-saver');
            saveWindowState();  // 置顶状态也记住，重进保留
        }
    });

    // 桌宠置顶开关（设置面板控制）
    ipcMain.on('pet-set-top', (e, on) => {
        if (petWindow) {
            petWindow.setAlwaysOnTop(!!on, 'screen-saver');
        }
    });

});

// macOS 隐藏菜单栏
if (process.platform === 'darwin') {
    app.dock.hide();
}
