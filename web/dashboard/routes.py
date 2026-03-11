"""Admin dashboard — web panel for managing connected groups."""

import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.config import settings
from database.service import (
    get_all_active_groups, get_group, get_setting, set_setting,
    deactivate_group, DEFAULT_SETTINGS,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _base_html(title: str, content: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — VKAdmin</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }}
    .container {{ max-width: 800px; margin: 0 auto; padding: 20px; }}
    .header {{ background: #1976d2; color: white; padding: 20px; margin-bottom: 20px; border-radius: 8px; }}
    .header h1 {{ font-size: 1.5rem; }}
    .header p {{ opacity: 0.8; margin-top: 5px; }}
    .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 15px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    .card h3 {{ margin-bottom: 10px; }}
    .btn {{ display: inline-block; padding: 10px 20px; background: #1976d2; color: white; text-decoration: none; border-radius: 6px; border: none; cursor: pointer; font-size: 1rem; }}
    .btn:hover {{ background: #1565c0; }}
    .btn-danger {{ background: #d32f2f; }}
    .btn-danger:hover {{ background: #c62828; }}
    .btn-sm {{ padding: 6px 14px; font-size: 0.9rem; }}
    .setting-row {{ display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #eee; }}
    .setting-row:last-child {{ border-bottom: none; }}
    .setting-key {{ font-weight: 600; }}
    .setting-value {{ color: #666; max-width: 400px; word-break: break-all; }}
    input[type="text"], textarea, select {{ width: 100%; padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 0.95rem; }}
    textarea {{ resize: vertical; min-height: 80px; }}
    form {{ margin-top: 10px; }}
    .form-group {{ margin-bottom: 12px; }}
    .form-group label {{ display: block; font-weight: 600; margin-bottom: 4px; }}
    .status {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.85rem; }}
    .status-active {{ background: #e8f5e9; color: #2e7d32; }}
    .back {{ color: #1976d2; text-decoration: none; display: inline-block; margin-bottom: 15px; }}
    .group-actions {{ display: flex; gap: 10px; margin-top: 10px; align-items: center; }}
    .flash {{ padding: 12px 16px; border-radius: 6px; margin-bottom: 15px; }}
    .flash-success {{ background: #e8f5e9; color: #2e7d32; border: 1px solid #c8e6c9; }}
</style></head>
<body><div class="container">{content}</div></body></html>"""


@router.get("/dashboard")
async def dashboard_home(request: Request):
    """Main dashboard page — list all connected groups."""
    groups = await get_all_active_groups()

    if not groups:
        content = f"""
        <div class="header">
            <h1>VKAdmin — Панель управления</h1>
            <p>AI-администратор для групп ВКонтакте</p>
        </div>
        <div class="card">
            <h3>Нет подключённых групп</h3>
            <p style="margin-bottom: 15px;">Подключите свою первую группу, чтобы начать.</p>
            <form action="/api/vk/oauth" method="GET" style="display:flex; gap:10px; align-items:end;">
                <div class="form-group" style="flex:1; margin:0;">
                    <label>ID группы ВКонтакте</label>
                    <input type="text" name="group_ids" placeholder="например 236517033" required>
                </div>
                <button type="submit" class="btn">Подключить</button>
            </form>
            <p style="margin-top:10px; font-size:0.85rem; color:#999;">ID группы можно найти в адресной строке: vk.com/club<b>123456</b> или в настройках группы</p>
        </div>
        """
        return HTMLResponse(_base_html("Панель управления", content))

    groups_html = ""
    for g in groups:
        groups_html += f"""
        <div class="card">
            <h3>{g.group_name or f'Group {g.group_id}'}</h3>
            <p>ID: {g.group_id} <span class="status status-active">Активна</span></p>
            <div class="group-actions">
                <a href="/dashboard/group/{g.group_id}" class="btn btn-sm">Настройки</a>
                <form method="POST" action="/dashboard/group/{g.group_id}/disconnect"
                      onsubmit="return confirm('Отключить группу {g.group_id}? Бот перестанет обрабатывать события.');">
                    <button type="submit" class="btn btn-danger btn-sm">Отключить</button>
                </form>
            </div>
        </div>
        """

    content = f"""
    <div class="header">
        <h1>VKAdmin — Панель управления</h1>
        <p>AI-администратор для групп ВКонтакте</p>
    </div>
    {groups_html}
    <div class="card">
        <h3>Подключить ещё группу</h3>
        <form action="/api/vk/oauth" method="GET" style="display:flex; gap:10px; align-items:end; margin-top:10px;">
            <div class="form-group" style="flex:1; margin:0;">
                <label>ID группы ВКонтакте</label>
                <input type="text" name="group_ids" placeholder="например 236517033" required>
            </div>
            <button type="submit" class="btn">Подключить</button>
        </form>
        <p style="margin-top:10px; font-size:0.85rem; color:#999;">ID группы: vk.com/club<b>123456</b> → ID = 123456</p>
    </div>
    """
    return HTMLResponse(_base_html("Панель управления", content))


@router.get("/dashboard/group/{group_id}")
async def group_settings_page(request: Request, group_id: int):
    """Settings page for a specific group."""
    group = await get_group(group_id)
    if not group:
        return HTMLResponse(_base_html("Ошибка", '<div class="card"><h3>Группа не найдена</h3></div>'), status_code=404)

    # Flash message from query param
    flash = ""
    msg = request.query_params.get("msg", "")
    if msg == "saved":
        flash = '<div class="flash flash-success">Настройка сохранена!</div>'

    settings_html = ""
    for key, (default_val, description) in DEFAULT_SETTINGS.items():
        value = await get_setting(group_id, key, default_val)
        settings_html += f"""
        <div class="setting-row">
            <div>
                <div class="setting-key">{key}</div>
                <div style="font-size: 0.85rem; color: #999;">{description}</div>
            </div>
            <div class="setting-value">{value}</div>
        </div>
        """

    # Build select options from DEFAULT_SETTINGS keys
    options_html = "".join(
        f'<option value="{key}">{key}</option>' for key in DEFAULT_SETTINGS.keys()
    )

    content = f"""
    <a href="/dashboard" class="back">&larr; Назад</a>
    <div class="header">
        <h1>{group.group_name or f'Group {group_id}'}</h1>
        <p>ID: {group_id}</p>
    </div>
    {flash}
    <div class="card">
        <h3>Текущие настройки</h3>
        {settings_html}
    </div>
    <div class="card">
        <h3>Изменить настройку</h3>
        <form method="POST" action="/dashboard/group/{group_id}/settings">
            <div class="form-group">
                <label>Ключ</label>
                <select name="key" required>
                    <option value="" disabled selected>Выберите настройку...</option>
                    {options_html}
                </select>
            </div>
            <div class="form-group">
                <label>Значение</label>
                <textarea name="value" placeholder="Новое значение..." required></textarea>
            </div>
            <button type="submit" class="btn">Сохранить</button>
        </form>
    </div>
    """
    return HTMLResponse(_base_html(group.group_name or f"Group {group_id}", content))


@router.post("/dashboard/group/{group_id}/settings")
async def update_group_setting(request: Request, group_id: int):
    """Update a setting for a group via dashboard form."""
    form = await request.form()
    key = str(form.get("key", "")).strip()
    value = str(form.get("value", "")).strip()

    if key and value:
        await set_setting(group_id, key, value)

    return RedirectResponse(f"/dashboard/group/{group_id}?msg=saved", status_code=303)


@router.post("/dashboard/group/{group_id}/disconnect")
async def disconnect_group(request: Request, group_id: int):
    """Deactivate (soft-delete) a group."""
    await deactivate_group(group_id)
    return RedirectResponse("/dashboard", status_code=303)
