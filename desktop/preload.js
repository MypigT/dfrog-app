// 预加载脚本，安全地暴露 IPC 给渲染进程
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
    movePet: (dx, dy) => ipcRenderer.send('move-pet', { dx, dy }),
    setClickThrough: (ignore) => ipcRenderer.send('set-click-through', ignore),
    closePet: () => ipcRenderer.send('close-pet'),
    openPanel: (type) => ipcRenderer.send('open-panel', type),
    closePanel: (type) => ipcRenderer.send('close-panel', type),
    openChatWindow: () => ipcRenderer.send('open-chat-window'),
    closeChatWindow: () => ipcRenderer.send('close-chat-window'),
    setPetChatRunning: (running) => ipcRenderer.send('pet-chat-running', running),
    onPetChatRunning: (cb) => ipcRenderer.on('pet-chat-running', cb),
    onPetRefresh: (cb) => ipcRenderer.on('pet-refresh', cb),
    onPetChatMode: (cb) => ipcRenderer.on('pet-chat-mode', cb),
    // 对话窗顶栏 📌 置顶开关
    setChatTop: (on) => ipcRenderer.send('chat-set-top', on),
    setPetTop: (on) => ipcRenderer.send('pet-set-top', on),
    onChatTopState: (cb) => ipcRenderer.on('chat-top-state', cb),
});
