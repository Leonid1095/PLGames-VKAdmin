"""VK Mini App — admin panel inside VK iframe."""

import json
import logging
from html import escape
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.vk_auth import verify_vk_launch_params, create_miniapp_token, verify_miniapp_token
from database.service import (
    get_group, get_groups_by_admin, get_setting, set_setting,
    get_content_sources, add_content_source, delete_content_source,
    get_content_tasks, create_content_task, delete_content_task,
)
from web.dashboard.routes import SETTINGS_SCHEMA

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── Auth helper ──────────────────────────────────────────────────────────────

def _get_auth(request: Request) -> dict | None:
    """Verify Mini App token from query param or form field."""
    token = (
        request.query_params.get("token", "")
        or request.query_params.get("t", "")
    )
    if not token:
        return None
    return verify_miniapp_token(token)


def _error_page(msg: str) -> HTMLResponse:
    return HTMLResponse(_miniapp_html("Ошибка", f"""
        <div class="card"><p style="color:#d32f2f;">{msg}</p></div>
    """, ""), status_code=403)


# ─── HTML template ────────────────────────────────────────────────────────────

def _miniapp_html(title: str, content: str, token: str, body_class: str = "") -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="https://unpkg.com/@vkontakte/vk-bridge@2/dist/browser.min.js"></script>
<script>
(function() {{
    function initBridge() {{
        if (typeof vkBridge !== 'undefined') {{
            vkBridge.send('VKWebAppInit')
                .then(function() {{ console.log('VK Bridge initialized'); }})
                .catch(function(e) {{ console.warn('VK Bridge init error:', e); }});
        }} else {{
            console.warn('vkBridge not loaded, retrying...');
            setTimeout(initBridge, 100);
        }}
    }}
    if (document.readyState === 'loading') {{
        document.addEventListener('DOMContentLoaded', initBridge);
    }} else {{
        initBridge();
    }}
}})();
</script>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif; background: #EBEDF0; color: #222; line-height: 1.5; padding: 12px; }}

    .card {{ background: white; border-radius: 10px; padding: 16px; margin-bottom: 10px; }}
    .card-title {{ font-size: 1rem; font-weight: 700; margin-bottom: 12px; display: flex; align-items: center; gap: 6px; }}

    .group-card {{ display: flex; justify-content: space-between; align-items: center; }}
    .group-info h3 {{ font-size: 0.95rem; margin-bottom: 2px; }}
    .group-info p {{ font-size: 0.8rem; color: #888; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.75rem; font-weight: 500; }}
    .badge-green {{ background: #e8f5e9; color: #2e7d32; }}

    .btn {{ display: inline-block; padding: 8px 16px; background: #2688EB; color: white; text-decoration: none; border-radius: 8px; border: none; cursor: pointer; font-size: 0.9rem; font-weight: 500; }}
    .btn:hover {{ background: #1f7ad8; }}
    .btn-sm {{ padding: 6px 14px; font-size: 0.82rem; }}
    .btn-danger {{ background: #d32f2f; }}

    .setting {{ padding: 12px 0; border-bottom: 1px solid #f0f0f0; }}
    .setting:last-child {{ border-bottom: none; }}
    .setting-label {{ font-weight: 600; font-size: 0.9rem; }}
    .setting-desc {{ font-size: 0.78rem; color: #888; margin-top: 1px; }}
    .setting-control {{ margin-top: 8px; }}

    input[type="text"], textarea, select {{
        width: 100%; padding: 8px 10px; border: 1.5px solid #e0e0e0; border-radius: 8px;
        font-size: 0.88rem; font-family: inherit; background: #fafafa;
    }}
    input:focus, textarea:focus, select:focus {{ outline: none; border-color: #2688EB; background: white; }}
    textarea {{ resize: vertical; min-height: 60px; }}

    .toggle {{ position: relative; display: inline-block; width: 44px; height: 24px; }}
    .toggle input {{ opacity: 0; width: 0; height: 0; }}
    .toggle-slider {{
        position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
        background: #ccc; border-radius: 24px; transition: 0.2s;
    }}
    .toggle-slider:before {{
        content: ""; position: absolute; height: 18px; width: 18px; left: 3px; bottom: 3px;
        background: white; border-radius: 50%; transition: 0.2s;
    }}
    .toggle input:checked + .toggle-slider {{ background: #4caf50; }}
    .toggle input:checked + .toggle-slider:before {{ transform: translateX(20px); }}

    .save-btn {{
        margin-top: 6px; padding: 6px 14px; font-size: 0.8rem; background: #4caf50;
        color: white; border: none; border-radius: 6px; cursor: pointer; display: none;
    }}
    .save-btn.show {{ display: inline-block; }}

    .flash {{ padding: 10px 14px; border-radius: 8px; margin-bottom: 10px; font-size: 0.85rem; }}
    .flash-success {{ background: #e8f5e9; color: #2e7d32; }}

    .back {{ color: #2688EB; text-decoration: none; display: inline-flex; align-items: center; gap: 4px; margin-bottom: 10px; font-size: 0.85rem; }}

    .source-table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
    .source-table th {{ text-align: left; font-size: 0.75rem; color: #999; padding: 4px 6px; border-bottom: 2px solid #f0f0f0; }}
    .source-table td {{ padding: 8px 6px; border-bottom: 1px solid #f5f5f5; font-size: 0.85rem; }}
    .source-url {{ color: #2688EB; word-break: break-all; }}
    .source-type {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 0.75rem; font-weight: 500; }}
    .source-type-rss {{ background: #fff3e0; color: #e65100; }}
    .source-type-vk {{ background: #e3f2fd; color: #1565c0; }}
    .source-type-api {{ background: #f3e5f5; color: #7b1fa2; }}
    .source-type-web {{ background: #e0f2f1; color: #00695c; }}
    .source-empty {{ text-align: center; padding: 16px; color: #bbb; font-size: 0.85rem; }}
    .source-add {{ display: flex; gap: 6px; align-items: flex-end; margin-top: 12px; flex-wrap: wrap; }}
    .source-add .form-group {{ margin: 0; }}
    .source-add label {{ font-size: 0.8rem; display: block; margin-bottom: 2px; font-weight: 500; }}
    .btn-delete {{ background: none; border: none; color: #d32f2f; cursor: pointer; font-size: 0.82rem; padding: 3px 6px; }}
    .hint {{ margin-top: 6px; font-size: 0.75rem; color: #aaa; }}

    .toast {{
        position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
        background: #2e7d32; color: white; padding: 10px 24px; border-radius: 10px;
        font-size: 0.9rem; font-weight: 500; z-index: 9999; opacity: 0;
        transition: opacity 0.3s; pointer-events: none;
    }}
    .toast.show {{ opacity: 1; }}

    /* Bottom navigation */
    .bottom-nav {{
        position: fixed; bottom: 0; left: 0; right: 0;
        background: white; border-top: 1px solid #e0e0e0;
        display: flex; justify-content: space-around; align-items: center;
        padding: 6px 0 env(safe-area-inset-bottom, 8px); z-index: 100;
    }}
    .nav-item {{
        display: flex; flex-direction: column; align-items: center;
        text-decoration: none; color: #999; font-size: 0.68rem; padding: 4px 12px;
        transition: color 0.15s;
    }}
    .nav-item.active {{ color: #2688EB; }}
    .nav-item span {{ font-size: 1.3rem; line-height: 1; }}
    body.has-nav {{ padding-bottom: 64px; }}

    /* Progress bar */
    .progress-bar {{ background: #e0e0e0; border-radius: 8px; height: 10px; overflow: hidden; }}
    .progress-fill {{ background: linear-gradient(90deg, #2688EB, #42a5f5); height: 100%; border-radius: 8px; transition: width 0.5s; }}

    /* Stat grid */
    .stat-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 12px 0; }}
    .stat-item {{ background: #f5f5f5; border-radius: 8px; padding: 12px; text-align: center; }}
    .stat-value {{ font-size: 1.3rem; font-weight: 700; color: #222; }}
    .stat-label {{ font-size: 0.75rem; color: #888; margin-top: 2px; }}

    /* Leaderboard */
    .lb-row {{ display: flex; align-items: center; padding: 10px 12px; border-bottom: 1px solid #f0f0f0; }}
    .lb-row:last-child {{ border-bottom: none; }}
    .lb-rank {{ font-size: 1.1rem; font-weight: 700; width: 32px; text-align: center; color: #888; }}
    .lb-rank.gold {{ color: #f9a825; }}
    .lb-rank.silver {{ color: #90a4ae; }}
    .lb-rank.bronze {{ color: #a1887f; }}
    .lb-info {{ flex: 1; margin-left: 10px; }}
    .lb-name {{ font-size: 0.9rem; font-weight: 600; color: #222; }}
    .lb-stats {{ font-size: 0.75rem; color: #888; }}
    .lb-xp {{ font-size: 0.9rem; font-weight: 700; color: #2688EB; }}
    .lb-me {{ background: #e3f2fd; border-radius: 8px; }}

    /* Shop */
    .shop-card {{
        background: linear-gradient(135deg, #7c4dff, #651fff); color: white;
        border-radius: 12px; padding: 20px; margin-bottom: 10px; position: relative; overflow: hidden;
    }}
    .shop-card.coins {{ background: linear-gradient(135deg, #f9a825, #ff8f00); }}
    .shop-card h3 {{ font-size: 1.1rem; margin-bottom: 4px; }}
    .shop-card p {{ font-size: 0.82rem; opacity: 0.9; margin-bottom: 12px; }}
    .shop-card .price {{ font-size: 1.3rem; font-weight: 700; }}
    .shop-btn {{ background: rgba(255,255,255,0.25); color: white; border: none; border-radius: 8px; padding: 10px 24px; font-size: 0.9rem; font-weight: 600; cursor: pointer; }}
    .shop-btn:hover {{ background: rgba(255,255,255,0.4); }}

    /* Filter tabs */
    .filter-tabs {{ display: flex; gap: 6px; margin-bottom: 12px; overflow-x: auto; }}
    .filter-tab {{
        padding: 6px 14px; border-radius: 16px; border: 1.5px solid #e0e0e0;
        background: white; font-size: 0.8rem; color: #666; cursor: pointer; white-space: nowrap;
        text-decoration: none;
    }}
    .filter-tab.active {{ background: #2688EB; color: white; border-color: #2688EB; }}

    /* Settings tabs */
    .settings-tabs {{ display: flex; gap: 4px; overflow-x: auto; padding: 0 0 8px; margin-bottom: 8px; position: sticky; top: 0; background: #EBEDF0; z-index: 50; }}
    .settings-tab {{
        padding: 8px 14px; border-radius: 10px; font-size: 0.78rem; font-weight: 500;
        background: white; color: #666; cursor: pointer; white-space: nowrap; border: none;
        box-shadow: 0 1px 2px rgba(0,0,0,0.06);
    }}
    .settings-tab.active {{ background: #2688EB; color: white; }}
    .tab-content {{ display: none; }}
    .tab-content.active {{ display: block; }}
</style>
<script>
function showToast(msg) {{
    var t = document.getElementById('toast');
    t.textContent = msg || 'Сохранено';
    t.classList.add('show');
    setTimeout(function(){{ t.classList.remove('show'); }}, 2000);
}}
function ajaxSubmit(form) {{
    var data = new FormData(form);
    fetch(form.action, {{
        method: 'POST',
        body: data,
        headers: {{'X-Requested-With': 'XMLHttpRequest'}}
    }}).then(function(r) {{
        if (r.ok) showToast();
        else showToast('Ошибка');
    }}).catch(function() {{ showToast('Ошибка сети'); }});
    return false;
}}
</script>
</head>
<body class="{body_class}"><div id="toast" class="toast"></div>{content}</body></html>"""


def _bottom_nav(active: str, token: str, group_id: int = 0, is_admin: bool = False) -> str:
    """Render bottom navigation bar."""
    gid_param = f"&gid={group_id}" if group_id else ""
    items = [
        ("profile", "👤", "Профиль", f"/miniapp/profile?token={token}{gid_param}"),
        ("leaderboard", "🏆", "Рейтинг", f"/miniapp/leaderboard?token={token}{gid_param}"),
        ("shop", "🛒", "Магазин", f"/miniapp/shop?token={token}{gid_param}"),
    ]
    if is_admin and group_id:
        items.append(("settings", "⚙️", "Настройки", f"/miniapp/group/{group_id}?token={token}"))

    nav = ""
    for key, icon, label, href in items:
        cls = "nav-item active" if key == active else "nav-item"
        nav += f'<a href="{href}" class="{cls}"><span>{icon}</span>{label}</a>'

    return f'<nav class="bottom-nav">{nav}</nav>'


# ─── User pages ──────────────────────────────────────────────────────────────

@router.get("/miniapp/profile")
async def miniapp_profile(request: Request):
    """User profile page — XP, level, stats."""
    auth = _get_auth(request)
    if not auth:
        return _error_page("Сессия истекла. Откройте приложение заново.")

    token = request.query_params.get("token", request.query_params.get("t", ""))
    vk_user_id = auth["uid"]
    group_id = int(request.query_params.get("gid", auth.get("gid", 0)))

    if not group_id:
        return _error_page("Откройте приложение из группы ВКонтакте.")

    group = await get_group(group_id)
    if not group:
        return _error_page("Группа не найдена")

    from database.service import get_user_stats, get_top_users
    stats = await get_user_stats(group_id, vk_user_id)

    # Calculate level progress
    xp_for_current = (stats.level - 1) ** 2 * 10 if stats.level > 1 else 0
    xp_for_next = stats.level ** 2 * 10
    xp_in_level = stats.xp - xp_for_current
    xp_needed = xp_for_next - xp_for_current
    progress_pct = min(100, int(xp_in_level / xp_needed * 100)) if xp_needed > 0 else 0

    # Find user's rank
    top = await get_top_users(group_id, order_by="xp", limit=100)
    rank = next((i for i, u in enumerate(top, 1) if u.vk_id == vk_user_id), "—")

    # VIP status
    vip_html = ""
    if stats.is_vip:
        expires = stats.vip_expires.strftime("%d.%m.%Y") if stats.vip_expires else "Навсегда"
        vip_html = f'<div style="background:#7c4dff;color:white;padding:8px 14px;border-radius:8px;font-size:0.85rem;margin-bottom:10px;">⭐ VIP до {expires}</div>'

    requests_left = "∞" if stats.is_vip else max(0, 10 - stats.daily_requests)
    is_admin = group.admin_vk_id == vk_user_id
    nav = _bottom_nav("profile", token, group_id, is_admin)
    group_name = escape(group.group_name or f"Группа {group_id}")

    content = f"""
    {vip_html}
    <div class="card">
        <div style="text-align:center;padding:8px 0;">
            <div style="font-size:2.5rem;line-height:1;">👤</div>
            <div style="font-size:0.8rem;color:#888;margin-top:4px;">{group_name}</div>
            <div style="font-size:1.4rem;font-weight:700;margin-top:4px;">Уровень {stats.level}</div>
            <div style="font-size:0.82rem;color:#888;margin-top:2px;">Ранг #{rank}</div>
        </div>
        <div style="margin:12px 0 4px;">
            <div style="display:flex;justify-content:space-between;font-size:0.75rem;color:#888;">
                <span>{stats.xp} XP</span>
                <span>{xp_for_next} XP</span>
            </div>
            <div class="progress-bar">
                <div class="progress-fill" style="width:{progress_pct}%"></div>
            </div>
            <div style="text-align:center;font-size:0.72rem;color:#aaa;margin-top:3px;">
                Ещё {xp_for_next - stats.xp} XP до уровня {stats.level + 1}
            </div>
        </div>
    </div>

    <div class="stat-grid">
        <div class="stat-item">
            <div class="stat-value">{stats.messages_count}</div>
            <div class="stat-label">Сообщений</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{stats.reputation}</div>
            <div class="stat-label">Репутация</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{stats.balance:.0f}</div>
            <div class="stat-label">Коинов</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{requests_left}</div>
            <div class="stat-label">ИИ-запросов</div>
        </div>
    </div>

    <div class="card">
        <div class="card-title" style="font-size:0.9rem;">📊 Активность</div>
        <div style="font-size:0.85rem;color:#666;line-height:1.8;">
            Предупреждений: {stats.warnings}/3<br>
            XP за сообщения, лайки и репосты<br>
            Репутация: +/- через ответы на комментарии
        </div>
    </div>
    {nav}
    """
    return HTMLResponse(_miniapp_html("Профиль", content, token, body_class="has-nav"))


@router.get("/miniapp/leaderboard")
async def miniapp_leaderboard(request: Request):
    """Leaderboard page — top users with filtering."""
    auth = _get_auth(request)
    if not auth:
        return _error_page("Сессия истекла.")

    token = request.query_params.get("token", request.query_params.get("t", ""))
    vk_user_id = auth["uid"]
    group_id = int(request.query_params.get("gid", auth.get("gid", 0)))
    sort_by = request.query_params.get("sort", "xp")

    if not group_id:
        return _error_page("Откройте приложение из группы.")

    group = await get_group(group_id)
    if not group:
        return _error_page("Группа не найдена")

    from database.service import get_top_users

    if sort_by not in ("xp", "level", "messages", "rep"):
        sort_by = "xp"

    top = await get_top_users(group_id, order_by=sort_by, limit=50)

    # Resolve names via VK API
    names = {}
    if top:
        try:
            from core.crypto import decrypt_token
            from vkbottle import API
            vk_token = decrypt_token(group.access_token)
            api = API(token=vk_token)
            vk_ids = [u.vk_id for u in top]
            users = await api.users.get(user_ids=vk_ids)
            names = {u.id: f"{u.first_name} {u.last_name}" for u in users}
        except Exception:
            pass

    # Filter tabs
    sort_labels = {"xp": "По XP", "level": "По уровню", "messages": "Сообщения", "rep": "Репутация"}
    tabs_html = ""
    for key, label in sort_labels.items():
        cls = "filter-tab active" if key == sort_by else "filter-tab"
        tabs_html += f'<a href="/miniapp/leaderboard?token={token}&gid={group_id}&sort={key}" class="{cls}">{label}</a>'

    # Rows
    rows_html = ""
    if not top:
        rows_html = '<p style="text-align:center;color:#aaa;padding:24px;">Пока нет участников. Напишите боту в группе, чтобы начать!</p>'
    else:
        for i, u in enumerate(top, 1):
            name = escape(names.get(u.vk_id, f"id{u.vk_id}"))
            rank_cls = {1: "gold", 2: "silver", 3: "bronze"}.get(i, "")
            me_cls = "lb-me" if u.vk_id == vk_user_id else ""
            stat_val = {"xp": u.xp, "level": u.level, "messages": u.messages_count, "rep": u.reputation}.get(sort_by, u.xp)
            stat_label = {"xp": "XP", "level": "ур.", "messages": "сообщ.", "rep": "реп."}.get(sort_by, "XP")
            rows_html += f"""
            <div class="lb-row {me_cls}">
                <div class="lb-rank {rank_cls}">{i}</div>
                <div class="lb-info">
                    <div class="lb-name">{name}</div>
                    <div class="lb-stats">Ур. {u.level} · {u.messages_count} сообщ.</div>
                </div>
                <div class="lb-xp">{stat_val} {stat_label}</div>
            </div>
            """

    is_admin = group.admin_vk_id == vk_user_id
    nav = _bottom_nav("leaderboard", token, group_id, is_admin)

    content = f"""
    <div class="card" style="padding-bottom:4px;">
        <div class="card-title">🏆 Рейтинг участников</div>
        <div class="filter-tabs">{tabs_html}</div>
    </div>
    <div class="card" style="padding:0;">
        {rows_html}
    </div>
    {nav}
    """
    return HTMLResponse(_miniapp_html("Рейтинг", content, token, body_class="has-nav"))


@router.get("/miniapp/shop")
async def miniapp_shop(request: Request):
    """Shop page — VIP and coins."""
    auth = _get_auth(request)
    if not auth:
        return _error_page("Сессия истекла.")

    token = request.query_params.get("token", request.query_params.get("t", ""))
    vk_user_id = auth["uid"]
    group_id = int(request.query_params.get("gid", auth.get("gid", 0)))

    if not group_id:
        return _error_page("Откройте приложение из группы.")

    group = await get_group(group_id)
    if not group:
        return _error_page("Группа не найдена")

    from database.service import get_user_stats
    stats = await get_user_stats(group_id, vk_user_id)

    balance_html = f"""
    <div class="card" style="text-align:center;">
        <div style="font-size:0.82rem;color:#888;">Ваш баланс</div>
        <div style="font-size:1.8rem;font-weight:700;color:#222;">{stats.balance:.0f} <span style="font-size:0.9rem;color:#888;">коинов</span></div>
    </div>
    """

    vip_status = ""
    if stats.is_vip:
        expires = stats.vip_expires.strftime("%d.%m.%Y") if stats.vip_expires else "Навсегда"
        vip_status = f'<div style="background:#e8f5e9;color:#2e7d32;padding:10px 14px;border-radius:8px;font-size:0.85rem;margin-bottom:10px;">✓ У вас VIP до {expires}</div>'

    is_admin = group.admin_vk_id == vk_user_id
    nav = _bottom_nav("shop", token, group_id, is_admin)

    content = f"""
    {balance_html}
    {vip_status}

    <div class="shop-card">
        <h3>⭐ VIP-статус</h3>
        <p>Безлимитные ИИ-запросы, премиум-модели, приоритетная поддержка</p>
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <div class="price">150 ₽/мес</div>
            <button class="shop-btn" onclick="buyVip()">Купить VIP</button>
        </div>
    </div>

    <div class="shop-card coins">
        <h3>💰 1000 коинов</h3>
        <p>Внутренняя валюта — для будущих возможностей и бонусов</p>
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <div class="price">100 ₽</div>
            <button class="shop-btn" onclick="buyCoins()">Купить коины</button>
        </div>
    </div>

    <div class="card" style="text-align:center;">
        <p style="font-size:0.82rem;color:#888;">
            Для оплаты напишите администратору группы.<br>
            Скоро здесь появится автоматическая оплата через VK Pay!
        </p>
    </div>

    <script>
    function buyVip() {{
        vkBridge.send('VKWebAppOpenPayForm', {{
            app_id: {group_id},
            action: 'pay-to-group',
            params: {{group_id: {group_id}, amount: 150, description: 'VIP-статус 1 месяц'}}
        }}).then(function(r) {{ showToast('Спасибо за покупку!'); }})
        .catch(function(e) {{
            if (e && e.error_data && e.error_data.error_code !== 4)
                showToast('Напишите администратору для оплаты');
        }});
    }}
    function buyCoins() {{
        vkBridge.send('VKWebAppOpenPayForm', {{
            app_id: {group_id},
            action: 'pay-to-group',
            params: {{group_id: {group_id}, amount: 100, description: '1000 коинов'}}
        }}).then(function(r) {{ showToast('Спасибо за покупку!'); }})
        .catch(function(e) {{
            if (e && e.error_data && e.error_data.error_code !== 4)
                showToast('Напишите администратору для оплаты');
        }});
    }}
    </script>
    {nav}
    """
    return HTMLResponse(_miniapp_html("Магазин", content, token, body_class="has-nav"))


# ─── Entry point ──────────────────────────────────────────────────────────────

@router.get("/miniapp")
async def miniapp_entry(request: Request):
    """Entry point — VK opens this URL with launch params."""
    params = dict(request.query_params)

    # Verify VK launch params
    launch = verify_vk_launch_params(params)
    if not launch:
        # Fallback: try token (already authenticated)
        auth = _get_auth(request)
        if not auth:
            return _error_page("Не удалось проверить подпись VK. Откройте приложение из группы ВКонтакте.")
        vk_user_id = auth["uid"]
        vk_group_id = auth.get("gid", 0)
    else:
        vk_user_id = launch.vk_user_id
        vk_group_id = launch.vk_group_id

    # Create session token
    token = create_miniapp_token(vk_user_id, vk_group_id)

    # If opened from a specific group — go to profile (user-first)
    if vk_group_id:
        group = await get_group(vk_group_id)
        if group:
            return RedirectResponse(f"/miniapp/profile?token={token}&gid={vk_group_id}", status_code=303)

    # Otherwise show all groups this admin manages
    groups = await get_groups_by_admin(vk_user_id)

    if not groups:
        content = f"""
        <div class="card" style="text-align:center; padding:32px;">
            <p style="font-size:1.1rem; font-weight:600; margin-bottom:8px;">Нет подключённых групп</p>
            <p style="color:#888; margin-bottom:16px;">Подключите группу через панель управления</p>
            <a href="/api/vk/oauth" target="_blank" class="btn">Подключить группу</a>
        </div>
        """
        return HTMLResponse(_miniapp_html("VKAdmin", content, token))

    groups_html = ""
    for g in groups:
        name = escape(g.group_name or f"Группа {g.group_id}")
        groups_html += f"""
        <div class="card">
            <div class="group-card">
                <div class="group-info">
                    <h3>{name}</h3>
                    <p>ID: {g.group_id} <span class="badge badge-green">Работает</span></p>
                </div>
                <a href="/miniapp/group/{g.group_id}?token={token}" class="btn btn-sm">Настроить</a>
            </div>
        </div>
        """

    content = f"""
    <div class="card" style="background: linear-gradient(135deg, #2688EB, #1f7ad8); color: white; border-radius: 10px;">
        <h2 style="font-size: 1.1rem; font-weight: 700;">🤖 VKAdmin</h2>
        <p style="opacity: 0.85; font-size: 0.85rem;">AI-администратор ваших групп</p>
    </div>
    {groups_html}
    """
    return HTMLResponse(_miniapp_html("VKAdmin", content, token))


# ─── Group settings ───────────────────────────────────────────────────────────

@router.get("/miniapp/group/{group_id}")
async def miniapp_group_settings(request: Request, group_id: int):
    auth = _get_auth(request)
    if not auth:
        return _error_page("Сессия истекла. Откройте приложение заново.")

    token = request.query_params.get("token", request.query_params.get("t", ""))
    vk_user_id = auth["uid"]

    group = await get_group(group_id)
    if not group:
        return _error_page("Группа не найдена")
    if group.admin_vk_id != vk_user_id:
        return _error_page("У вас нет доступа к этой группе")

    # Group settings into tabs
    TAB_MAP = {
        "Искусственный интеллект": "ai",
        "ИИ-профиль группы": "ai",
        "Модерация": "ai",
        "Автопостинг": "content",
        "Приветствие новых участников": "content",
        "Контент-план": "content",
        "Виджет-лидерборд": "widget",
        "Telegram кросс-постинг": "integrations",
    }
    tab_sections = {"ai": "", "content": "", "widget": "", "integrations": ""}

    for section in SETTINGS_SCHEMA:
        items_html = ""
        for s in section["settings"]:
            key = s["key"]
            current_value = await get_setting(group_id, key, s.get("default", ""))
            control = _render_miniapp_control(group_id, s, current_value, token)
            items_html += f"""
            <div class="setting">
                <div class="setting-label">{s['label']}</div>
                <div class="setting-desc">{s['description']}</div>
                <div class="setting-control">{control}</div>
            </div>
            """

        card = f"""
        <div class="card">
            <div class="card-title">{section['icon']} {section['title']}</div>
            {items_html}
        </div>
        """
        tab_key = TAB_MAP.get(section["title"], "ai")
        tab_sections[tab_key] += card

    # Content sources
    sources = await get_content_sources(group_id)
    sources_rows = ""
    if sources:
        for s in sources:
            type_class = {"rss": "rss", "vk_group": "vk", "api": "api", "web": "web"}.get(s.source_type, "api")
            type_label = {"rss": "RSS", "vk_group": "VK", "api": "API", "web": "Сайт"}.get(s.source_type, s.source_type)
            sources_rows += f"""
            <tr>
                <td><span class="source-type source-type-{type_class}">{type_label}</span></td>
                <td><span class="source-url">{escape(s.source_url)}</span></td>
                <td>
                    <form method="POST" action="/miniapp/group/{group_id}/sources/delete?token={token}"
                          onsubmit="return confirm('Удалить?');">
                        <input type="hidden" name="source_id" value="{s.id}">
                        <button type="submit" class="btn-delete">✕</button>
                    </form>
                </td>
            </tr>
            """
        sources_table = f"""
        <table class="source-table">
            <thead><tr><th>Тип</th><th>Адрес</th><th></th></tr></thead>
            <tbody>{sources_rows}</tbody>
        </table>
        """
    else:
        sources_table = '<p class="source-empty">Нет источников. Добавьте RSS, VK или API.</p>'

    sources_html = f"""
    <div class="card">
        <div class="card-title">📡 Источники контента</div>
        {sources_table}
        <form method="POST" action="/miniapp/group/{group_id}/sources/add?token={token}" class="source-add">
            <div class="form-group">
                <label>Тип</label>
                <select name="source_type" style="width:100px;">
                    <option value="rss">RSS</option>
                    <option value="web">Сайт</option>
                    <option value="vk_group">VK</option>
                    <option value="api">API</option>
                </select>
            </div>
            <div class="form-group" style="flex:1;">
                <label>Адрес</label>
                <input type="text" name="source_url" placeholder="URL или имя группы" required>
            </div>
            <button type="submit" class="btn btn-sm">+</button>
        </form>
        <p class="hint">Бот читает источники и пишет статьи на основе реального контента</p>
    </div>
    """

    # Content tasks
    tasks = await get_content_tasks(group_id)
    tasks_rows = ""
    if tasks:
        for t in tasks:
            last = t.last_run_at.strftime("%d.%m %H:%M") if t.last_run_at else "никогда"
            type_labels = {"patch_notes": "Патч-ноты", "article": "Статья", "digest": "Дайджест"}
            type_label = type_labels.get(t.task_type, t.task_type)
            tasks_rows += f"""
            <tr>
                <td><span class="source-type source-type-api">{type_label}</span></td>
                <td><span class="source-url">{escape(t.source_url or '—')}</span></td>
                <td style="font-size:0.75rem;color:#888;">{escape(t.schedule_cron)}</td>
                <td style="font-size:0.75rem;color:#888;">{last}</td>
                <td>
                    <form method="POST" action="/miniapp/group/{group_id}/tasks/delete?token={token}"
                          onsubmit="return confirm('Удалить?');">
                        <input type="hidden" name="task_id" value="{t.id}">
                        <button type="submit" class="btn-delete">✕</button>
                    </form>
                </td>
            </tr>
            """
        tasks_table = f"""
        <table class="source-table">
            <thead><tr><th>Тип</th><th>Источник</th><th>Расписание</th><th>Последний</th><th></th></tr></thead>
            <tbody>{tasks_rows}</tbody>
        </table>
        """
    else:
        tasks_table = '<p class="source-empty">Нет автозадач. Добавьте, чтобы бот сам писал контент по расписанию.</p>'

    tasks_html = f"""
    <div class="card">
        <div class="card-title">📋 Контент-задачи</div>
        {tasks_table}
        <form method="POST" action="/miniapp/group/{group_id}/tasks/add?token={token}" class="source-add">
            <div class="form-group">
                <label>Тип</label>
                <select name="task_type" style="width:120px;">
                    <option value="patch_notes">Патч-ноты</option>
                    <option value="article">Статья</option>
                    <option value="digest">Дайджест</option>
                </select>
            </div>
            <div class="form-group" style="flex:1;">
                <label>Источник (URL)</label>
                <input type="text" name="source_url" placeholder="https://github.com/user/repo">
            </div>
            <div class="form-group">
                <label>Расписание (cron)</label>
                <input type="text" name="schedule_cron" placeholder="0 18 * * 5" style="width:120px;" required>
            </div>
            <button type="submit" class="btn btn-sm">+</button>
        </form>
        <p class="hint">Cron: мин час день мес день_недели. Пример: 0 18 * * 5 = пятница 18:00 UTC</p>
    </div>
    """

    # AI refresh button
    ai_desc = await get_setting(group_id, "ai_group_description", "")
    ai_status = f'<span style="color:#2e7d32;">Настроен: {escape(ai_desc[:80])}...</span>' if ai_desc else '<span style="color:#d32f2f;">Не настроен — нажмите кнопку ниже</span>'

    ai_refresh_html = f"""
    <div class="card">
        <div class="card-title">🔄 ИИ-профиль</div>
        <p style="font-size:0.85rem;margin-bottom:10px;">{ai_status}</p>
        <p style="font-size:0.78rem;color:#888;margin-bottom:12px;">
            Бот сканирует группу (описание, посты) и настраивает свою личность,
            правила модерации и темы контента автоматически.
        </p>
        <form method="POST" action="/miniapp/group/{group_id}/ai-refresh?token={token}">
            <button type="submit" class="btn">Обновить ИИ-профиль</button>
        </form>
    </div>
    """

    # Widget install section
    widget_enabled = (await get_setting(group_id, "widget_enabled", "false")).lower() == "true"
    widget_status = '<span style="color:#2e7d32;">✓ Включён</span>' if widget_enabled else '<span style="color:#888;">Выключен</span>'

    widget_html = f"""
    <div class="card">
        <div class="card-title">🏆 Виджет-лидерборд</div>
        <p style="font-size:0.85rem;margin-bottom:6px;">Статус: {widget_status}</p>
        <p style="font-size:0.78rem;color:#888;margin-bottom:12px;">
            Виджет показывает таблицу топ-участников прямо на странице группы.
            Участники получают XP за сообщения, лайки и репосты.
        </p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
            <button class="btn" onclick="installWidget()">Установить виджет</button>
            <button class="btn" style="background:#4caf50;" onclick="refreshWidget()">Обновить данные</button>
        </div>
        <p id="widget-status" style="font-size:0.8rem;color:#888;margin-top:8px;"></p>
    </div>
    <script>
    function installWidget() {{
        var statusEl = document.getElementById('widget-status');
        statusEl.textContent = 'Подготовка виджета...';

        // 1. Get widget code from server
        fetch('/miniapp/group/{group_id}/widget/code?token={token}')
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{
                if (data.error) {{
                    statusEl.textContent = 'Ошибка: ' + data.error;
                    return;
                }}
                // 2. Show VK widget preview dialog
                statusEl.textContent = 'Открытие диалога VK...';
                return vkBridge.send('VKWebAppShowCommunityWidgetPreviewBox', {{
                    group_id: {group_id},
                    type: 'table',
                    code: data.code
                }});
            }})
            .then(function(result) {{
                if (result) {{
                    statusEl.innerHTML = '<span style="color:#2e7d32;">✓ Виджет установлен!</span>';
                    // Enable widget in settings
                    var fd = new FormData();
                    fd.append('key', 'widget_enabled');
                    fd.append('value', 'true');
                    fetch('/miniapp/group/{group_id}/settings?token={token}', {{
                        method: 'POST', body: fd,
                        headers: {{'X-Requested-With': 'XMLHttpRequest'}}
                    }});
                    showToast('Виджет установлен!');
                }}
            }})
            .catch(function(e) {{
                console.error('Widget install error:', e);
                if (e && e.error_data && e.error_data.error_code === 4) {{
                    statusEl.textContent = 'Отменено пользователем';
                }} else {{
                    statusEl.textContent = 'Ошибка: ' + (e.error_data ? e.error_data.error_reason : (e.message || 'неизвестная'));
                }}
            }});
    }}

    function refreshWidget() {{
        var statusEl = document.getElementById('widget-status');
        statusEl.textContent = 'Обновление...';
        fetch('/miniapp/group/{group_id}/widget/refresh?token={token}', {{method: 'POST'}})
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{
                if (data.ok) {{
                    statusEl.innerHTML = '<span style="color:#2e7d32;">✓ Виджет обновлён (' + data.users + ' участников)</span>';
                    showToast('Виджет обновлён!');
                }} else {{
                    statusEl.textContent = 'Ошибка: ' + (data.error || 'не удалось обновить');
                }}
            }})
            .catch(function() {{ statusEl.textContent = 'Ошибка сети'; }});
    }}
    </script>
    """

    name = escape(group.group_name or f"Группа {group_id}")
    nav = _bottom_nav("settings", token, group_id, True)

    content = f"""
    <div class="card" style="background: linear-gradient(135deg, #2688EB, #1f7ad8); color: white;">
        <h2 style="font-size: 1rem; font-weight: 700;">⚙️ {name}</h2>
        <p style="opacity: 0.85; font-size: 0.8rem;">ID: {group_id} · Настройки</p>
    </div>

    <div class="settings-tabs">
        <button class="settings-tab active" onclick="switchTab('ai')">🤖 ИИ</button>
        <button class="settings-tab" onclick="switchTab('content')">📝 Контент</button>
        <button class="settings-tab" onclick="switchTab('widget')">🏆 Виджет</button>
        <button class="settings-tab" onclick="switchTab('integrations')">🔗 Связи</button>
    </div>

    <div id="tab-ai" class="tab-content active">
        {tab_sections["ai"]}
        {ai_refresh_html}
    </div>

    <div id="tab-content" class="tab-content">
        {tab_sections["content"]}
        {sources_html}
        {tasks_html}
    </div>

    <div id="tab-widget" class="tab-content">
        {tab_sections["widget"]}
        {widget_html}
    </div>

    <div id="tab-integrations" class="tab-content">
        {tab_sections["integrations"]}
    </div>

    <script>
    function switchTab(name) {{
        document.querySelectorAll('.tab-content').forEach(function(el) {{ el.classList.remove('active'); }});
        document.querySelectorAll('.settings-tab').forEach(function(el) {{ el.classList.remove('active'); }});
        document.getElementById('tab-' + name).classList.add('active');
        event.target.classList.add('active');
    }}
    </script>
    {nav}
    """
    return HTMLResponse(_miniapp_html(name, content, token, body_class="has-nav"))


def _render_miniapp_control(group_id: int, setting: dict, current_value: str, token: str) -> str:
    """Render input control for Mini App (compact, with token)."""
    key = setting["key"]
    stype = setting.get("type", "text")
    action = f"/miniapp/group/{group_id}/settings?token={token}"

    if stype == "toggle":
        checked = "checked" if current_value.lower() == "true" else ""
        return f"""
        <form method="POST" action="{action}" onsubmit="return ajaxSubmit(this)" style="display:flex;align-items:center;gap:8px;">
            <input type="hidden" name="key" value="{key}">
            <input type="hidden" name="value" value="false">
            <label class="toggle">
                <input type="checkbox" name="value" value="true" {checked} onchange="ajaxSubmit(this.form)">
                <span class="toggle-slider"></span>
            </label>
        </form>
        """

    if stype == "select":
        opts_html = ""
        for val, label in setting.get("options", []):
            selected = "selected" if val == current_value else ""
            opts_html += f'<option value="{val}" {selected}>{label}</option>'
        return f"""
        <form method="POST" action="{action}" onsubmit="return ajaxSubmit(this)">
            <input type="hidden" name="key" value="{key}">
            <select name="value" onchange="ajaxSubmit(this.form)">{opts_html}</select>
        </form>
        """

    if stype == "textarea":
        placeholder = setting.get("placeholder", "")
        return f"""
        <form method="POST" action="{action}" onsubmit="return ajaxSubmit(this)">
            <input type="hidden" name="key" value="{key}">
            <textarea name="value" placeholder="{escape(placeholder)}"
                      onfocus="this.nextElementSibling.classList.add('show')">{escape(current_value)}</textarea>
            <button type="submit" class="save-btn">Сохранить</button>
        </form>
        """

    placeholder = setting.get("placeholder", "")
    return f"""
    <form method="POST" action="{action}" onsubmit="return ajaxSubmit(this)">
        <input type="hidden" name="key" value="{key}">
        <input type="text" name="value" value="{escape(current_value, quote=True)}" placeholder="{escape(placeholder, quote=True)}"
               onfocus="this.nextElementSibling.classList.add('show')">
        <button type="submit" class="save-btn">Сохранить</button>
    </form>
    """


# ─── Actions ──────────────────────────────────────────────────────────────────

@router.post("/miniapp/group/{group_id}/settings")
async def miniapp_update_setting(request: Request, group_id: int):
    auth = _get_auth(request)
    if not auth:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JSONResponse({"error": "Сессия истекла"}, status_code=401)
        return _error_page("Сессия истекла")

    token = request.query_params.get("token", "")
    group = await get_group(group_id)
    if not group or group.admin_vk_id != auth["uid"]:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JSONResponse({"error": "Нет доступа"}, status_code=403)
        return _error_page("Нет доступа")

    form = await request.form()
    key = str(form.get("key", "")).strip()
    values = form.getlist("value")
    value = str(values[-1]).strip() if values else ""

    if key:
        await set_setting(group_id, key, value)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JSONResponse({"ok": True})

    return RedirectResponse(f"/miniapp/group/{group_id}?token={token}&msg=saved", status_code=303)


@router.post("/miniapp/group/{group_id}/sources/add")
async def miniapp_add_source(request: Request, group_id: int):
    auth = _get_auth(request)
    if not auth:
        return _error_page("Сессия истекла")

    token = request.query_params.get("token", "")
    group = await get_group(group_id)
    if not group or group.admin_vk_id != auth["uid"]:
        return _error_page("Нет доступа")

    form = await request.form()
    source_type = str(form.get("source_type", "rss")).strip()
    source_url = str(form.get("source_url", "")).strip()
    filter_keywords = str(form.get("filter_keywords", "")).strip()

    if source_url:
        await add_content_source(group_id, source_type, source_url, filter_keywords)

    return RedirectResponse(f"/miniapp/group/{group_id}?token={token}&msg=saved", status_code=303)


@router.post("/miniapp/group/{group_id}/tasks/add")
async def miniapp_add_task(request: Request, group_id: int):
    auth = _get_auth(request)
    if not auth:
        return _error_page("Сессия истекла")

    token = request.query_params.get("token", "")
    group = await get_group(group_id)
    if not group or group.admin_vk_id != auth["uid"]:
        return _error_page("Нет доступа")

    form = await request.form()
    task_type = str(form.get("task_type", "article")).strip()
    source_url = str(form.get("source_url", "")).strip()
    schedule_cron = str(form.get("schedule_cron", "")).strip()

    if schedule_cron:
        try:
            from croniter import croniter
            croniter(schedule_cron)
        except Exception:
            return _error_page(f"Неверный cron: {escape(schedule_cron)}")

        name = f"{task_type}_{source_url.split('/')[-1] if source_url else 'manual'}"
        await create_content_task(
            group_id=group_id, name=name, task_type=task_type,
            schedule_cron=schedule_cron, source_url=source_url,
        )

    return RedirectResponse(f"/miniapp/group/{group_id}?token={token}&msg=saved", status_code=303)


@router.post("/miniapp/group/{group_id}/tasks/delete")
async def miniapp_delete_task(request: Request, group_id: int):
    auth = _get_auth(request)
    if not auth:
        return _error_page("Сессия истекла")

    token = request.query_params.get("token", "")
    group = await get_group(group_id)
    if not group or group.admin_vk_id != auth["uid"]:
        return _error_page("Нет доступа")

    form = await request.form()
    task_id = int(form.get("task_id", 0))
    if task_id:
        await delete_content_task(task_id)

    return RedirectResponse(f"/miniapp/group/{group_id}?token={token}&msg=saved", status_code=303)


@router.post("/miniapp/group/{group_id}/ai-refresh")
async def miniapp_ai_refresh(request: Request, group_id: int):
    auth = _get_auth(request)
    if not auth:
        return _error_page("Сессия истекла")

    token = request.query_params.get("token", "")
    group = await get_group(group_id)
    if not group or group.admin_vk_id != auth["uid"]:
        return _error_page("Нет доступа")

    from core.crypto import decrypt_token
    from core.group_setup import setup_group_ai

    try:
        vk_token = decrypt_token(group.access_token)
        await setup_group_ai(group_id, vk_token)
    except Exception as e:
        logger.error(f"AI refresh failed for group {group_id}: {e}")

    return RedirectResponse(f"/miniapp/group/{group_id}?token={token}&msg=saved", status_code=303)


@router.post("/miniapp/group/{group_id}/sources/delete")
async def miniapp_delete_source(request: Request, group_id: int):
    auth = _get_auth(request)
    if not auth:
        return _error_page("Сессия истекла")

    token = request.query_params.get("token", "")
    group = await get_group(group_id)
    if not group or group.admin_vk_id != auth["uid"]:
        return _error_page("Нет доступа")

    form = await request.form()
    source_id = int(form.get("source_id", 0))
    if source_id:
        await delete_content_source(source_id)

    return RedirectResponse(f"/miniapp/group/{group_id}?token={token}&msg=saved", status_code=303)


# ─── Widget API ────────────────────────────────────────────────────────────

@router.get("/miniapp/group/{group_id}/widget/code")
async def miniapp_widget_code(request: Request, group_id: int):
    """Return VKScript code for the widget preview dialog."""
    auth = _get_auth(request)
    if not auth:
        return JSONResponse({"error": "Сессия истекла"}, status_code=401)

    group = await get_group(group_id)
    if not group or group.admin_vk_id != auth["uid"]:
        return JSONResponse({"error": "Нет доступа"}, status_code=403)

    from core.widgets import _build_table_widget_code, _resolve_user_names
    from core.crypto import decrypt_token
    from database.service import get_top_users
    from vkbottle import API

    widget_count = int(await get_setting(group_id, "widget_top_count", "10"))
    widget_sort = await get_setting(group_id, "widget_sort_by", "xp")
    top = await get_top_users(group_id, order_by=widget_sort, limit=widget_count)

    if not top:
        # Demo widget if no data yet
        import json
        demo = {
            "title": "🏆 Топ участников",
            "head": [{"text": "#"}, {"text": "Участник"}, {"text": "Уровень"}, {"text": "XP"}],
            "body": [
                [{"text": "1"}, {"text": "Пока нет данных"}, {"text": "1"}, {"text": "0"}],
            ],
        }
        return JSONResponse({"code": f"return {json.dumps(demo, ensure_ascii=False)};"})

    try:
        token = decrypt_token(group.access_token)
        api = API(token=token)
        vk_ids = [u.vk_id for u in top]
        names = await _resolve_user_names(api, vk_ids)
    except Exception:
        names = {}

    rows = []
    for u in top:
        rows.append({
            "vk_id": u.vk_id,
            "name": names.get(u.vk_id, f"id{u.vk_id}"),
            "level": u.level,
            "xp": u.xp,
        })

    code = _build_table_widget_code(rows)
    return JSONResponse({"code": code})


@router.post("/miniapp/group/{group_id}/widget/refresh")
async def miniapp_widget_refresh(request: Request, group_id: int):
    """Force-refresh the widget data."""
    auth = _get_auth(request)
    if not auth:
        return JSONResponse({"error": "Сессия истекла"}, status_code=401)

    group = await get_group(group_id)
    if not group or group.admin_vk_id != auth["uid"]:
        return JSONResponse({"error": "Нет доступа"}, status_code=403)

    await set_setting(group_id, "widget_enabled", "true")

    from core.widgets import update_widget_for_group
    success = await update_widget_for_group(group_id)

    if success:
        from database.service import get_top_users
        widget_count = int(await get_setting(group_id, "widget_top_count", "10"))
        top = await get_top_users(group_id, limit=widget_count)
        return JSONResponse({"ok": True, "users": len(top)})

    return JSONResponse({"ok": False, "error": "Нет данных об участниках или ошибка VK API"})
