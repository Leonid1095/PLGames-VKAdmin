"""VK Mini App — admin panel inside VK iframe."""

import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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

def _miniapp_html(title: str, content: str, token: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="https://unpkg.com/@vkontakte/vk-bridge/dist/browser.min.js"></script>
<script>vkBridge.send("VKWebAppInit", {{}});</script>
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
</style></head>
<body>{content}</body></html>"""


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

    # If opened from a specific group — go directly to its settings
    if vk_group_id:
        group = await get_group(vk_group_id)
        if group and group.admin_vk_id == vk_user_id:
            return RedirectResponse(f"/miniapp/group/{vk_group_id}?token={token}", status_code=303)

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
        name = g.group_name or f"Группа {g.group_id}"
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

    flash = ""
    msg = request.query_params.get("msg", "")
    if msg == "saved":
        flash = '<div class="flash flash-success">✓ Сохранено</div>'

    sections_html = ""
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

        sections_html += f"""
        <div class="card">
            <div class="card-title">{section['icon']} {section['title']}</div>
            {items_html}
        </div>
        """

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
                <td><span class="source-url">{s.source_url}</span></td>
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
                <td><span class="source-url">{t.source_url or '—'}</span></td>
                <td style="font-size:0.75rem;color:#888;">{t.schedule_cron}</td>
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
    ai_status = f'<span style="color:#2e7d32;">Настроен: {ai_desc[:80]}...</span>' if ai_desc else '<span style="color:#d32f2f;">Не настроен — нажмите кнопку ниже</span>'

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

    name = group.group_name or f"Группа {group_id}"
    back_html = f'<a href="/miniapp?token={token}" class="back">← Назад</a>'

    content = f"""
    {back_html}
    <div class="card" style="background: linear-gradient(135deg, #2688EB, #1f7ad8); color: white;">
        <h2 style="font-size: 1rem; font-weight: 700;">{name}</h2>
        <p style="opacity: 0.85; font-size: 0.8rem;">ID: {group_id} · <span class="badge badge-green" style="font-size:0.7rem;">Работает</span></p>
    </div>
    {flash}
    {sections_html}
    {sources_html}
    {tasks_html}
    {ai_refresh_html}
    """
    return HTMLResponse(_miniapp_html(name, content, token))


def _render_miniapp_control(group_id: int, setting: dict, current_value: str, token: str) -> str:
    """Render input control for Mini App (compact, with token)."""
    key = setting["key"]
    stype = setting.get("type", "text")
    action = f"/miniapp/group/{group_id}/settings?token={token}"

    if stype == "toggle":
        checked = "checked" if current_value.lower() == "true" else ""
        return f"""
        <form method="POST" action="{action}" style="display:flex;align-items:center;gap:8px;">
            <input type="hidden" name="key" value="{key}">
            <input type="hidden" name="value" value="false">
            <label class="toggle">
                <input type="checkbox" name="value" value="true" {checked} onchange="this.form.submit()">
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
        <form method="POST" action="{action}">
            <input type="hidden" name="key" value="{key}">
            <select name="value" onchange="this.form.submit()">{opts_html}</select>
        </form>
        """

    if stype == "textarea":
        placeholder = setting.get("placeholder", "")
        return f"""
        <form method="POST" action="{action}">
            <input type="hidden" name="key" value="{key}">
            <textarea name="value" placeholder="{placeholder}"
                      onfocus="this.nextElementSibling.classList.add('show')">{current_value}</textarea>
            <button type="submit" class="save-btn">Сохранить</button>
        </form>
        """

    placeholder = setting.get("placeholder", "")
    return f"""
    <form method="POST" action="{action}">
        <input type="hidden" name="key" value="{key}">
        <input type="text" name="value" value="{current_value}" placeholder="{placeholder}"
               onfocus="this.nextElementSibling.classList.add('show')">
        <button type="submit" class="save-btn">Сохранить</button>
    </form>
    """


# ─── Actions ──────────────────────────────────────────────────────────────────

@router.post("/miniapp/group/{group_id}/settings")
async def miniapp_update_setting(request: Request, group_id: int):
    auth = _get_auth(request)
    if not auth:
        return _error_page("Сессия истекла")

    token = request.query_params.get("token", "")
    group = await get_group(group_id)
    if not group or group.admin_vk_id != auth["uid"]:
        return _error_page("Нет доступа")

    form = await request.form()
    key = str(form.get("key", "")).strip()
    values = form.getlist("value")
    value = str(values[-1]).strip() if values else ""

    if key:
        await set_setting(group_id, key, value)

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
            return _error_page(f"Неверный cron: {schedule_cron}")

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
