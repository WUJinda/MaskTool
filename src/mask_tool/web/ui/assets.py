# -*- coding: utf-8 -*-
"""静态资产注入：logo SVG、全局样式（static/app.css）、主题侦测桥。

static/app.css 头部维护「Streamlit 内部选择器清单」——升级
Streamlit 版本时它是唯一需要核对的样式检查点。
"""
from pathlib import Path

import streamlit as st

# web/ 包根（components/ 与 static/ 的定位锚点；frozen 下由 spec 收集到
# _MEIPASS/mask_tool/web/，本模块 __file__ 同在该布局内）
WEB_ROOT = Path(__file__).resolve().parent.parent


def _inject_css():
    """注入全局样式（static/app.css；缺失时报错而非静默，便于打包问题早暴露）。"""
    css_path = WEB_ROOT / "static" / "app.css"
    try:
        css = css_path.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(
            f"样式表缺失或不可读：{css_path}（打包收集遗漏？运行 pyinstaller 前请核对 "
            "mask-tool.spec 的 web/static 数据项）"
        ) from e
    st.markdown(f"<style>\n{css}\n    </style>", unsafe_allow_html=True)

# ──────────────────────────────────────────────
# 品牌 logo（内联 SVG，v2 扁平设计：深蓝紫底 + 白文档 + 品牌紫遮蔽条，
# 与 assets/icon/masktool-icon.svg 同构；纯色无渐变）
# ──────────────────────────────────────────────

def _logo_svg(size: int = 34) -> str:
    """生成内联 SVG logo。≤24px 切换简化构图（纸占比加大、遮蔽条加粗），
    保证侧边栏小尺寸下依然清晰。"""
    if size <= 24:
        body = (
            '<rect width="256" height="256" rx="56" fill="#1E2440"/>'
            '<rect x="58" y="44" width="140" height="168" rx="9" fill="#FFFFFF"/>'
            '<rect x="76" y="90" width="104" height="26" rx="6" fill="#5B6EE8"/>'
            '<rect x="76" y="132" width="76" height="26" rx="6" fill="#5B6EE8"/>'
        )
    else:
        body = (
            '<rect width="256" height="256" rx="56" fill="#1E2440"/>'
            '<rect x="66" y="50" width="124" height="156" rx="10" fill="#FFFFFF"/>'
            '<polygon points="158,50 190,50 190,82" fill="#1E2440"/>'
            '<polygon points="158,50 190,82 158,82" fill="#D9DEEB"/>'
            '<rect x="84" y="92" width="88" height="20" rx="5" fill="#5B6EE8"/>'
            '<rect x="84" y="124" width="64" height="20" rx="5" fill="#5B6EE8"/>'
            '<rect x="84" y="162" width="88" height="8" rx="4" fill="#C9CEDC"/>'
        )
    return (
        f'<svg class="mt-logo" width="{size}" height="{size}" viewBox="0 0 256 256"'
        f' xmlns="http://www.w3.org/2000/svg" role="img" aria-label="mask-tool logo">{body}</svg>'
    )



def _inject_theme_bridge():
    """隐形 iframe 主题侦测：监听父页 .stApp 背景亮度维护 <html data-app-theme>。

    Streamlit 未暴露主题 CSS 变量，且 st.context.theme 需 reload 才更新；
    手动 light/dark（app_settings.yaml）时直接锁定并标记 data-theme-forced，
    auto 时保持亮度侦测；rerun 会重建本 iframe，脚本随设置更新重新生效。
    """
    import json as _json

    from mask_tool.core.app_settings import get_theme as _get_manual_theme

    _manual_theme = _json.dumps(_get_manual_theme())
    import streamlit.components.v1 as _components
    _components.html(
        f"""
        <script>
        (function () {{
          try {{
            var d = window.parent.document;
            var manual = {_manual_theme};
            function upd() {{
              if (manual === 'light' || manual === 'dark') {{
                d.documentElement.setAttribute('data-app-theme', manual);
                d.documentElement.setAttribute('data-theme-forced', '1');
                return;
              }}
              d.documentElement.removeAttribute('data-theme-forced');
              var app = d.querySelector('.stApp');
              if (!app) return;
              var bg = getComputedStyle(app).backgroundColor || '';
              var m = bg.match(/([\\d]+)\\s*,\\s*([\\d]+)\\s*,\\s*([\\d]+)/);
              if (!m) return;
              var lum = (parseInt(m[1],10)*299 + parseInt(m[2],10)*587 + parseInt(m[3],10)*114) / 1000;
              d.documentElement.setAttribute('data-app-theme', lum > 128 ? 'light' : 'dark');
            }}
            upd();
            var app = d.querySelector('.stApp');
            if (app && typeof MutationObserver !== 'undefined') {{
              new MutationObserver(upd).observe(app, {{attributes: true, attributeFilter: ['class','style']}});
            }}
            /* AgGrid 深色涂装：st-aggrid 是 iframe 组件，父页 CSS 穿透不了；
               同源直接向其 document.head 注入 CSS 变量覆盖（ag-grid 31 的
               theme='dark' 字符串/custom_css 参数在该库 1.2.1 均无效，
               实测只能这样注入）。深色注入、浅色移除，轮询兼容 rerun 重建。 */
            var AG_CSS = '.ag-root-wrapper{{--ag-background-color:#1a1d2e;'
              + '--ag-data-background-color:#1a1d2e;'
              + '--ag-foreground-color:#dfe2ee;--ag-secondary-foreground-color:#9aa3b5;'
              + '--ag-disabled-foreground-color:rgba(223,226,238,0.38);'
              + '--ag-border-color:rgba(255,255,255,0.12);'
              + '--ag-header-background-color:#14161f;--ag-header-text-color:#9aa3b5;'
              + '--ag-odd-row-background-color:#1d2032;'
              + '--ag-row-hover-color:rgba(91,110,232,0.15);'
              + '--ag-selected-row-background-color:rgba(91,110,232,0.22);'
              + '--ag-checkbox-fill-color:#5b6ee8;--ag-accent-color:#5b6ee8;'
              + '--ag-toggle-button-off-background-color:rgba(255,255,255,0.15);'
              + '--ag-chip-background-color:rgba(91,110,232,0.18);'
              + '--ag-input-border-color:rgba(255,255,255,0.18);'
              + '--ag-range-selection-border-color:#5b6ee8;'
              + '--ag-header-column-resize-handle-color:rgba(255,255,255,0.3)}}'
              + '.ag-paging-panel{{color:#9aa3b5}}'
              + '.ag-overlay-loading-center,.ag-overlay-no-rows-center{{color:#9aa3b5}}'
              + '.ag-cell,.ag-cell-value{{color:var(--ag-foreground-color)}}'
              + '.ag-header-cell-text{{color:var(--ag-header-text-color)}}'
              + '.ag-floating-top,.ag-body-viewport,.ag-center-cols-container{{color:var(--ag-foreground-color)}}'
              + '.ag-picker-field-wrapper{{background:rgba(255,255,255,0.08);'
              + 'border-color:rgba(255,255,255,0.18);color:var(--ag-foreground-color)}}'
              + '.ag-select .ag-picker-field-display{{color:var(--ag-foreground-color)}}';
            function paintAg() {{
              var dark = d.documentElement.getAttribute('data-app-theme') === 'dark';
              for (var i = 0; i < d.querySelectorAll('iframe').length; i++) {{
                var f = d.querySelectorAll('iframe')[i];
                if ((f.getAttribute('src') || '').indexOf('st_aggrid') < 0) continue;
                try {{
                  var gd = f.contentDocument;
                  if (!gd || !gd.head || !gd.querySelector('.ag-root-wrapper')) continue;
                  var el = gd.getElementById('mt-ag-dark');
                  if (dark && !el) {{
                    var s = gd.createElement('style');
                    s.id = 'mt-ag-dark';
                    s.textContent = AG_CSS;
                    gd.head.appendChild(s);
                  }} else if (!dark && el) {{
                    el.parentNode.removeChild(el);
                  }}
                }} catch (err) {{ /* 跨域/未就绪时静默 */ }}
              }}
            }}
            paintAg();
            /* MutationObserver：iframe 新插入（重建/首次挂载）时立即涂装，
               避免最多 800ms 的 light→dark 补涂闪烁；轮询仅作兜底 */
            new MutationObserver(function () {{ paintAg(); }}).observe(
              d.body, {{ childList: true, subtree: true }}
            );
            setInterval(paintAg, 800);
          }} catch (e) {{ /* 跨域/异常时静默，保持默认 light */ }}
        }})();
        </script>
        """,
        height=0,
    )

