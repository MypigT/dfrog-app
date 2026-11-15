// 渲染进程公共工具：markdown 符号剥离 + 模型签名尾巴清洗。
// 桌宠窗口(pet.html)与大对话框窗口(chat.html)共用。

// 展示净化：把模型输出的 markdown 格式符号从气泡文本中去掉
// （textContent 不渲染它们，不清理就变成了奶龙说话带星号竖线）
function stripMd(s) {
    let t = String(s || "");
    // 表格分隔行（|---|---|）整行去掉
    t = t.replace(/^\s*\|?\s*:?-{2,}[\s:|-]*\|?\s*$/gm, "");
    t = t.replace(/\|/g, " ");
    t = t.replace(/^\s*#{1,6}\s+/gm, "");
    t = t.replace(/(\*\*|__)(.*?)\1/g, "$2");
    t = t.replace(/(\*|_)(.*?)\1/g, "$2");
    t = t.replace(/^\s*[-*+]\s+/gm, "  ");
    t = t.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1");
    t = t.replace(/```(?:[a-zA-Z0-9]*)\n?/g, "");
    t = t.replace(/`([^`]*)`/g, "$1");
    t = t.replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n");
    return t.trim();
}

// 清掉模型在回复尾巴上自签的"落款"（DeepSeek 系模型偶尔会在角色台词后加
// "——DeepSeek" / "（由 Qwen 生成）" / "by GPT-4o" 之类）。
// 原则：① 尾巴行整个短且是签名 → 删行；② 行内签名短语 → 只删短语留正文；
// ③ 中段签名行删除；④ 整条回复就是一条签名 → 置空交前端兜底。
// 正文正经谈模型用法（API/部署/文档）不动。
function cleanReply(s) {
    const MODEL = /(deepseek|qwen[\w\-.]*|gpt[- ]?\w*|chatgpt|openai|claude)/i;
    const SIGN = /大模型|算法签名|落款|署名|签名|generated\s+by|provided\s+by|made\s+by|authored\s+by|由.{0,8}生成|由.{0,8}提供|出自/i;
    const TOPIC = /(api|sdk|平台|接口|官网|怎么用|用法|文档|示例|代码|部署|接入|付费|限额|充值|token)/i;
    const isSign = (t) => (MODEL.test(t) || SIGN.test(t)) && !TOPIC.test(t);

    let lines = String(s || "").trim().replace(/\n{3,}/g, "\n\n").split(/\n+/);
    for (let i = lines.length - 1; i >= 0; i--) {
        const t = lines[i].trim();
        if (!t) continue;
        // ——DeepSeek / （由 Qwen 生成）这类：从签名记号起含签名特征 → 截掉
        const mm = t.match(/([（(\[【—~·*,，][ \t]*)([^—~\n]{0,40})$/);
        if (mm && isSign(mm[2])) {
            lines[i] = t.slice(0, t.indexOf(mm[1])).trim();
            continue;
        }
        // 中文句后的英文签名尾巴："，by DeepSeek"
        const en = t.match(/([ \t,，。；;:：!！?？][ \t]*(?:by|from)[ \t]+[\w\-.]{1,30}[ \t]*$)/i);
        if (en && isSign(en[1])) {
            lines[i] = t.slice(0, t.indexOf(en[1])).trim();
            continue;
        }
        // ③ 尾巴行整个是签名 → 删行（至少留一行，单行情况由④兜底）
        if (lines.length > 1 && t.length <= 26 && isSign(t) && !/[。？！？.!?]/.test(t)) {
            lines.splice(i, 1);
            continue;
        }
        break;
    }
    // ② 签名可能在中间（正文+签名+又补一句）：删以签名记号开头、无句尾标点的签名行
    lines = lines.filter(t => {
        const tt = t.trim();
        return !(tt.length <= 24 && isSign(tt) && !/[。？！？.!?]/.test(tt) &&
                 /(^[—–~·*（(\[【]|^\s+(?:by|from)\b)/i.test(tt));
    });
    let body = lines.join("\n").trim();
    // ④ 整条回复就是签名 → 置空：以签名记号开头的短签名句，无句尾标点
    if (body && body.length <= 30 && isSign(body) && !/[。？！？.!?]/.test(body) &&
        /^[—–~·*（(\[【][ \t]*[\w\-.一-龥]{1,20}$/.test(body)) {
        body = "";
    }
    return body;
}
