/* mask-tool 设置弹窗 · Streamlit components.v2 组件
 *
 * ── 组件契约（与 mask_tool/web/ui/settings_dialog.py 对应）──────────────
 * Python → JS（每轮 render 经 component.data 下发 payload）：
 *   { lexicon: [{cat,label,icon,words[]}], whitelist: [word, ...],
 *     theme: 'auto|light|dark',
 *     save_dir, save_dir_default, save_dir_explicit: bool,
 *     llm: {base_url, model, role, api_key_set: bool},
 *     flash: {level:'ok|err', text} | null }
 * JS → Python（component.setTriggerValue('event', json) → trigger 变化自动
 * rerun → _handle_settings_event(ev) 消费，_ts 时间戳去重）：
 *   {action:'set_theme', theme} | {action:'save_dir', path}
 *   | {action:'reset_dir'} | {action:'open_folder'}
 *   | {action:'create_category', name} | {action:'add_words', cat, words[]}
 *   | {action:'add_whitelist', words[]} | {action:'remove_whitelist', word}   ← R9 白名单
 *   | {action:'export_csv'} | {action:'import_csv', text}
 *   | {action:'save_llm', base_url, model, api_key, role}   ← P3 模型配置
 *   | {action:'test_llm', base_url, model, api_key}
 *
 * ── 运行环境 ─────────────────────────────────────────────────────────
 * components.v2 的 JS 运行在主文档（非沙箱），可直接操作侧栏/页面 DOM：
 * 侧栏底部设置按钮 + 弹窗/子弹窗/toast 均注入 document.body。
 * UI 与原型（settings-dialog-preview.html）完全一致。
 *
 * ── Streamlit 内部结构依赖（升级 1.64 → 更高版本时唯一检查点）─────────
 * 下方 ST.sidebarBlock 引用的 data-testid 是 Streamlit 内部约定，
 * 仅用于把设置按钮注入侧栏底部；升级后若失效，只需改这一处常量。
 */

/* Streamlit 内部 DOM 选择器（集中收敛，勿散落使用） */
var ST = {
  sidebarBlock: '[data-testid="stSidebar"] [data-testid="stVerticalBlock"]',
};

export default function (component) {
  /* 跨 rerun 存活的单例桥接对象（组件每轮 render 重建 default export，
   * 但 window.__mtSettings 持有 UI 状态与通道引用，避免散落全局） */
  var B = window.__mtSettings || (window.__mtSettings = {
    data: {},                                        // 最近一轮 Python payload
    ui: { open: false, tab: 'basic', sub: null },    // 弹窗 UI 状态（跨 rerun 保持）
    booted: false,                                   // DOM/CSS 只注入一次
    onData: null,                                    // 数据刷新入口（boot 后由内部注册）
    emit: null,                                      // setTriggerValue 通道（每轮 render 刷新）
  });

  B.data = (component && component.data) || {};
  B.emit = function (name, value) {
    try { component.setTriggerValue(name, value); } catch (e) { /* 已卸载时忽略 */ }
  };

  if (!B.booted) {
    B.booted = true;
    boot(B);
  } else if (B.onData) {
    B.onData(B.data);
  }
}

function boot(B) {
  var doc = document;
  var q = function (id) { return doc.getElementById(id); };
  var ARGS = B.data;
  var UI = B.ui;

  function sendEvent(ev) {
    ev._ts = Date.now(); // 时间戳保证 trigger 值唯一，确保每次动作都触发 Python 端
    B.emit('event', JSON.stringify(ev));
  }

  /* ── 样式（作用域限定 #mt-settings-root / #mt-settings-entry，与原型一致） ── */
  var CSS = `
#mt-settings-entry { position: fixed; left: 13px; bottom: 13px; z-index: 300; }
#mt-settings-entry .mt-settings-icon {
  width: 34px; height: 34px; display: flex; align-items: center; justify-content: center;
  background: rgba(25,26,46,.045); border: 1px solid rgba(25,26,46,.1);
  color: #343b4e; border-radius: 8px; cursor: pointer;
  transition: background .15s, color .15s;
}
html[data-app-theme="dark"] #mt-settings-entry .mt-settings-icon {
  background: rgba(255,255,255,.07); border-color: rgba(255,255,255,.1); color: #dfe2ee;
}
#mt-settings-entry .mt-settings-icon:hover { background: rgba(91,110,232,.14); color: #4a5bd4; }
html[data-app-theme="dark"] #mt-settings-entry .mt-settings-icon:hover { background: rgba(91,110,232,.25); color: #a9b6ff; }
#mt-settings-entry .mt-gear { width: 18px; height: 18px; transition: transform .4s; }
#mt-settings-entry .mt-settings-icon:hover .mt-gear { transform: rotate(60deg); }

#mt-settings-root .mt-overlay {
  position: fixed; inset: 0; background: rgba(15,17,30,.45);
  display: none; align-items: center; justify-content: center; z-index: 100000;
  backdrop-filter: blur(2px);
}
#mt-settings-root .mt-overlay.open { display: flex; }
#mt-settings-root .mt-dialog {
  width: 780px; max-width: 94vw; height: 540px; max-height: 88vh;
  background: #fff; border-radius: 14px; box-shadow: 0 18px 60px rgba(15,17,30,.35);
  display: flex; overflow: hidden; animation: mt-pop .18s ease-out;
  position: relative;
}
html[data-app-theme="dark"] #mt-settings-root .mt-dialog { background: #1d2032; }
@keyframes mt-pop { from { transform: scale(.96); opacity: 0; } to { transform: scale(1); opacity: 1; } }

#mt-settings-root .mt-dlg-nav {
  width: 186px; flex-shrink: 0; background: #f6f7fa; border-right: 1px solid rgba(25,26,46,.07);
  padding: 1.1rem .7rem; display: flex; flex-direction: column;
}
html[data-app-theme="dark"] #mt-settings-root .mt-dlg-nav { background: #181a2b; border-right-color: rgba(255,255,255,.07); }
#mt-settings-root .mt-dlg-title { font-size: 1rem; font-weight: 800; color: #1e2440; padding: 0 .5rem .9rem; }
html[data-app-theme="dark"] #mt-settings-root .mt-dlg-title { color: #e8eaf2; }
#mt-settings-root .mt-nav-item {
  display: flex; align-items: center; gap: .55rem; padding: .55rem .65rem;
  border-radius: 8px; font-size: .85rem; font-weight: 600; color: #4a5064;
  cursor: pointer; margin-bottom: .25rem; border: none; background: none; width: 100%;
  text-align: left; font-family: inherit; transition: background .12s;
}
#mt-settings-root .mt-nav-item:hover { background: rgba(91,110,232,.09); }
#mt-settings-root .mt-nav-item.active {
  background: linear-gradient(135deg, rgba(91,110,232,.14), rgba(118,75,162,.14));
  color: #4a5bd4; position: relative;
}
#mt-settings-root .mt-nav-item.active::before {
  content: ""; position: absolute; left: 0; top: 22%; bottom: 22%; width: 3px;
  border-radius: 2px; background: linear-gradient(180deg, #5b6ee8, #764ba2);
}
html[data-app-theme="dark"] #mt-settings-root .mt-nav-item { color: #aab2c5; }
html[data-app-theme="dark"] #mt-settings-root .mt-nav-item.active { color: #a9b6ff; background: rgba(91,110,232,.2); }
#mt-settings-root .mt-nav-item .soon {
  margin-left: auto; font-size: .62rem; padding: .08rem .38rem; border-radius: 99px;
  background: rgba(25,26,46,.08); color: rgba(43,48,64,.55); font-weight: 700;
}
html[data-app-theme="dark"] #mt-settings-root .mt-nav-item .soon { background: rgba(255,255,255,.1); color: #8a93a5; }

#mt-settings-root .mt-dlg-body { flex: 1; overflow-y: auto; padding: 0.4rem 1.5rem 1.3rem; position: relative; }
/* 关闭按钮挂在不滚动的 dialog 层：内容滚动时恒定右上角 */
#mt-settings-root .mt-dlg-close {
  position: absolute; top: .9rem; right: 1rem; width: 28px; height: 28px;
  border-radius: 7px; border: none; background: rgba(25,26,46,.05); color: #4a5064;
  font-size: 1rem; cursor: pointer; line-height: 1; z-index: 10;
}
html[data-app-theme="dark"] #mt-settings-root .mt-dlg-close { background: rgba(255,255,255,.08); color: #aab2c5; }
#mt-settings-root .mt-dlg-close:hover { background: rgba(192,57,43,.12); color: #c0392b; }
/* 面板标题：sticky 固定在滚动区顶部（全宽背景条，滚动时遮住下方内容） */
#mt-settings-root .mt-pane-title {
  font-size: 1.02rem; font-weight: 800; color: #1e2440;
  display: flex; align-items: baseline;
  position: sticky; top: 0; z-index: 5;
  margin: 0 -1.5rem 1rem; padding: 0.85rem 3.2rem 0.7rem 1.5rem;
  background: #fff; border-bottom: 1px solid rgba(25,26,46,.07);
}
html[data-app-theme="dark"] #mt-settings-root .mt-pane-title {
  color: #e8eaf2; background: #1d2032; border-bottom-color: rgba(255,255,255,.07);
}
#mt-settings-root .mt-pane { display: none; }
#mt-settings-root .mt-pane.active { display: block; }

#mt-settings-root .mt-set-card {
  border: 1px solid rgba(25,26,46,.1); background: rgba(25,26,46,.025);
  border-radius: 10px; padding: .95rem 1.05rem; margin-bottom: .9rem;
}
html[data-app-theme="dark"] #mt-settings-root .mt-set-card { border-color: rgba(255,255,255,.1); background: rgba(255,255,255,.03); }
#mt-settings-root .mt-card-title { font-size: .88rem; font-weight: 700; margin-bottom: .55rem; display: flex; align-items: center; gap: .4rem; }
#mt-settings-root .mt-card-sub { font-size: .74rem; color: rgba(43,48,64,.58); margin-bottom: .7rem; line-height: 1.5; }
html[data-app-theme="dark"] #mt-settings-root .mt-card-sub { color: #8a93a5; }

#mt-settings-root .mt-seg { display: inline-flex; border: 1px solid rgba(25,26,46,.14); border-radius: 8px; overflow: hidden; }
html[data-app-theme="dark"] #mt-settings-root .mt-seg { border-color: rgba(255,255,255,.14); }
#mt-settings-root .mt-seg button {
  border: none; background: transparent; padding: .42rem .95rem; font-size: .8rem;
  cursor: pointer; color: #4a5064; font-weight: 600; display: flex; align-items: center; gap: .35rem; font-family: inherit;
}
html[data-app-theme="dark"] #mt-settings-root .mt-seg button { color: #aab2c5; }
#mt-settings-root .mt-seg button.active { background: linear-gradient(135deg, #5b6ee8, #764ba2); color: #fff; }

#mt-settings-root .mt-path-row {
  display: flex; align-items: center; gap: .55rem; background: #fff;
  border: 1px solid rgba(25,26,46,.16); border-radius: 7px; padding: .35rem .35rem .35rem .75rem;
}
html[data-app-theme="dark"] #mt-settings-root .mt-path-row { background: rgba(255,255,255,.06); border-color: rgba(255,255,255,.13); }
#mt-settings-root .mt-path-row code {
  flex: 1; font-size: .78rem; color: #4a5bd4; font-family: Consolas, monospace;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
html[data-app-theme="dark"] #mt-settings-root .mt-path-row code { color: #a9b6ff; }
#mt-settings-root .mt-mini-btn {
  border: 1px solid rgba(91,110,232,.45); color: #4a5bd4; background: rgba(91,110,232,.07);
  border-radius: 6px; padding: .28rem .7rem; font-size: .76rem; font-weight: 600; cursor: pointer; white-space: nowrap; font-family: inherit;
}
html[data-app-theme="dark"] #mt-settings-root .mt-mini-btn { background: rgba(91,110,232,.16); color: #a9b6ff; border-color: rgba(91,110,232,.5); }
#mt-settings-root .mt-mini-btn:hover { background: rgba(91,110,232,.16); }
#mt-settings-root .mt-ghost-btn {
  border: none; background: rgba(25,26,46,.05); color: #4a5064; border-radius: 6px;
  padding: .28rem .7rem; font-size: .76rem; font-weight: 600; cursor: pointer; font-family: inherit;
}
html[data-app-theme="dark"] #mt-settings-root .mt-ghost-btn { background: rgba(255,255,255,.07); color: #aab2c5; }
#mt-settings-root .mt-ghost-btn:hover { background: rgba(25,26,46,.09); }
#mt-settings-root .mt-manual-path { display: flex; align-items: center; gap: .5rem; margin-top: .6rem; }
#mt-settings-root .mt-manual-path input {
  flex: 1; height: 1.9rem; border-radius: 6px; padding: 0 .6rem; font-size: .78rem; outline: none;
  border: 1px solid rgba(25,26,46,.18); background: #fff; color: #2b3040; font-family: inherit; min-width: 0;
}
html[data-app-theme="dark"] #mt-settings-root .mt-manual-path input { background: rgba(255,255,255,.08); border-color: rgba(255,255,255,.16); color: #dfe2ee; }
#mt-settings-root .mt-manual-path input:focus { border-color: #5b6ee8; box-shadow: 0 0 0 2px rgba(91,110,232,.18); }

#mt-settings-root .mt-pane-title .mt-lex-count { font-size: .72rem; font-weight: 600; color: rgba(43,48,64,.5); margin-left: .55rem; }
html[data-app-theme="dark"] #mt-settings-root .mt-pane-title .mt-lex-count { color: #8a93a5; }
#mt-settings-root .mt-toolbar { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; margin-bottom: .85rem; }
#mt-settings-root .mt-tool-btn {
  display: inline-flex; align-items: center; gap: .32rem;
  border-radius: 7px; padding: .34rem .75rem; font-size: .78rem; font-weight: 700;
  cursor: pointer; transition: filter .12s, background .12s; white-space: nowrap; font-family: inherit;
}
#mt-settings-root .mt-tool-btn.add {
  background: linear-gradient(135deg, #5b6ee8, #764ba2); color: #fff;
  box-shadow: 0 1px 5px rgba(91,110,232,.3); border: none;
}
#mt-settings-root .mt-tool-btn.add:hover { filter: brightness(1.08); }
#mt-settings-root .mt-tool-btn.io {
  background: rgba(25,26,46,.045); color: #4a5bd4; border: 1px solid rgba(91,110,232,.35);
}
html[data-app-theme="dark"] #mt-settings-root .mt-tool-btn.io { background: rgba(255,255,255,.06); color: #a9b6ff; border-color: rgba(91,110,232,.45); }
#mt-settings-root .mt-tool-btn.io:hover { background: rgba(91,110,232,.12); }
#mt-settings-root .mt-tb-sep { width: 1px; height: 1.15rem; background: rgba(25,26,46,.12); margin: 0 .1rem; }
html[data-app-theme="dark"] #mt-settings-root .mt-tb-sep { background: rgba(255,255,255,.14); }

#mt-settings-root .mt-lex-cat { border: 1px solid rgba(25,26,46,.1); border-radius: 9px; margin-bottom: .6rem; overflow: hidden; }
html[data-app-theme="dark"] #mt-settings-root .mt-lex-cat { border-color: rgba(255,255,255,.1); }
#mt-settings-root .mt-lex-cat summary {
  list-style: none; display: flex; align-items: center; justify-content: space-between;
  padding: .55rem .85rem; font-size: .84rem; font-weight: 700; cursor: pointer;
  background: rgba(25,26,46,.02); user-select: none;
}
html[data-app-theme="dark"] #mt-settings-root .mt-lex-cat summary { background: rgba(255,255,255,.02); }
#mt-settings-root .mt-lex-cat summary::-webkit-details-marker { display: none; }
#mt-settings-root .mt-lex-cat summary .cnt { font-size: .72rem; font-weight: 800; color: #5b6ee8; }
html[data-app-theme="dark"] #mt-settings-root .mt-lex-cat summary .cnt { color: #9fb0ff; }
#mt-settings-root .mt-lex-words { padding: .3rem .85rem .6rem; display: flex; flex-wrap: wrap; gap: .4rem; }
#mt-settings-root .mt-word-tag {
  font-size: .76rem; background: rgba(91,110,232,.09); color: #3d4bb8;
  border: 1px solid rgba(91,110,232,.18); border-radius: 6px; padding: .16rem .55rem;
  font-family: Consolas, monospace;
}
html[data-app-theme="dark"] #mt-settings-root .mt-word-tag { background: rgba(91,110,232,.16); color: #a9b6ff; border-color: rgba(91,110,232,.3); }
#mt-settings-root .mt-lex-empty { font-size: .76rem; color: rgba(43,48,64,.5); align-self: center; }
/* R9 白名单：tag 内嵌移除按钮 */
#mt-settings-root .mt-wl-tag { display: inline-flex; align-items: center; gap: .3rem; }
#mt-settings-root .mt-wl-del {
  border: none; background: transparent; cursor: pointer; padding: 0 .05rem;
  font-size: .7rem; line-height: 1; color: rgba(43,48,64,.4); border-radius: 4px;
}
#mt-settings-root .mt-wl-del:hover { color: #e5484d; background: rgba(229,72,77,.12); }
html[data-app-theme="dark"] #mt-settings-root .mt-wl-del { color: rgba(255,255,255,.38); }
html[data-app-theme="dark"] #mt-settings-root .mt-lex-empty { color: #8a93a5; }
#mt-settings-root .mt-csv-note { font-size: .73rem; color: rgba(43,48,64,.55); line-height: 1.6; margin-top: .8rem; }
html[data-app-theme="dark"] #mt-settings-root .mt-csv-note { color: #8a93a5; }
#mt-settings-root .mt-csv-note code { background: rgba(25,26,46,.06); padding: .06rem .35rem; border-radius: 4px; font-size: .72rem; }
html[data-app-theme="dark"] #mt-settings-root .mt-csv-note code { background: rgba(255,255,255,.08); }

#mt-settings-root .mt-empty-state { text-align: center; padding: 3.2rem 1rem; color: rgba(43,48,64,.5); }
#mt-settings-root .mt-empty-state .emoji { font-size: 2.6rem; margin-bottom: .8rem; }
#mt-settings-root .mt-empty-state .t { font-size: .95rem; font-weight: 700; color: rgba(43,48,64,.7); margin-bottom: .35rem; }
#mt-settings-root .mt-empty-state .d { font-size: .78rem; line-height: 1.6; }

#mt-settings-root .mt-sub-overlay {
  position: fixed; inset: 0; background: rgba(15,17,30,.35);
  display: none; align-items: center; justify-content: center; z-index: 100100;
}
#mt-settings-root .mt-sub-overlay.open { display: flex; }
#mt-settings-root .mt-sub-dialog {
  width: 400px; max-width: 92vw; background: #fff; border-radius: 12px;
  box-shadow: 0 14px 44px rgba(15,17,30,.3); padding: 1.1rem 1.2rem 1rem;
  animation: mt-pop .16s ease-out;
}
html[data-app-theme="dark"] #mt-settings-root .mt-sub-dialog { background: #232639; }
#mt-settings-root .mt-sub-title { font-size: .95rem; font-weight: 800; color: #1e2440; margin-bottom: .85rem; display: flex; align-items: center; gap: .4rem; }
html[data-app-theme="dark"] #mt-settings-root .mt-sub-title { color: #e8eaf2; }
#mt-settings-root .mt-sub-field { margin-bottom: .75rem; }
#mt-settings-root .mt-sub-field label { display: block; font-size: .76rem; font-weight: 700; color: rgba(43,48,64,.72); margin-bottom: .3rem; }
#mt-settings-root .mt-fmt-hint {
  display: inline-flex; align-items: center; justify-content: center;
  width: 14px; height: 14px; margin-left: 4px; border-radius: 50%;
  background: rgba(25,26,46,.07); color: #8a93a5;
  font-size: 10px; font-weight: 800; cursor: help; vertical-align: 1px;
}
#mt-settings-root .mt-fmt-hint:hover { background: rgba(91,110,232,.15); color: #4a5bd0; }
html[data-app-theme="dark"] #mt-settings-root .mt-sub-field label { color: #aab2c5; }
#mt-settings-root .mt-sub-field .ctrl {
  width: 100%; height: 2rem; border-radius: 7px; padding: 0 .6rem; font-size: .82rem; outline: none;
  border: 1px solid rgba(25,26,46,.18); background: #fff; color: #2b3040; font-family: inherit; box-sizing: border-box;
}
html[data-app-theme="dark"] #mt-settings-root .mt-sub-field .ctrl { background: rgba(255,255,255,.08); border-color: rgba(255,255,255,.16); color: #dfe2ee; }
#mt-settings-root .mt-sub-field .ctrl:focus { border-color: #5b6ee8; box-shadow: 0 0 0 2px rgba(91,110,232,.18); }
/* API Key 输入行：包裹层 + 右侧眼睛切换按钮 */
#mt-settings-root .mt-key-wrap { position: relative; }
#mt-settings-root .mt-key-wrap .ctrl { padding-right: 2.1rem; }
#mt-settings-root .mt-eye {
  position: absolute; right: .3rem; top: 50%; transform: translateY(-50%);
  width: 1.6rem; height: 1.6rem; border: none; background: transparent; cursor: pointer;
  display: flex; align-items: center; justify-content: center; border-radius: 6px; color: #8a93a5;
}
#mt-settings-root .mt-eye:hover { color: #5b6ee8; background: rgba(91,110,232,.1); }
#mt-settings-root .mt-eye svg { width: 15px; height: 15px; }
html[data-app-theme="dark"] #mt-settings-root .mt-eye { color: #8a93a5; }
#mt-settings-root .mt-sub-actions { display: flex; justify-content: flex-end; gap: .5rem; margin-top: .2rem; }
#mt-settings-root .mt-ok-btn {
  background: linear-gradient(135deg, #5b6ee8, #764ba2); color: #fff; border: none;
  border-radius: 7px; padding: .4rem 1.1rem; font-size: .8rem; font-weight: 700; cursor: pointer;
  box-shadow: 0 1px 5px rgba(91,110,232,.3); font-family: inherit;
}
#mt-settings-root .mt-ok-btn:hover { filter: brightness(1.08); }

#mt-settings-root .mt-toast {
  position: fixed; top: 1.4rem; left: 50%; transform: translateX(-50%) translateY(-8px);
  background: #1e2440; color: #fff; font-size: .82rem; font-weight: 600;
  border-radius: 8px; padding: .55rem 1.1rem; box-shadow: 0 6px 24px rgba(15,17,30,.3);
  opacity: 0; pointer-events: none; transition: all .25s; z-index: 100200; max-width: 80vw;
}
#mt-settings-root .mt-toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
#mt-settings-root .mt-toast.err { background: #8e3327; }
`;
  var style = doc.createElement('style');
  style.id = 'mt-settings-style';
  style.textContent = CSS;
  doc.head.appendChild(style);

  /* ── 弹窗 DOM ── */
  var root = doc.createElement('div');
  root.id = 'mt-settings-root';
  root.innerHTML = `
    <div class="mt-overlay" id="mt-overlay">
      <div class="mt-dialog" role="dialog" aria-label="设置">
        <button class="mt-dlg-close" id="mt-close" title="关闭">✕</button>
        <nav class="mt-dlg-nav">
          <div class="mt-dlg-title">⚙️ 设置</div>
          <button class="mt-nav-item active" data-pane="basic"><span class="ic">🧩</span>基本设置</button>
          <button class="mt-nav-item" data-pane="lexicon"><span class="ic">📖</span>词库管理</button>
          <button class="mt-nav-item" data-pane="model"><span class="ic">💻</span>模型配置</button>
        </nav>
        <div class="mt-dlg-body">

          <section class="mt-pane active" id="mt-pane-basic">
            <div class="mt-pane-title">基本设置</div>
            <div class="mt-set-card">
              <div class="mt-card-title">🎨 主题风格</div>
              <div class="mt-card-sub">选择界面配色；「跟随系统」自动匹配操作系统的深浅色设置</div>
              <div class="mt-seg" id="mt-theme-seg">
                <button data-theme="light">☀️ 浅色</button>
                <button data-theme="auto" class="active">🌗 跟随系统</button>
                <button data-theme="dark">🌙 深色</button>
              </div>
            </div>
            <div class="mt-set-card">
              <div class="mt-card-title">📁 保存路径</div>
              <div class="mt-card-sub">脱敏 / 恢复文件的输出文件夹；未自定义时使用安装目录下 output</div>
              <div class="mt-path-row">
                <code id="mt-save-path"></code>
                <button class="mt-mini-btn" id="mt-browse">浏览…</button>
              </div>
              <div class="mt-manual-path">
                <input id="mt-manual-path" placeholder="手动输入保存文件夹绝对路径，回车应用">
                <button class="mt-ghost-btn" id="mt-apply-path">应用</button>
              </div>
              <div style="display:flex;gap:.55rem;margin-top:.6rem">
                <button class="mt-ghost-btn" id="mt-open-folder">📂 打开文件夹</button>
                <button class="mt-ghost-btn" id="mt-reset-dir">↩️ 恢复默认</button>
              </div>
            </div>
          </section>

          <section class="mt-pane" id="mt-pane-lexicon">
            <div class="mt-pane-title">词库管理<span class="mt-lex-count" id="mt-lex-count"></span></div>
            <div class="mt-toolbar">
              <button class="mt-tool-btn add" id="mt-add-cat">＋ 新增类别</button>
              <button class="mt-tool-btn add" id="mt-add-word">＋ 新增敏感词</button>
              <span class="mt-tb-sep"></span>
              <button class="mt-tool-btn io" id="mt-export-csv">⬇️ 导出 CSV</button>
              <button class="mt-tool-btn io" id="mt-import-csv">⬆️ 导入 CSV</button>
              <input type="file" id="mt-csv-file" accept=".csv" style="display:none">
            </div>
            <div id="mt-lex-list"></div>
            <div class="mt-set-card" style="margin-top:.9rem">
              <div class="mt-card-title" style="margin-bottom:.4rem">🚫 白名单
                <span class="mt-lex-count" id="mt-wl-count"></span>
                <button class="mt-mini-btn" id="mt-add-wl" style="margin-left:auto">＋ 添加白名单词</button>
              </div>
              <div class="mt-card-sub">白名单内的词不会被检测识别（适用于自动检测的常见误报）；检测页勾选「对未勾选的敏感词进行永久排除」也会写入这里。点击词条右侧 ✕ 移除。</div>
              <div class="mt-lex-words" id="mt-wl-list" style="padding:.35rem 0 .1rem"></div>
            </div>
            <div class="mt-csv-note">
              💡 CSV 格式为两列 <code>类别,词条</code>（UTF-8 编码，首行为表头）。导出后在离线机器上通过「导入 CSV」合并入词库，
              无需重新录入；导入时自动映射中文分类，重复词条自动跳过。导出文件保存到当前「保存路径」文件夹。
            </div>
          </section>

          <section class="mt-pane" id="mt-pane-model">
            <div class="mt-pane-title">模型配置</div>
            <div class="mt-set-card">
              <div class="mt-card-title">💻 AI 增强检测（内网大模型）</div>
              <div class="mt-card-sub">接入 OpenAI 兼容端点；保存后在侧栏「AI 增强检测」开关启用。</div>
              <div class="mt-sub-field">
                <label>服务地址（Base URL）<span class="mt-fmt-hint" title="以版本段结尾，如 /v1 或 /v4。示例：Ollama http://localhost:11434/v1 · vLLM http://内网IP:8000/v1 · 智谱 https://open.bigmodel.cn/api/paas/v4。⚠️ Anthropic 专用地址（/api/anthropic）不适用。">?</span></label>
                <input class="ctrl" id="mt-llm-url" placeholder="如 http://192.168.1.10:11434/v1">
              </div>
              <div class="mt-sub-field">
                <label>模型名称</label>
                <input class="ctrl" id="mt-llm-model" placeholder="如 qwen3:8b">
              </div>
              <div class="mt-sub-field">
                <label>API Key（内网通常留空）</label>
                <div class="mt-key-wrap">
                  <input class="ctrl" id="mt-llm-key" type="password" placeholder="可选；也可用环境变量 MASKTOOL_LLM_API_KEY">
                  <button type="button" id="mt-llm-key-eye" class="mt-eye" aria-label="显示/隐藏 API Key" title="显示/隐藏"></button>
                </div>
              </div>
              <div class="mt-sub-field">
                <label>使用方式</label>
                <div class="mt-seg" id="mt-llm-role">
                  <button data-role="adjudicator" class="active">🛡️ 仅复核误报</button>
                  <button data-role="detector">🔎 仅补充检测</button>
                  <button data-role="both">⚡ 复核+检测</button>
                </div>
              </div>
              <div style="display:flex;gap:.55rem;margin-top:.7rem;align-items:center;flex-wrap:wrap">
                <button class="mt-ok-btn" id="mt-llm-save">保存配置</button>
                <button class="mt-ghost-btn" id="mt-llm-test">🔌 测试连接</button>
                <span id="mt-llm-status" style="font-size:.74rem;color:#8a93a5"></span>
              </div>
            </div>
            <div class="mt-csv-note">💡 隐私：仅发送待复核片段（前后各 50 字上下文）至上述端点，建议内网或本机部署。</div>
          </section>
        </div>
      </div>
    </div>

    <div class="mt-sub-overlay" id="mt-sub-cat">
      <div class="mt-sub-dialog" role="dialog" aria-label="新增类别">
        <div class="mt-sub-title">🏷️ 新增类别</div>
        <div class="mt-sub-field">
          <label>类别名称</label>
          <input class="ctrl" id="mt-new-cat" placeholder="如：品牌、部门、供应商…">
        </div>
        <div class="mt-sub-actions">
          <button class="mt-ghost-btn" id="mt-cancel-cat">取消</button>
          <button class="mt-ok-btn" id="mt-confirm-cat">创建</button>
        </div>
      </div>
    </div>

    <div class="mt-sub-overlay" id="mt-sub-word">
      <div class="mt-sub-dialog" role="dialog" aria-label="新增敏感词">
        <div class="mt-sub-title">✏️ 新增敏感词</div>
        <div class="mt-sub-field">
          <label>所属类别</label>
          <select class="ctrl" id="mt-word-cat"></select>
        </div>
        <div class="mt-sub-field">
          <label>敏感词条（多条用逗号分隔）</label>
          <input class="ctrl" id="mt-new-word" placeholder="如：某某公司，某某项目，张三">
        </div>
        <div class="mt-sub-actions">
          <button class="mt-ghost-btn" id="mt-cancel-word">取消</button>
          <button class="mt-ok-btn" id="mt-confirm-word">添加</button>
        </div>
      </div>
    </div>

    <div class="mt-sub-overlay" id="mt-sub-wl">
      <div class="mt-sub-dialog" role="dialog" aria-label="添加白名单词">
        <div class="mt-sub-title">🚫 添加白名单词</div>
        <div class="mt-sub-field">
          <label>词条（多条用逗号分隔）</label>
          <input class="ctrl" id="mt-new-wl" placeholder="如：有限公司，某某项目，张三">
        </div>
        <div class="mt-sub-actions">
          <button class="mt-ghost-btn" id="mt-cancel-wl">取消</button>
          <button class="mt-ok-btn" id="mt-confirm-wl">添加</button>
        </div>
      </div>
    </div>

    <div class="mt-toast" id="mt-toast"></div>
  `;
  doc.body.appendChild(root);

  /* ── 侧栏底部设置按钮 ── */
  var GEAR_SVG = '<svg class="mt-gear" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';
  var EYE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
  var EYE_OFF_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
  (function initEye() {
    var eye = doc.getElementById('mt-llm-key-eye');
    if (eye) eye.innerHTML = EYE_SVG;
  })();

  /* 幂等注入：目标容器（Streamlit 侧栏垂直块）不存在时静默返回；
   * 按钮已挂在当前容器上时直接复用；容器被重建（rerun）时先移除旧节点再注入 */
  function ensureSettingsButton() {
    var sb = doc.querySelector(ST.sidebarBlock);
    if (!sb) return;
    var existing = doc.getElementById('mt-settings-entry');
    if (existing && existing.parentNode === sb) return;
    if (existing) existing.remove();
    var wrap = doc.createElement('div');
    wrap.id = 'mt-settings-entry';
    var btn = doc.createElement('button');
    btn.className = 'mt-settings-icon';
    btn.title = '设置';
    btn.setAttribute('aria-label', '打开设置');
    btn.innerHTML = GEAR_SVG;
    btn.addEventListener('click', openSettings);
    wrap.appendChild(btn);
    sb.appendChild(wrap);
  }

  /* 事件驱动的注入保活：监听文档子树变化，合并到同一帧内只做一次
   * isConnected 廉价检查，按钮脱挂（侧栏被 Streamlit 重建）时才重注入。
   * 替代旧版 800ms 定时轮询方案（空转 + 升级脆弱）。 */
  function watchSettingsButton() {
    if (typeof MutationObserver === 'undefined') {
      ensureSettingsButton(); // 极老 WebView 无 Observer：退化为单次注入
      return;
    }
    var scheduled = false;
    new MutationObserver(function () {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(function () {
        scheduled = false;
        var entry = doc.getElementById('mt-settings-entry');
        if (!entry || !entry.isConnected) ensureSettingsButton();
      });
    }).observe(doc.body, { childList: true, subtree: true });
  }

  /* ── 基础交互 ── */
  function openSettings() { UI.open = true; q('mt-overlay').classList.add('open'); }
  function closeSettings() { UI.open = false; UI.sub = null; q('mt-overlay').classList.remove('open'); closeSub(); }
  function switchPane(name) {
    UI.tab = name;
    doc.querySelectorAll('#mt-settings-root .mt-nav-item').forEach(function (b) { b.classList.toggle('active', b.dataset.pane === name); });
    doc.querySelectorAll('#mt-settings-root .mt-pane').forEach(function (p) { p.classList.toggle('active', p.id === 'mt-pane-' + name); });
  }
  function openSub(which) {
    closeSub();
    UI.sub = which;
    var panelMap = { cat: 'mt-sub-cat', word: 'mt-sub-word', wl: 'mt-sub-wl' };
    var inputMap = { cat: 'mt-new-cat', word: 'mt-new-word', wl: 'mt-new-wl' };
    q(panelMap[which]).classList.add('open');
    var first = q(inputMap[which]);
    if (first) setTimeout(function () { first.focus(); }, 30);
    if (which === 'word') buildWordCatOptions();
  }
  function closeSub() {
    UI.sub = null;
    ['mt-sub-cat', 'mt-sub-word', 'mt-sub-wl'].forEach(function (id) {
      q(id).classList.remove('open');
    });
  }

  function bindOnce(id, ev, fn) {
    var el = q(id);
    if (el) el.addEventListener(ev, fn);
  }

  q('mt-overlay').addEventListener('click', function (e) { if (e.target === q('mt-overlay')) closeSettings(); });
  bindOnce('mt-close', 'click', closeSettings);
  q('mt-sub-cat').addEventListener('click', function (e) { if (e.target === q('mt-sub-cat')) closeSub(); });
  q('mt-sub-word').addEventListener('click', function (e) { if (e.target === q('mt-sub-word')) closeSub(); });
  doc.querySelectorAll('#mt-settings-root .mt-nav-item').forEach(function (b) {
    b.addEventListener('click', function () { switchPane(b.dataset.pane); });
  });
  doc.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (q('mt-sub-cat').classList.contains('open') || q('mt-sub-word').classList.contains('open')) closeSub();
    else if (q('mt-overlay').classList.contains('open')) closeSettings();
  });

  var toastTimer;
  function toast(msg, isErr) {
    var t = q('mt-toast');
    t.textContent = msg;
    t.classList.toggle('err', !!isErr);
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('show'); }, 2400);
  }

  /* ── 主题 ── */
  doc.querySelectorAll('#mt-theme-seg button').forEach(function (b) {
    b.addEventListener('click', function () {
      var mode = b.dataset.theme;
      doc.querySelectorAll('#mt-theme-seg button').forEach(function (x) { x.classList.remove('active'); });
      b.classList.add('active');
      var real = mode;
      var html = doc.documentElement;
      if (mode === 'auto') {
        html.removeAttribute('data-theme-forced');
        real = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
      } else {
        html.setAttribute('data-theme-forced', '1');
      }
      html.setAttribute('data-app-theme', real);
      sendEvent({ action: 'set_theme', theme: mode });
      toast(mode === 'auto' ? '已切换为跟随系统（当前：' + (real === 'dark' ? '深色' : '浅色') + '）'
                            : '已切换为' + (mode === 'dark' ? '深色' : '浅色') + '主题');
    });
  });

  /* ── 保存路径 ── */
  q('mt-browse').addEventListener('click', async function () {
    var api = window.pywebview && window.pywebview.api;
    if (!api || !api.pick_save_folder) {
      toast('当前环境不支持原生选择，请在下方手动输入路径', true);
      return;
    }
    toast('等待选择文件夹…');
    try {
      var dir = await api.pick_save_folder();
      if (dir) sendEvent({ action: 'save_dir', path: dir });
      else toast('已取消');
    } catch (e) { toast('选择失败：' + e, true); }
  });
  function applyManualPath() {
    var v = q('mt-manual-path').value.trim().replace(/^["']|["']$/g, '');
    if (!v) { toast('请输入路径', true); return; }
    sendEvent({ action: 'save_dir', path: v });
  }
  bindOnce('mt-apply-path', 'click', applyManualPath);
  bindOnce('mt-manual-path', 'keydown', function (e) { if (e.key === 'Enter') applyManualPath(); });
  bindOnce('mt-open-folder', 'click', function () { sendEvent({ action: 'open_folder' }); });
  bindOnce('mt-reset-dir', 'click', function () { sendEvent({ action: 'reset_dir' }); });

  /* ── 词库 ── */
  function esc(s) { return String(s).replace(/[&<>"']/g, function (m) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]; }); }

  function renderLexicon() {
    var lex = ARGS.lexicon || [];
    var total = 0; lex.forEach(function (c) { total += (c.words || []).length; });
    q('mt-lex-count').textContent = '共 ' + total + ' 条 · ' + lex.length + ' 个分类';
    q('mt-lex-list').innerHTML = lex.map(function (c) {
      var words = c.words || [];
      var inner = words.length
        ? words.map(function (w) { return '<span class="mt-word-tag">' + esc(w) + '</span>'; }).join('')
        : '<span class="mt-lex-empty">暂无词条，点击「＋ 新增敏感词」添加</span>';
      return '<div class="mt-lex-cat"><details open><summary>' + (c.icon || '🏷️') + ' ' + esc(c.label) +
        ' <span class="cnt">' + words.length + ' 条</span></summary><div class="mt-lex-words">' + inner + '</div></details></div>';
    }).join('') || '<div class="mt-lex-empty">词库未加载</div>';
  }
  function buildWordCatOptions() {
    var lex = ARGS.lexicon || [];
    var sel = q('mt-word-cat');
    var prev = sel.value;
    sel.innerHTML = lex.map(function (c) {
      return '<option value="' + esc(c.cat) + '">' + (c.icon || '') + ' ' + esc(c.label) + '</option>';
    }).join('');
    if (prev && lex.some(function (c) { return c.cat === prev; })) sel.value = prev;
  }

  bindOnce('mt-add-cat', 'click', function () { openSub('cat'); });
  bindOnce('mt-add-word', 'click', function () { openSub('word'); });
  bindOnce('mt-cancel-cat', 'click', closeSub);
  bindOnce('mt-cancel-word', 'click', closeSub);

  function confirmAddCat() {
    var name = q('mt-new-cat').value.trim();
    if (!name) { toast('请输入类别名称', true); return; }
    q('mt-new-cat').value = '';
    closeSub();
    sendEvent({ action: 'create_category', name: name });
    toast('正在创建类别「' + name + '」…');
  }
  bindOnce('mt-confirm-cat', 'click', confirmAddCat);
  bindOnce('mt-new-cat', 'keydown', function (e) { if (e.key === 'Enter') confirmAddCat(); });

  function confirmAddWord() {
    var cat = q('mt-word-cat').value;
    var raw = q('mt-new-word').value.trim();
    if (!raw) { toast('请输入敏感词', true); return; }
    var words = raw.replace(/，/g, ',').split(',').map(function (s) { return s.trim(); }).filter(Boolean);
    q('mt-new-word').value = '';
    closeSub();
    sendEvent({ action: 'add_words', cat: cat, words: words });
    toast('正在添加 ' + words.length + ' 条词条…');
  }
  bindOnce('mt-confirm-word', 'click', confirmAddWord);
  bindOnce('mt-new-word', 'keydown', function (e) { if (e.key === 'Enter') confirmAddWord(); });

  bindOnce('mt-export-csv', 'click', function () { sendEvent({ action: 'export_csv' }); toast('正在导出词库…'); });

  /* ── 白名单（R9）── */
  function renderWhitelist() {
    var wl = ARGS.whitelist || [];
    var c = q('mt-wl-count');
    if (c) c.textContent = wl.length ? '共 ' + wl.length + ' 条' : '';
    var list = q('mt-wl-list');
    if (!list) return;
    list.innerHTML = wl.length
      ? wl.map(function (w) {
          return '<span class="mt-word-tag mt-wl-tag">' + esc(w) +
            '<button class="mt-wl-del" data-w="' + esc(w) + '" title="从白名单移除">✕</button></span>';
        }).join('')
      : '<span class="mt-lex-empty">暂无白名单词条</span>';
  }
  function confirmAddWl() {
    var raw = q('mt-new-wl').value.trim();
    if (!raw) { toast('请输入词条', true); return; }
    var words = raw.replace(/，/g, ',').split(',').map(function (s) { return s.trim(); }).filter(Boolean);
    q('mt-new-wl').value = '';
    closeSub();
    sendEvent({ action: 'add_whitelist', words: words });
    toast('正在添加 ' + words.length + ' 条白名单…');
  }
  bindOnce('mt-add-wl', 'click', function () { openSub('wl'); });
  bindOnce('mt-confirm-wl', 'click', confirmAddWl);
  bindOnce('mt-cancel-wl', 'click', closeSub);
  bindOnce('mt-new-wl', 'keydown', function (e) { if (e.key === 'Enter') confirmAddWl(); });
  /* tag 为动态渲染，删除走事件委托 */
  root.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target.closest('.mt-wl-del') : null;
    if (!t) return;
    sendEvent({ action: 'remove_whitelist', word: t.getAttribute('data-w') });
    toast('正在移除「' + t.getAttribute('data-w') + '」…');
  });
  bindOnce('mt-import-csv', 'click', function () { q('mt-csv-file').click(); });
  bindOnce('mt-csv-file', 'change', function () {
    var f = this.files && this.files[0];
    if (!f) return;
    var reader = new FileReader();
    reader.onload = function () {
      sendEvent({ action: 'import_csv', text: String(reader.result) });
      toast('正在导入 ' + f.name + ' …');
    };
    reader.readAsText(f, 'utf-8');
    this.value = '';
  });

  /* ── 模型配置（P3）── */
  function llmRole() {
    var active = q('mt-llm-role') && q('mt-llm-role').querySelector('button.active');
    return active ? active.dataset.role : 'adjudicator';
  }
  doc.querySelectorAll('#mt-llm-role button').forEach(function (b) {
    b.addEventListener('click', function () {
      doc.querySelectorAll('#mt-llm-role button').forEach(function (x) { x.classList.remove('active'); });
      b.classList.add('active');
    });
  });
  function llmForm() {
    return {
      base_url: q('mt-llm-url').value.trim(),
      model: q('mt-llm-model').value.trim(),
      api_key: q('mt-llm-key').value,
      role: llmRole(),
    };
  }
  /* 表单草稿：测试/保存触发 rerun，刷新时先回填未保存的表单值（否则被
     已保存的 yaml 值覆盖——测试的目的正是验证未保存的输入）。
     挂 B.ui 跨 rerun：iframe 重建时局部变量会丢 */
  var DRAFT = B.ui.draft || null;
  function _stashDraft(f) { B.ui.draft = f; DRAFT = f; }
  function _dropDraft() { B.ui.draft = null; DRAFT = null; }
  bindOnce('mt-llm-save', 'click', function () {
    var f = llmForm();
    if (!f.model) { toast('请填写模型名称', true); return; }
    _stashDraft(f);
    sendEvent({ action: 'save_llm', base_url: f.base_url, model: f.model,
                api_key: f.api_key, role: f.role });
    toast('正在保存模型配置…');
  });
  bindOnce('mt-llm-test', 'click', function () {
    var f = llmForm();
    if (!f.base_url || !f.model) { toast('请先填写服务地址与模型名称', true); return; }
    _stashDraft(f);
    q('mt-llm-status').textContent = '测试中…';
    sendEvent({ action: 'test_llm', base_url: f.base_url, model: f.model,
                api_key: f.api_key });
  });

  /* ── 数据刷新入口（组件每轮 render 调用；ARGS 引用替换为新 payload） ── */
  B.onData = function (args) {
    ARGS = args || {};
    renderLexicon();
    renderWhitelist();
    var theme = ARGS.theme || 'auto';
    doc.querySelectorAll('#mt-theme-seg button').forEach(function (b) {
      b.classList.toggle('active', b.dataset.theme === theme);
    });
    q('mt-save-path').textContent = ARGS.save_dir || ARGS.save_dir_default || '';
    q('mt-save-path').title = q('mt-save-path').textContent;
    if (doc.activeElement !== q('mt-manual-path')) q('mt-manual-path').value = '';
    q('mt-reset-dir').disabled = !ARGS.save_dir_explicit;
    /* P3：模型配置回填（输入焦点中的字段不覆盖；api_key 仅回显已设置状态） */
    var llm = ARGS.llm || {};
    ['mt-llm-url', 'mt-llm-model'].forEach(function (id) {
      var el = q(id);
      if (el && doc.activeElement !== el) el.value = llm[id === 'mt-llm-url' ? 'base_url' : 'model'] || '';
    });
    var keyEl = q('mt-llm-key');
    if (keyEl && doc.activeElement !== keyEl) {
      /* 回显已保存的 key（明文保存在本地配置，组件为同源本地 iframe）；
         输入焦点中不覆盖；password 类型默认遮罩，眼睛按钮切换显隐 */
      keyEl.value = llm.api_key || '';
    }
    /* 眼睛按钮：切换 password/text + 图标切换 */
    bindOnce('mt-llm-key-eye', 'click', function () {
      var el = q('mt-llm-key');
      var show = el.type === 'password';
      el.type = show ? 'text' : 'password';
      this.innerHTML = show ? EYE_OFF_SVG : EYE_SVG;
    });
    doc.querySelectorAll('#mt-llm-role button').forEach(function (b) {
      b.classList.toggle('active', b.dataset.role === (llm.role || 'adjudicator'));
    });
    /* 草稿回填：rerun 后优先恢复测试/保存前未落盘的表单值。
       保留到 ARGS.llm 与草稿一致（=已保存）才清除：onData 单轮 rerun
       可能被调多次（v2 组件多次 render），一次性 drop 会让后续调用
       用空 yaml 再次覆盖，表单仍被清空 */
    if (DRAFT) {
      if (doc.activeElement !== q('mt-llm-url')) q('mt-llm-url').value = DRAFT.base_url;
      if (doc.activeElement !== q('mt-llm-model')) q('mt-llm-model').value = DRAFT.model;
      if (doc.activeElement !== q('mt-llm-key')) q('mt-llm-key').value = DRAFT.api_key;
      doc.querySelectorAll('#mt-llm-role button').forEach(function (b) {
        b.classList.toggle('active', b.dataset.role === DRAFT.role);
      });
      var saved = (llm.base_url || '') === DRAFT.base_url
        && (llm.model || '') === DRAFT.model
        && (llm.api_key || '') === (DRAFT.api_key || '')
        && (llm.role || 'adjudicator') === DRAFT.role;
      if (saved) _dropDraft();
    }
    if (ARGS.flash && ARGS.flash.text) {
      q('mt-llm-status').textContent = ARGS.flash.text;
      toast(ARGS.flash.text, ARGS.flash.level === 'err');
    } else {
      q('mt-llm-status').textContent = '';
    }
    /* rerun 后侧栏容器可能被重建：顺带做一次幂等检查（正常路径早退，零成本） */
    ensureSettingsButton();
  };

  /* ── 启动 ── */
  ensureSettingsButton();
  watchSettingsButton();
  if (UI.tab && UI.tab !== 'basic') switchPane(UI.tab);
  if (UI.open) q('mt-overlay').classList.add('open');
  if (UI.sub === 'cat' || UI.sub === 'word' || UI.sub === 'wl') {
    var _pm = { cat: 'mt-sub-cat', word: 'mt-sub-word', wl: 'mt-sub-wl' };
    q(_pm[UI.sub]).classList.add('open');
  }
  B.onData(ARGS);
}
