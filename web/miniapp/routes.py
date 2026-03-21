"""VK Mini App — admin panel inside VK iframe."""

import json
import logging
from html import escape
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.config import settings
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
    reopen = ""
    if "истекла" in msg.lower() or "сессия" in msg.lower():
        reopen = """
        <div style="text-align:center;margin-top:12px;">
            <button class="btn" onclick="window.location.reload()">Обновить страницу</button>
        </div>
        """
    return HTMLResponse(_miniapp_html("Ошибка", f"""
        <div class="card" style="text-align:center;padding:24px;">
            <div style="font-size:2rem;margin-bottom:8px;">⚠️</div>
            <p style="color:#d32f2f;font-size:0.95rem;">{msg}</p>
            {reopen}
        </div>
    """, ""), status_code=403)


# ─── HTML template ────────────────────────────────────────────────────────────

def _miniapp_html(title: str, content: str, token: str, body_class: str = "") -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="https://unpkg.com/@vkontakte/vk-bridge/dist/browser.min.js"></script>
<script>
(function() {{
    var attempts = 0;
    function initBridge() {{
        attempts++;
        if (typeof vkBridge !== 'undefined') {{
            vkBridge.send('VKWebAppInit').catch(function() {{}});
        }} else if (attempts < 30) {{
            setTimeout(initBridge, 200);
        }}
    }}
    initBridge();
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

    # Achievements
    badges = []
    if stats.messages_count >= 1:
        badges.append(("💬", "Первое слово"))
    if stats.messages_count >= 50:
        badges.append(("🗣", "Болтун"))
    if stats.messages_count >= 200:
        badges.append(("📢", "Оратор"))
    if stats.level >= 5:
        badges.append(("⭐", "Уровень 5"))
    if stats.level >= 10:
        badges.append(("🌟", "Уровень 10"))
    if stats.level >= 25:
        badges.append(("💎", "Уровень 25"))
    if stats.reputation >= 10:
        badges.append(("👍", "Уважаемый"))
    if stats.reputation >= 50:
        badges.append(("🏅", "Авторитет"))
    if stats.xp >= 1000:
        badges.append(("🔥", "1000 XP"))
    if stats.xp >= 5000:
        badges.append(("🚀", "5000 XP"))
    if stats.is_vip:
        badges.append(("👑", "VIP"))
    if rank != "—" and rank <= 3:
        badges.append(("🏆", f"Топ-{rank}"))

    badges_html = ""
    if badges:
        items = "".join(f'<div style="display:inline-flex;align-items:center;gap:3px;background:#f5f5f5;padding:4px 10px;border-radius:12px;font-size:0.78rem;"><span style="font-size:1rem;">{icon}</span>{label}</div>' for icon, label in badges)
        badges_html = f'<div class="card"><div class="card-title" style="font-size:0.9rem;">🎖 Достижения ({len(badges)})</div><div style="display:flex;flex-wrap:wrap;gap:6px;">{items}</div></div>'

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

    {badges_html}

    <div class="card">
        <div class="card-title" style="font-size:0.9rem;">📊 Как получить XP</div>
        <div style="font-size:0.82rem;color:#666;line-height:1.8;">
            💬 Сообщения боту — 1-5 XP<br>
            👍 Лайки постов — 2 XP<br>
            🔁 Репосты — 5 XP<br>
            Предупреждений: {stats.warnings}/3
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


# ─── Admin pages ─────────────────────────────────────────────────────────────

def _admin_check(auth: dict | None, group) -> str | None:
    """Return error HTML or None if admin access is OK."""
    if not auth:
        return "Сессия истекла"
    if not group:
        return "Группа не найдена"
    if group.admin_vk_id != auth["uid"]:
        return "Нет доступа"
    return None


@router.get("/miniapp/admin/analytics")
async def miniapp_analytics(request: Request):
    """Analytics dashboard for admins."""
    auth = _get_auth(request)
    token = request.query_params.get("token", request.query_params.get("t", ""))
    group_id = int(request.query_params.get("gid", auth.get("gid", 0) if auth else 0))

    group = await get_group(group_id) if group_id else None
    err = _admin_check(auth, group)
    if err:
        return _error_page(err)

    from database.service import get_post_analytics, get_top_users, get_pending_suggestions

    posts = await get_post_analytics(group_id, limit=20)
    top = await get_top_users(group_id, order_by="xp", limit=5)
    pending = await get_pending_suggestions(group_id, limit=100)

    # Summary stats
    total_likes = sum(p.likes for p in posts)
    total_views = sum(p.views for p in posts)
    total_reposts = sum(p.reposts for p in posts)
    total_comments = sum(p.comments for p in posts)
    avg_likes = total_likes // len(posts) if posts else 0
    avg_views = total_views // len(posts) if posts else 0

    # Post rows
    posts_html = ""
    if posts:
        for p in posts:
            date = p.published_at.strftime("%d.%m %H:%M") if p.published_at else "—"
            posts_html += f"""
            <div class="lb-row">
                <div style="flex:1;">
                    <div style="font-size:0.85rem;font-weight:600;">Пост #{p.vk_post_id}</div>
                    <div style="font-size:0.72rem;color:#888;">{date}</div>
                </div>
                <div style="display:flex;gap:10px;font-size:0.8rem;color:#555;">
                    <span>👍 {p.likes}</span>
                    <span>🔁 {p.reposts}</span>
                    <span>💬 {p.comments}</span>
                    <span>👁 {p.views}</span>
                </div>
            </div>
            """
    else:
        posts_html = '<p style="text-align:center;color:#aaa;padding:20px;">Нет данных. Аналитика собирается каждые 6 часов.</p>'

    # Top users mini
    top_html = ""
    for i, u in enumerate(top, 1):
        top_html += f'<div style="display:flex;justify-content:space-between;padding:4px 0;font-size:0.85rem;"><span>{i}. id{u.vk_id}</span><span style="color:#2688EB;font-weight:600;">{u.xp} XP</span></div>'

    nav = _bottom_nav("settings", token, group_id, True)

    content = f"""
    <a href="/miniapp/group/{group_id}?token={token}" class="back">← Настройки</a>
    <div class="card" style="background:linear-gradient(135deg,#1976d2,#0d47a1);color:white;">
        <div class="card-title" style="color:white;">📊 Аналитика</div>
        <p style="opacity:0.85;font-size:0.8rem;">Последние {len(posts)} постов</p>
    </div>

    <div class="stat-grid">
        <div class="stat-item">
            <div class="stat-value">{total_likes}</div>
            <div class="stat-label">Лайков</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{total_views}</div>
            <div class="stat-label">Просмотров</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{total_reposts}</div>
            <div class="stat-label">Репостов</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{total_comments}</div>
            <div class="stat-label">Комментариев</div>
        </div>
    </div>

    <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <div class="card-title" style="margin:0;">Среднее на пост</div>
        </div>
        <div style="display:flex;gap:16px;font-size:0.9rem;">
            <span>👍 {avg_likes}</span>
            <span>👁 {avg_views}</span>
        </div>
    </div>

    <div class="card">
        <div class="card-title">📈 Посты</div>
        <div style="margin:0 -12px;">{posts_html}</div>
    </div>

    <div class="card">
        <div class="card-title">🏆 Топ-5 участников</div>
        {top_html if top_html else '<p style="color:#aaa;font-size:0.85rem;">Нет данных</p>'}
    </div>

    <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <span style="font-size:0.9rem;">📝 Предложений на модерации</span>
            <span style="font-size:1.2rem;font-weight:700;">{len(pending)}</span>
        </div>
        {'<a href="/miniapp/admin/suggestions?token=' + token + '&gid=' + str(group_id) + '" class="btn btn-sm" style="margin-top:8px;display:block;text-align:center;">Открыть предложку</a>' if pending else ''}
    </div>
    {nav}
    """
    return HTMLResponse(_miniapp_html("Аналитика", content, token, body_class="has-nav"))


@router.get("/miniapp/admin/create-post")
async def miniapp_create_post_page(request: Request):
    """Create post page — AI generation or manual text."""
    auth = _get_auth(request)
    token = request.query_params.get("token", request.query_params.get("t", ""))
    group_id = int(request.query_params.get("gid", auth.get("gid", 0) if auth else 0))

    group = await get_group(group_id) if group_id else None
    err = _admin_check(auth, group)
    if err:
        return _error_page(err)

    nav = _bottom_nav("settings", token, group_id, True)

    content = f"""
    <a href="/miniapp/group/{group_id}?token={token}" class="back">← Настройки</a>
    <div class="card">
        <div class="card-title">✏️ Создать пост</div>

        <div style="margin-bottom:16px;">
            <div class="filter-tabs">
                <button class="filter-tab active" onclick="showPostMode('ai')">🤖 AI-генерация</button>
                <button class="filter-tab" onclick="showPostMode('manual')">📝 Вручную</button>
                <button class="filter-tab" onclick="showPostMode('url')">🔗 Из URL</button>
            </div>
        </div>

        <div id="mode-ai" class="tab-content active">
            <label style="font-size:0.85rem;font-weight:500;">Тема поста</label>
            <input type="text" id="ai-topic" placeholder="Оставьте пустым для случайной темы" style="margin-bottom:10px;">
            <button class="btn" onclick="generatePost()" id="btn-generate" style="width:100%;">Сгенерировать</button>
        </div>

        <div id="mode-manual" class="tab-content">
            <label style="font-size:0.85rem;font-weight:500;">Текст поста</label>
            <textarea id="manual-text" rows="6" placeholder="Напишите текст поста..."></textarea>
        </div>

        <div id="mode-url" class="tab-content">
            <label style="font-size:0.85rem;font-weight:500;">URL источника</label>
            <input type="text" id="url-source" placeholder="https://..." style="margin-bottom:8px;">
            <label style="font-size:0.85rem;font-weight:500;">Инструкция (необязательно)</label>
            <input type="text" id="url-instruction" placeholder="Напиши краткий пересказ..." style="margin-bottom:10px;">
            <button class="btn" onclick="generateFromUrl()" id="btn-from-url" style="width:100%;">Написать статью</button>
        </div>
    </div>

    <div id="preview-card" class="card" style="display:none;">
        <div class="card-title">👁 Превью</div>
        <div id="preview-text" style="font-size:0.88rem;line-height:1.6;white-space:pre-wrap;max-height:300px;overflow-y:auto;"></div>
        <div style="display:flex;gap:8px;margin-top:12px;">
            <button class="btn" onclick="publishPost()" id="btn-publish" style="flex:1;">Опубликовать</button>
            <button class="btn" onclick="schedulePost()" style="flex:1;background:#7b1fa2;">Запланировать</button>
        </div>
        <div id="schedule-row" style="display:none;margin-top:8px;">
            <input type="datetime-local" id="schedule-time" style="width:100%;">
            <button class="btn btn-sm" onclick="confirmSchedule()" style="margin-top:6px;width:100%;">Подтвердить</button>
        </div>
    </div>

    <div id="post-status" style="text-align:center;padding:8px;font-size:0.85rem;color:#888;"></div>

    <script>
    var currentText = '';
    var currentMode = 'ai';

    function showPostMode(mode) {{
        currentMode = mode;
        document.querySelectorAll('#mode-ai,#mode-manual,#mode-url').forEach(function(el){{ el.classList.remove('active'); }});
        document.getElementById('mode-' + mode).classList.add('active');
        document.querySelectorAll('.filter-tab').forEach(function(el){{ el.classList.remove('active'); }});
        event.target.classList.add('active');
    }}

    function setStatus(msg) {{ document.getElementById('post-status').textContent = msg; }}
    function showPreview(text) {{
        currentText = text;
        document.getElementById('preview-text').textContent = text;
        document.getElementById('preview-card').style.display = 'block';
        document.getElementById('preview-card').scrollIntoView({{behavior:'smooth'}});
    }}

    function generatePost() {{
        var topic = document.getElementById('ai-topic').value;
        var btn = document.getElementById('btn-generate');
        btn.disabled = true; btn.textContent = 'Генерация...';
        setStatus('ИИ пишет пост...');
        fetch('/miniapp/admin/api/generate?token={token}&gid={group_id}&topic=' + encodeURIComponent(topic))
            .then(function(r){{ return r.json(); }})
            .then(function(data) {{
                btn.disabled = false; btn.textContent = 'Сгенерировать';
                if (data.text) {{ showPreview(data.text); setStatus(''); }}
                else {{ setStatus('Ошибка: ' + (data.error || 'не удалось')); }}
            }}).catch(function(){{ btn.disabled=false; btn.textContent='Сгенерировать'; setStatus('Ошибка сети'); }});
    }}

    function generateFromUrl() {{
        var url = document.getElementById('url-source').value;
        var instr = document.getElementById('url-instruction').value;
        if (!url) {{ setStatus('Введите URL'); return; }}
        var btn = document.getElementById('btn-from-url');
        btn.disabled = true; btn.textContent = 'Генерация...';
        setStatus('ИИ пишет статью...');
        fetch('/miniapp/admin/api/generate-from-url?token={token}&gid={group_id}&url=' + encodeURIComponent(url) + '&instruction=' + encodeURIComponent(instr))
            .then(function(r){{ return r.json(); }})
            .then(function(data) {{
                btn.disabled = false; btn.textContent = 'Написать статью';
                if (data.text) {{ showPreview(data.text); setStatus(''); }}
                else {{ setStatus('Ошибка: ' + (data.error || 'не удалось')); }}
            }}).catch(function(){{ btn.disabled=false; btn.textContent='Написать статью'; setStatus('Ошибка сети'); }});
    }}

    function publishPost() {{
        var text = currentMode === 'manual' ? document.getElementById('manual-text').value : currentText;
        if (!text) {{ setStatus('Нет текста'); return; }}
        var btn = document.getElementById('btn-publish');
        btn.disabled = true; btn.textContent = 'Публикация...';
        fetch('/miniapp/admin/api/publish?token={token}&gid={group_id}', {{
            method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{text: text}})
        }}).then(function(r){{ return r.json(); }})
        .then(function(data) {{
            btn.disabled=false; btn.textContent='Опубликовать';
            if (data.ok) {{ showToast('Опубликовано!'); setStatus('Пост #' + data.vk_post_id + ' на стене'); }}
            else {{ setStatus('Ошибка: ' + (data.error||'?')); }}
        }}).catch(function(){{ btn.disabled=false; btn.textContent='Опубликовать'; setStatus('Ошибка сети'); }});
    }}

    function schedulePost() {{
        document.getElementById('schedule-row').style.display = 'block';
    }}

    function confirmSchedule() {{
        var text = currentMode === 'manual' ? document.getElementById('manual-text').value : currentText;
        var dt = document.getElementById('schedule-time').value;
        if (!text || !dt) {{ setStatus('Введите текст и время'); return; }}
        fetch('/miniapp/admin/api/schedule?token={token}&gid={group_id}', {{
            method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{text: text, scheduled_at: dt}})
        }}).then(function(r){{ return r.json(); }})
        .then(function(data) {{
            if (data.ok) {{ showToast('Запланировано!'); setStatus('Пост запланирован на ' + dt); }}
            else {{ setStatus('Ошибка: ' + (data.error||'?')); }}
        }}).catch(function(){{ setStatus('Ошибка сети'); }});
    }}
    </script>
    {nav}
    """
    return HTMLResponse(_miniapp_html("Создать пост", content, token, body_class="has-nav"))


@router.get("/miniapp/admin/suggestions")
async def miniapp_suggestions(request: Request):
    """Suggestions review page."""
    auth = _get_auth(request)
    token = request.query_params.get("token", request.query_params.get("t", ""))
    group_id = int(request.query_params.get("gid", auth.get("gid", 0) if auth else 0))

    group = await get_group(group_id) if group_id else None
    err = _admin_check(auth, group)
    if err:
        return _error_page(err)

    from database.service import get_pending_suggestions

    pending = await get_pending_suggestions(group_id, limit=50)
    nav = _bottom_nav("settings", token, group_id, True)

    items_html = ""
    if not pending:
        items_html = '<p style="text-align:center;color:#aaa;padding:24px;">Нет предложений на модерации</p>'
    else:
        for s in pending:
            date = s.created_at.strftime("%d.%m %H:%M") if s.created_at else ""
            text_preview = escape(s.text[:200]) + ("..." if len(s.text) > 200 else "")
            items_html += f"""
            <div class="card" id="suggestion-{s.id}">
                <div style="display:flex;justify-content:space-between;margin-bottom:6px;">
                    <span style="font-size:0.8rem;color:#888;">от id{s.from_vk_id} · {date}</span>
                    <span style="font-size:0.8rem;color:#2688EB;">#{s.id}</span>
                </div>
                <div style="font-size:0.88rem;line-height:1.5;margin-bottom:10px;white-space:pre-wrap;">{text_preview}</div>
                <div style="display:flex;gap:8px;">
                    <button class="btn btn-sm" style="flex:1;" onclick="reviewSuggestion({s.id},'approve')">✓ Принять</button>
                    <button class="btn btn-sm btn-danger" style="flex:1;" onclick="reviewSuggestion({s.id},'reject')">✕ Отклонить</button>
                </div>
            </div>
            """

    content = f"""
    <a href="/miniapp/group/{group_id}?token={token}" class="back">← Настройки</a>
    <div class="card" style="background:linear-gradient(135deg,#ff9800,#f57c00);color:white;">
        <div class="card-title" style="color:white;">📝 Предложка</div>
        <p style="opacity:0.85;font-size:0.8rem;">{len(pending)} на модерации</p>
    </div>
    {items_html}

    <script>
    function reviewSuggestion(id, action) {{
        fetch('/miniapp/admin/api/review-suggestion?token={token}&gid={group_id}', {{
            method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{suggestion_id: id, action: action}})
        }}).then(function(r){{ return r.json(); }})
        .then(function(data) {{
            if (data.ok) {{
                showToast(action === 'approve' ? 'Опубликовано!' : 'Отклонено');
                document.getElementById('suggestion-'+id).style.display='none';
            }} else {{ showToast('Ошибка: ' + (data.error||'?')); }}
        }}).catch(function(){{ showToast('Ошибка сети'); }});
    }}
    </script>
    {nav}
    """
    return HTMLResponse(_miniapp_html("Предложка", content, token, body_class="has-nav"))


# ─── Admin API endpoints ──────────────────────────────────────────────────────

@router.get("/miniapp/admin/api/generate")
async def api_generate_post(request: Request):
    """Generate a post via AI."""
    auth = _get_auth(request)
    group_id = int(request.query_params.get("gid", 0))
    group = await get_group(group_id) if group_id else None
    err = _admin_check(auth, group)
    if err:
        return JSONResponse({"error": err}, status_code=403)

    topic = request.query_params.get("topic", "")
    from core.ai_brain import generate_post
    try:
        text = await generate_post(group_id=group_id, topic=topic)
        return JSONResponse({"text": text})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/miniapp/admin/api/generate-from-url")
async def api_generate_from_url(request: Request):
    """Generate article from URL."""
    auth = _get_auth(request)
    group_id = int(request.query_params.get("gid", 0))
    group = await get_group(group_id) if group_id else None
    err = _admin_check(auth, group)
    if err:
        return JSONResponse({"error": err}, status_code=403)

    url = request.query_params.get("url", "")
    instruction = request.query_params.get("instruction", "")
    if not url:
        return JSONResponse({"error": "URL не указан"}, status_code=400)

    from core.content_writer import write_from_url
    try:
        text = await write_from_url(group_id=group_id, url=url, instruction=instruction)
        return JSONResponse({"text": text})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/miniapp/admin/api/publish")
async def api_publish_post(request: Request):
    """Publish post to VK wall."""
    auth = _get_auth(request)
    group_id = int(request.query_params.get("gid", 0))
    group = await get_group(group_id) if group_id else None
    err = _admin_check(auth, group)
    if err:
        return JSONResponse({"error": err}, status_code=403)

    data = await request.json()
    text = data.get("text", "").strip()
    if not text:
        return JSONResponse({"error": "Пустой текст"}, status_code=400)

    from core.crypto import decrypt_token
    from core.telegram import send_to_telegram
    from vkbottle import API

    try:
        token = decrypt_token(group.access_token)
        api = API(token=token)
        result = await api.wall.post(owner_id=-group_id, message=text)
        vk_post_id = result.post_id if result else 0
        await send_to_telegram(group_id, text, vk_post_id)
        return JSONResponse({"ok": True, "vk_post_id": vk_post_id})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/miniapp/admin/api/schedule")
async def api_schedule_post(request: Request):
    """Schedule a post for later."""
    auth = _get_auth(request)
    group_id = int(request.query_params.get("gid", 0))
    group = await get_group(group_id) if group_id else None
    err = _admin_check(auth, group)
    if err:
        return JSONResponse({"error": err}, status_code=403)

    data = await request.json()
    text = data.get("text", "").strip()
    scheduled_at_str = data.get("scheduled_at", "")
    if not text or not scheduled_at_str:
        return JSONResponse({"error": "Укажите текст и время"}, status_code=400)

    from datetime import datetime, timezone
    from database.service import create_scheduled_post

    try:
        scheduled_at = datetime.fromisoformat(scheduled_at_str).replace(tzinfo=timezone.utc)
        post = await create_scheduled_post(group_id, text, scheduled_at, source="manual")
        return JSONResponse({"ok": True, "post_id": post.id})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/miniapp/admin/api/review-suggestion")
async def api_review_suggestion(request: Request):
    """Approve or reject a suggestion."""
    auth = _get_auth(request)
    group_id = int(request.query_params.get("gid", 0))
    group = await get_group(group_id) if group_id else None
    err = _admin_check(auth, group)
    if err:
        return JSONResponse({"error": err}, status_code=403)

    data = await request.json()
    suggestion_id = data.get("suggestion_id", 0)
    action = data.get("action", "")

    from database.service import get_suggestion, review_suggestion

    suggestion = await get_suggestion(suggestion_id)
    if not suggestion or suggestion.group_id != group_id:
        return JSONResponse({"error": "Не найдено"}, status_code=404)

    if action == "approve":
        from core.crypto import decrypt_token
        from core.telegram import send_to_telegram
        from vkbottle import API

        try:
            vk_token = decrypt_token(group.access_token)
            api = API(token=vk_token)
            result = await api.wall.post(owner_id=-group_id, message=suggestion.text)
            vk_post_id = result.post_id if result else 0
            await review_suggestion(suggestion_id, "published", auth["uid"])
            await send_to_telegram(group_id, suggestion.text, vk_post_id)
            return JSONResponse({"ok": True, "vk_post_id": vk_post_id})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    elif action == "reject":
        await review_suggestion(suggestion_id, "rejected", auth["uid"])
        return JSONResponse({"ok": True})
    else:
        return JSONResponse({"error": "Неизвестное действие"}, status_code=400)


# ─── Content calendar ─────────────────────────────────────────────────────────

@router.get("/miniapp/admin/calendar")
async def miniapp_calendar(request: Request):
    """Content calendar — scheduled posts view."""
    auth = _get_auth(request)
    token = request.query_params.get("token", request.query_params.get("t", ""))
    group_id = int(request.query_params.get("gid", auth.get("gid", 0) if auth else 0))

    group = await get_group(group_id) if group_id else None
    err = _admin_check(auth, group)
    if err:
        return _error_page(err)

    from datetime import datetime, timezone, timedelta
    from database.service import get_content_plan

    # Get posts for today and next 7 days
    now = datetime.now(timezone.utc)
    days_html = ""

    for day_offset in range(7):
        day = now + timedelta(days=day_offset)
        posts = await get_content_plan(group_id, day)

        day_label = "Сегодня" if day_offset == 0 else ("Завтра" if day_offset == 1 else day.strftime("%d.%m %a"))
        posts_count = len(posts)

        posts_items = ""
        if posts:
            for p in posts:
                time_str = p.scheduled_at.strftime("%H:%M") if p.scheduled_at else "—"
                status_cls = {"pending": "🕐", "published": "✅", "failed": "❌"}.get(p.status, "")
                source_label = {"manual": "вручную", "ai": "ИИ", "parsed": "парсинг", "suggested": "предложка"}.get(p.source.split(":")[0] if p.source else "", p.source or "")
                text_preview = escape(p.text[:80]) + ("..." if len(p.text) > 80 else "")
                posts_items += f"""
                <div style="padding:8px 0;border-bottom:1px solid #f0f0f0;">
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <span style="font-weight:600;font-size:0.85rem;">{status_cls} {time_str}</span>
                        <span style="font-size:0.72rem;color:#888;background:#f5f5f5;padding:2px 6px;border-radius:4px;">{source_label}</span>
                    </div>
                    <div style="font-size:0.8rem;color:#555;margin-top:3px;">{text_preview}</div>
                </div>
                """
        else:
            posts_items = '<div style="padding:12px;text-align:center;color:#bbb;font-size:0.82rem;">Нет постов</div>'

        dot_color = "#4caf50" if posts_count > 0 else "#e0e0e0"
        days_html += f"""
        <div class="card" style="padding:12px 16px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
                <div style="font-weight:600;font-size:0.9rem;">
                    <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{dot_color};margin-right:6px;"></span>
                    {day_label}
                </div>
                <span style="font-size:0.75rem;color:#888;">{posts_count} пост(ов)</span>
            </div>
            {posts_items}
        </div>
        """

    nav = _bottom_nav("settings", token, group_id, True)

    content = f"""
    <a href="/miniapp/group/{group_id}?token={token}" class="back">← Настройки</a>
    <div class="card" style="background:linear-gradient(135deg,#7b1fa2,#4a148c);color:white;">
        <div class="card-title" style="color:white;">📅 Контент-план</div>
        <p style="opacity:0.85;font-size:0.8rem;">Ближайшие 7 дней</p>
    </div>
    <a href="/miniapp/admin/create-post?token={token}&gid={group_id}" class="btn" style="display:block;text-align:center;margin-bottom:10px;">+ Создать пост</a>
    {days_html}
    {nav}
    """
    return HTMLResponse(_miniapp_html("Контент-план", content, token, body_class="has-nav"))


# ─── Newsletter ───────────────────────────────────────────────────────────────

@router.get("/miniapp/admin/newsletter")
async def miniapp_newsletter_page(request: Request):
    """Newsletter form page."""
    auth = _get_auth(request)
    token = request.query_params.get("token", request.query_params.get("t", ""))
    group_id = int(request.query_params.get("gid", auth.get("gid", 0) if auth else 0))

    group = await get_group(group_id) if group_id else None
    err = _admin_check(auth, group)
    if err:
        return _error_page(err)

    nav = _bottom_nav("settings", token, group_id, True)

    content = f"""
    <a href="/miniapp/group/{group_id}?token={token}" class="back">← Настройки</a>
    <div class="card" style="background:linear-gradient(135deg,#d32f2f,#b71c1c);color:white;">
        <div class="card-title" style="color:white;">📨 Рассылка</div>
        <p style="opacity:0.85;font-size:0.8rem;">Отправка сообщения всем участникам группы</p>
    </div>

    <div class="card">
        <label style="font-size:0.85rem;font-weight:600;display:block;margin-bottom:6px;">Текст рассылки</label>
        <textarea id="nl-text" rows="5" placeholder="Привет! У нас для вас отличные новости..."></textarea>

        <div style="margin-top:12px;">
            <label style="font-size:0.82rem;color:#888;">
                <input type="checkbox" id="nl-confirm" style="margin-right:6px;">
                Я понимаю, что сообщение будет отправлено всем участникам группы
            </label>
        </div>

        <button class="btn" id="nl-btn" onclick="sendNewsletter()" style="width:100%;margin-top:12px;" disabled>Отправить рассылку</button>
        <div id="nl-status" style="text-align:center;font-size:0.82rem;color:#888;margin-top:8px;"></div>
    </div>

    <div class="card">
        <div style="font-size:0.82rem;color:#888;">
            ⚠️ Рассылка отправляется в ЛС каждому участнику.<br>
            Это может занять время для больших групп.<br>
            VK может ограничить отправку если участников много.
        </div>
    </div>

    <script>
    document.getElementById('nl-confirm').addEventListener('change', function() {{
        document.getElementById('nl-btn').disabled = !this.checked;
    }});

    function sendNewsletter() {{
        var text = document.getElementById('nl-text').value.trim();
        if (!text) {{ document.getElementById('nl-status').textContent = 'Введите текст'; return; }}

        var btn = document.getElementById('nl-btn');
        btn.disabled = true; btn.textContent = 'Отправка...';
        document.getElementById('nl-status').textContent = 'Запуск рассылки...';

        fetch('/miniapp/admin/api/newsletter?token={token}&gid={group_id}', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{text: text}})
        }}).then(function(r) {{ return r.json(); }})
        .then(function(data) {{
            btn.textContent = 'Отправить рассылку';
            document.getElementById('nl-confirm').checked = false;
            btn.disabled = true;
            if (data.ok) {{
                document.getElementById('nl-status').innerHTML = '<span style="color:#2e7d32;">✓ Рассылка запущена! ' + data.total + ' получателей</span>';
                showToast('Рассылка запущена!');
            }} else {{
                document.getElementById('nl-status').textContent = 'Ошибка: ' + (data.error || '?');
            }}
        }}).catch(function() {{
            btn.disabled = false; btn.textContent = 'Отправить рассылку';
            document.getElementById('nl-status').textContent = 'Ошибка сети';
        }});
    }}
    </script>
    {nav}
    """
    return HTMLResponse(_miniapp_html("Рассылка", content, token, body_class="has-nav"))


@router.post("/miniapp/admin/api/newsletter")
async def api_send_newsletter(request: Request):
    """Start a newsletter broadcast."""
    auth = _get_auth(request)
    group_id = int(request.query_params.get("gid", 0))
    group = await get_group(group_id) if group_id else None
    err = _admin_check(auth, group)
    if err:
        return JSONResponse({"error": err}, status_code=403)

    data = await request.json()
    text = data.get("text", "").strip()
    if not text:
        return JSONResponse({"error": "Пустой текст"}, status_code=400)

    import asyncio
    from core.crypto import decrypt_token
    from vkbottle import API
    from database.service import create_newsletter, update_newsletter_progress

    try:
        vk_token = decrypt_token(group.access_token)
        api = API(token=vk_token)

        members_resp = await api.groups.get_members(group_id=group_id, count=0)
        total = members_resp.count if members_resp else 0
        if total == 0:
            return JSONResponse({"error": "Нет участников"}, status_code=400)

        nl = await create_newsletter(group_id, text, auth["uid"], total)

        # Send in background
        async def _send():
            sent = 0
            offset = 0
            batch = 200
            while offset < total:
                try:
                    resp = await api.groups.get_members(group_id=group_id, offset=offset, count=batch)
                    for member in (resp.items or []):
                        try:
                            await api.messages.send(user_id=member, message=text, random_id=0)
                            sent += 1
                        except Exception:
                            pass
                        await asyncio.sleep(0.05)
                except Exception:
                    pass
                offset += batch
                await update_newsletter_progress(nl.id, sent)
            await update_newsletter_progress(nl.id, sent, status="sent")

        asyncio.create_task(_send())
        return JSONResponse({"ok": True, "total": total, "newsletter_id": nl.id})

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── Onboarding ──────────────────────────────────────────────────────────────

@router.get("/miniapp/onboarding")
async def miniapp_onboarding(request: Request):
    """Onboarding checklist for new groups."""
    auth = _get_auth(request)
    token = request.query_params.get("token", request.query_params.get("t", ""))
    group_id = int(request.query_params.get("gid", auth.get("gid", 0) if auth else 0))

    group = await get_group(group_id) if group_id else None
    err = _admin_check(auth, group)
    if err:
        return _error_page(err)

    # Check what's configured
    ai_desc = await get_setting(group_id, "ai_group_description", "")
    sources = await get_content_sources(group_id)
    autopost = (await get_setting(group_id, "autopost_enabled", "false")).lower() == "true"
    widget = (await get_setting(group_id, "widget_enabled", "false")).lower() == "true"
    telegram = (await get_setting(group_id, "telegram_enabled", "false")).lower() == "true"

    steps = [
        ("ai", "🤖 Настроить ИИ-профиль", "Бот проанализирует группу и создаст персонализированный промпт", bool(ai_desc), f"/miniapp/group/{group_id}?token={token}"),
        ("sources", "📡 Добавить источники контента", "RSS, VK-группы или сайты для автоматического парсинга", len(sources) > 0, f"/miniapp/group/{group_id}?token={token}"),
        ("autopost", "📝 Включить автопостинг", "Бот будет сам писать и публиковать посты из источников", autopost, f"/miniapp/group/{group_id}?token={token}"),
        ("widget", "🏆 Установить виджет", "Таблица топ-участников на странице группы", widget, f"/miniapp/group/{group_id}?token={token}"),
        ("telegram", "📨 Подключить Telegram", "Автоматический кросс-постинг в Telegram-канал", telegram, f"/miniapp/group/{group_id}?token={token}"),
    ]

    done_count = sum(1 for _, _, _, done, _ in steps)
    total = len(steps)
    pct = int(done_count / total * 100)

    steps_html = ""
    for key, title, desc, done, link in steps:
        icon = "✅" if done else "⬜"
        opacity = "0.6" if done else "1"
        steps_html += f"""
        <a href="{link}" style="text-decoration:none;color:inherit;opacity:{opacity};">
            <div style="display:flex;align-items:flex-start;gap:10px;padding:12px 0;border-bottom:1px solid #f0f0f0;">
                <div style="font-size:1.2rem;line-height:1;padding-top:2px;">{icon}</div>
                <div>
                    <div style="font-weight:600;font-size:0.9rem;">{title}</div>
                    <div style="font-size:0.78rem;color:#888;">{desc}</div>
                </div>
            </div>
        </a>
        """

    nav = _bottom_nav("settings", token, group_id, True)

    content = f"""
    <div class="card" style="background:linear-gradient(135deg,#2688EB,#1565c0);color:white;text-align:center;">
        <div style="font-size:1.5rem;margin-bottom:4px;">🚀</div>
        <h2 style="font-size:1.1rem;font-weight:700;">Настройка бота</h2>
        <p style="opacity:0.85;font-size:0.8rem;">Выполните шаги ниже для полной настройки</p>
        <div style="margin-top:12px;">
            <div style="background:rgba(255,255,255,0.2);border-radius:8px;height:8px;overflow:hidden;">
                <div style="background:white;height:100%;width:{pct}%;border-radius:8px;transition:width 0.5s;"></div>
            </div>
            <div style="font-size:0.75rem;opacity:0.8;margin-top:4px;">{done_count} из {total} · {pct}%</div>
        </div>
    </div>

    <div class="card">
        {steps_html}
    </div>

    <a href="/miniapp/group/{group_id}?token={token}" class="btn" style="display:block;text-align:center;">
        Перейти к настройкам →
    </a>
    {nav}
    """
    return HTMLResponse(_miniapp_html("Настройка", content, token, body_class="has-nav"))


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

    # If opened from a specific group
    if vk_group_id:
        group = await get_group(vk_group_id)
        if group:
            # Admin opening for first time? Show onboarding
            if group.admin_vk_id == vk_user_id:
                ai_desc = await get_setting(vk_group_id, "ai_group_description", "")
                if not ai_desc:
                    return RedirectResponse(f"/miniapp/onboarding?token={token}&gid={vk_group_id}", status_code=303)
            # Regular flow — profile page
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

    # If only one group — redirect to profile
    if len(groups) == 1:
        g = groups[0]
        return RedirectResponse(f"/miniapp/profile?token={token}&gid={g.group_id}", status_code=303)

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
                <div style="display:flex;gap:6px;">
                    <a href="/miniapp/profile?token={token}&gid={g.group_id}" class="btn btn-sm">Открыть</a>
                    <a href="/miniapp/group/{g.group_id}?token={token}" class="btn btn-sm" style="background:#455a64;">⚙️</a>
                </div>
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
        statusEl.textContent = 'Запрос прав на виджет...';

        // 1. Get widget token with app_widget scope via VK Bridge
        vkBridge.send('VKWebAppGetCommunityToken', {{
            app_id: {settings.VK_MINIAPP_ID or 0},
            group_id: {group_id},
            scope: 'app_widget'
        }})
        .then(function(tokenResult) {{
            var widgetToken = tokenResult.access_token;
            statusEl.textContent = 'Сохранение токена...';

            // 2. Save widget token on server
            var fd = new FormData();
            fd.append('widget_token', widgetToken);
            return fetch('/miniapp/group/{group_id}/widget/save-token?token={token}', {{
                method: 'POST', body: fd,
                headers: {{'X-Requested-With': 'XMLHttpRequest'}}
            }}).then(function(r) {{ return r.json(); }});
        }})
        .then(function(saveResult) {{
            statusEl.textContent = 'Подготовка виджета...';

            // 3. Get widget code from server
            return fetch('/miniapp/group/{group_id}/widget/code?token={token}')
                .then(function(r) {{ return r.json(); }});
        }})
        .then(function(data) {{
            if (data.error) {{
                statusEl.textContent = 'Ошибка: ' + data.error;
                return;
            }}
            // 4. Show VK widget preview dialog
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
                    statusEl.innerHTML = '<span style="color:#2e7d32;">✓ ' + (data.message || 'Виджет обновлён') + '</span>';
                    showToast('Виджет обновлён!');
                }} else {{
                    statusEl.innerHTML = '<span style="color:#c62828;">' + (data.error || 'Не удалось обновить') + '</span>';
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

    <div style="display:flex;gap:6px;margin-bottom:8px;overflow-x:auto;">
        <a href="/miniapp/admin/create-post?token={token}&gid={group_id}" class="btn btn-sm" style="white-space:nowrap;">✏️ Пост</a>
        <a href="/miniapp/admin/calendar?token={token}&gid={group_id}" class="btn btn-sm" style="white-space:nowrap;background:#7b1fa2;">📅 План</a>
        <a href="/miniapp/admin/analytics?token={token}&gid={group_id}" class="btn btn-sm" style="white-space:nowrap;background:#0d47a1;">📊 Стата</a>
        <a href="/miniapp/admin/suggestions?token={token}&gid={group_id}" class="btn btn-sm" style="white-space:nowrap;background:#f57c00;">📝 Предложка</a>
        <a href="/miniapp/admin/newsletter?token={token}&gid={group_id}" class="btn btn-sm" style="white-space:nowrap;background:#d32f2f;">📨 Рассылка</a>
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

@router.post("/miniapp/group/{group_id}/widget/save-token")
async def miniapp_widget_save_token(request: Request, group_id: int):
    """Save the widget token (app_widget scope) obtained via VKWebAppGetCommunityToken."""
    auth = _get_auth(request)
    if not auth:
        return JSONResponse({"error": "Сессия истекла"}, status_code=401)

    group = await get_group(group_id)
    if not group or group.admin_vk_id != auth["uid"]:
        return JSONResponse({"error": "Нет доступа"}, status_code=403)

    form = await request.form()
    widget_token = str(form.get("widget_token", "")).strip()
    if not widget_token:
        return JSONResponse({"error": "Токен не передан"}, status_code=400)

    await set_setting(group_id, "widget_token", widget_token)
    logger.info(f"Widget token saved for group {group_id}")
    return JSONResponse({"ok": True})


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
            "messages": u.messages_count,
            "reputation": u.reputation,
        })

    code = _build_table_widget_code(rows, sort_by=widget_sort)
    return JSONResponse({"code": code})


@router.api_route("/miniapp/group/{group_id}/widget/refresh", methods=["GET", "POST"])
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
    success, message = await update_widget_for_group(group_id)

    if success:
        return JSONResponse({"ok": True, "message": message})

    return JSONResponse({"ok": False, "error": message})
