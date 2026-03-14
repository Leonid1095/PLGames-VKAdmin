"""Admin dashboard — web panel for managing connected groups."""

import logging
from html import escape
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.config import settings as app_settings
from core.auth import (
    is_authenticated, set_auth_cookie, clear_auth_cookie, get_dashboard_password,
    get_csrf_token, set_csrf_cookie, verify_csrf_token,
)
from database.service import (
    get_all_active_groups, get_group, get_setting, set_setting,
    deactivate_group, get_content_sources, add_content_source,
    delete_content_source, get_content_tasks, create_content_task,
    delete_content_task,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _require_auth(request: Request):
    """Returns RedirectResponse to login if not authenticated, else None."""
    if not is_authenticated(request):
        return RedirectResponse("/dashboard/login", status_code=303)
    return None


def _csrf_field(request: Request) -> str:
    """Generate a hidden CSRF input field for forms."""
    token = get_csrf_token(request)
    return f'<input type="hidden" name="_csrf" value="{token}">'


# ─── Settings schema: grouped, with human-readable labels and input types ────

SETTINGS_SCHEMA = [
    {
        "title": "Искусственный интеллект",
        "icon": "🤖",
        "settings": [
            {
                "key": "active_model",
                "label": "Модель ИИ",
                "description": "Какая модель отвечает на сообщения и генерирует контент",
                "type": "select",
                "options": [
                    ("plgames-ai", "PLGames AI (свой сервер)"),
                    ("openai/gpt-4o-mini", "GPT-4o Mini (через OpenRouter)"),
                    ("openai/gpt-4o", "GPT-4o (через OpenRouter)"),
                    ("anthropic/claude-3.5-sonnet", "Claude 3.5 Sonnet (через OpenRouter)"),
                    ("google/gemini-pro-1.5", "Gemini Pro 1.5 (через OpenRouter)"),
                ],
                "default": "plgames-ai",
            },
            {
                "key": "system_prompt",
                "label": "Характер бота (ручной)",
                "description": "Ручная настройка — переопределяет автоматический профиль",
                "type": "textarea",
                "default": "",
                "placeholder": "Оставьте пустым для автоматического профиля (рекомендуется)",
            },
        ],
    },
    {
        "title": "ИИ-профиль группы",
        "icon": "🧠",
        "settings": [
            {
                "key": "ai_group_description",
                "label": "Описание группы",
                "description": "Автоматически сгенерировано. Бот использует это для понимания контекста",
                "type": "textarea",
                "default": "",
                "placeholder": "Нажмите 'Обновить ИИ' внизу страницы для автогенерации",
            },
            {
                "key": "ai_system_prompt",
                "label": "ИИ-промпт (автоматический)",
                "description": "Как бот общается — сгенерировано на основе анализа группы",
                "type": "textarea",
                "default": "",
                "placeholder": "Генерируется автоматически при подключении группы",
            },
            {
                "key": "ai_moderation_rules",
                "label": "Правила модерации ИИ",
                "description": "Контекстные правила модерации для этой группы",
                "type": "textarea",
                "default": "",
                "placeholder": "Генерируется автоматически",
            },
            {
                "key": "ai_content_topics",
                "label": "Темы для контента",
                "description": "ИИ использует эти темы при генерации постов и подборе контента",
                "type": "textarea",
                "default": "",
                "placeholder": "Генерируется автоматически",
            },
            {
                "key": "ai_tone",
                "label": "Стиль общения",
                "description": "Тон, в котором бот пишет посты и общается",
                "type": "select",
                "options": [
                    ("friendly", "Дружелюбный"),
                    ("casual", "Неформальный"),
                    ("gaming", "Геймерский"),
                    ("professional", "Профессиональный"),
                    ("formal", "Деловой"),
                ],
                "default": "friendly",
            },
        ],
    },
    {
        "title": "Модерация",
        "icon": "🛡",
        "settings": [
            {
                "key": "moderation_aggressiveness",
                "label": "Жёсткость модерации",
                "description": "Насколько строго бот удаляет комментарии",
                "type": "select",
                "options": [
                    ("low", "Мягкая — только мат и угрозы"),
                    ("medium", "Средняя — мат, оскорбления, спам"),
                    ("high", "Жёсткая — любой негатив и реклама"),
                ],
                "default": "medium",
            },
            {
                "key": "reply_to_comments",
                "label": "Отвечать на комментарии",
                "description": "Бот будет автоматически отвечать на комментарии под постами",
                "type": "toggle",
                "default": "true",
            },
        ],
    },
    {
        "title": "Автопостинг",
        "icon": "📝",
        "settings": [
            {
                "key": "autopost_enabled",
                "label": "Автопостинг включён",
                "description": "Бот берёт контент из источников (RSS, сайты) и публикует. Добавьте источники ниже!",
                "type": "toggle",
                "default": "false",
            },
            {
                "key": "autopost_interval_hours",
                "label": "Интервал (часы)",
                "description": "Как часто публиковать автопосты",
                "type": "select",
                "options": [
                    ("2", "Каждые 2 часа"),
                    ("4", "Каждые 4 часа"),
                    ("6", "Каждые 6 часов"),
                    ("12", "Каждые 12 часов"),
                    ("24", "Раз в сутки"),
                ],
                "default": "6",
            },
            {
                "key": "autopost_topics",
                "label": "Темы для постов",
                "description": "О чём бот будет писать (через запятую)",
                "type": "text",
                "default": "новости технологий, интересные факты, советы дня",
                "placeholder": "игры, кино, музыка, технологии...",
            },
        ],
    },
    {
        "title": "Приветствие новых участников",
        "icon": "👋",
        "settings": [
            {
                "key": "welcome_message",
                "label": "Текст приветствия",
                "description": "Сообщение новому участнику в ЛС. Оставьте пустым, чтобы не отправлять",
                "type": "textarea",
                "default": "",
                "placeholder": "Привет! Добро пожаловать в нашу группу! Напиши боту, если нужна помощь.",
            },
            {
                "key": "welcome_ai",
                "label": "ИИ-приветствие",
                "description": "Генерировать персональное приветствие через ИИ (вместо шаблона выше)",
                "type": "toggle",
                "default": "false",
            },
        ],
    },
    {
        "title": "Контент-план",
        "icon": "📅",
        "settings": [
            {
                "key": "autoplan_enabled",
                "label": "Автоматический контент-план",
                "description": "ИИ сам составит план постов на день и опубликует по расписанию",
                "type": "toggle",
                "default": "false",
            },
            {
                "key": "autoplan_times",
                "label": "Время публикаций",
                "description": "В какое время публиковать посты (через запятую, UTC)",
                "type": "text",
                "default": "09:00,13:00,18:00",
                "placeholder": "09:00,13:00,18:00",
            },
            {
                "key": "content_parse_interval_hours",
                "label": "Парсинг источников (часы)",
                "description": "Как часто проверять RSS и другие источники контента",
                "type": "select",
                "options": [
                    ("2", "Каждые 2 часа"),
                    ("4", "Каждые 4 часа"),
                    ("6", "Каждые 6 часов"),
                    ("12", "Каждые 12 часов"),
                ],
                "default": "4",
            },
        ],
    },
    {
        "title": "Виджет-лидерборд",
        "icon": "🏆",
        "settings": [
            {
                "key": "widget_enabled",
                "label": "Виджет топ-участников",
                "description": "Показывает таблицу лучших участников в виджете сообщества (как Coliseum)",
                "type": "toggle",
                "default": "false",
            },
            {
                "key": "widget_sort_by",
                "label": "Сортировка",
                "description": "По какому показателю строить рейтинг",
                "type": "select",
                "options": [
                    ("xp", "Опыт (XP)"),
                    ("level", "Уровень"),
                    ("messages", "Сообщения"),
                    ("rep", "Репутация"),
                ],
                "default": "xp",
            },
            {
                "key": "widget_top_count",
                "label": "Количество в топе",
                "description": "Сколько участников показывать в виджете",
                "type": "select",
                "options": [
                    ("5", "5"),
                    ("10", "10"),
                    ("15", "15"),
                    ("20", "20"),
                ],
                "default": "10",
            },
            {
                "key": "xp_per_like",
                "label": "XP за лайк",
                "description": "Сколько опыта получает пользователь за лайк поста в группе",
                "type": "select",
                "options": [
                    ("0", "Отключено"),
                    ("1", "1 XP"),
                    ("2", "2 XP"),
                    ("3", "3 XP"),
                    ("5", "5 XP"),
                ],
                "default": "2",
            },
            {
                "key": "xp_per_repost",
                "label": "XP за репост",
                "description": "Сколько опыта получает пользователь за репост",
                "type": "select",
                "options": [
                    ("0", "Отключено"),
                    ("3", "3 XP"),
                    ("5", "5 XP"),
                    ("10", "10 XP"),
                ],
                "default": "5",
            },
        ],
    },
    {
        "title": "Telegram кросс-постинг",
        "icon": "📨",
        "settings": [
            {
                "key": "telegram_enabled",
                "label": "Кросс-постинг в Telegram",
                "description": "Автоматически дублировать посты из VK в Telegram-канал",
                "type": "toggle",
                "default": "false",
            },
            {
                "key": "telegram_bot_token",
                "label": "Токен бота",
                "description": "Токен Telegram-бота (получите у @BotFather). Если пусто — используется глобальный из .env",
                "type": "text",
                "default": "",
                "placeholder": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            },
            {
                "key": "telegram_chat_id",
                "label": "Chat ID / канал",
                "description": "ID чата или @username канала, куда отправлять посты",
                "type": "text",
                "default": "",
                "placeholder": "@mychannel или -1001234567890",
            },
            {
                "key": "telegram_add_vk_link",
                "label": "Добавлять ссылку на VK",
                "description": "Добавлять ссылку на оригинальный пост VK в конец сообщения",
                "type": "toggle",
                "default": "true",
            },
        ],
    },
]


def _base_html(title: str, content: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — VKAdmin</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f2f5; color: #333; line-height: 1.5; }}
    .container {{ max-width: 780px; margin: 0 auto; padding: 20px; }}

    .header {{ background: linear-gradient(135deg, #1976d2, #1565c0); color: white; padding: 24px; margin-bottom: 20px; border-radius: 12px; }}
    .header h1 {{ font-size: 1.4rem; font-weight: 700; }}
    .header p {{ opacity: 0.85; margin-top: 4px; font-size: 0.9rem; }}

    .card {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
    .card-title {{ font-size: 1.1rem; font-weight: 700; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }}

    .btn {{ display: inline-block; padding: 10px 20px; background: #1976d2; color: white; text-decoration: none; border-radius: 8px; border: none; cursor: pointer; font-size: 0.95rem; font-weight: 500; transition: background 0.15s; }}
    .btn:hover {{ background: #1565c0; }}
    .btn-danger {{ background: #d32f2f; }}
    .btn-danger:hover {{ background: #c62828; }}
    .btn-sm {{ padding: 7px 16px; font-size: 0.85rem; }}
    .btn-outline {{ background: transparent; color: #1976d2; border: 1.5px solid #1976d2; }}
    .btn-outline:hover {{ background: #e3f2fd; }}

    .group-card {{ display: flex; justify-content: space-between; align-items: center; }}
    .group-info h3 {{ font-size: 1.05rem; margin-bottom: 2px; }}
    .group-info p {{ font-size: 0.85rem; color: #888; }}
    .group-actions {{ display: flex; gap: 8px; align-items: center; }}

    .badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.8rem; font-weight: 500; }}
    .badge-green {{ background: #e8f5e9; color: #2e7d32; }}

    /* Settings */
    .setting {{ padding: 16px 0; border-bottom: 1px solid #f0f0f0; }}
    .setting:last-child {{ border-bottom: none; }}
    .setting-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }}
    .setting-info {{ flex: 1; }}
    .setting-label {{ font-weight: 600; font-size: 0.95rem; }}
    .setting-desc {{ font-size: 0.82rem; color: #888; margin-top: 2px; }}
    .setting-control {{ flex-shrink: 0; min-width: 200px; }}

    input[type="text"], textarea, select {{
        width: 100%; padding: 8px 12px; border: 1.5px solid #e0e0e0; border-radius: 8px;
        font-size: 0.9rem; font-family: inherit; transition: border-color 0.15s; background: #fafafa;
    }}
    input[type="text"]:focus, textarea:focus, select:focus {{
        outline: none; border-color: #1976d2; background: white;
    }}
    textarea {{ resize: vertical; min-height: 70px; }}
    select {{ cursor: pointer; }}

    /* Toggle switch */
    .toggle {{ position: relative; display: inline-block; width: 48px; height: 26px; }}
    .toggle input {{ opacity: 0; width: 0; height: 0; }}
    .toggle-slider {{
        position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
        background: #ccc; border-radius: 26px; transition: 0.2s;
    }}
    .toggle-slider:before {{
        content: ""; position: absolute; height: 20px; width: 20px; left: 3px; bottom: 3px;
        background: white; border-radius: 50%; transition: 0.2s;
    }}
    .toggle input:checked + .toggle-slider {{ background: #4caf50; }}
    .toggle input:checked + .toggle-slider:before {{ transform: translateX(22px); }}

    .save-btn {{
        margin-top: 4px; padding: 5px 14px; font-size: 0.8rem; background: #4caf50;
        color: white; border: none; border-radius: 6px; cursor: pointer; display: none;
    }}
    .save-btn:hover {{ background: #43a047; }}
    .save-btn.show {{ display: inline-block; }}

    .flash {{ padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 0.9rem; }}
    .flash-success {{ background: #e8f5e9; color: #2e7d32; border: 1px solid #c8e6c9; }}

    .back {{ color: #1976d2; text-decoration: none; display: inline-flex; align-items: center; gap: 4px; margin-bottom: 16px; font-size: 0.9rem; }}
    .back:hover {{ text-decoration: underline; }}

    .connect-form {{ display: flex; gap: 10px; align-items: flex-end; margin-top: 12px; }}
    .connect-form .form-group {{ flex: 1; margin: 0; }}
    .connect-form label {{ display: block; font-weight: 600; margin-bottom: 4px; font-size: 0.9rem; }}
    .hint {{ margin-top: 8px; font-size: 0.8rem; color: #aaa; }}

    /* Sources table */
    .source-table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
    .source-table th {{ text-align: left; font-size: 0.8rem; color: #999; font-weight: 500; padding: 6px 8px; border-bottom: 2px solid #f0f0f0; }}
    .source-table td {{ padding: 10px 8px; border-bottom: 1px solid #f5f5f5; font-size: 0.9rem; vertical-align: middle; }}
    .source-table tr:last-child td {{ border-bottom: none; }}
    .source-url {{ color: #1976d2; word-break: break-all; }}
    .source-type {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.78rem; font-weight: 500; }}
    .source-type-rss {{ background: #fff3e0; color: #e65100; }}
    .source-type-vk {{ background: #e3f2fd; color: #1565c0; }}
    .source-type-api {{ background: #f3e5f5; color: #7b1fa2; }}
    .source-type-web {{ background: #e0f2f1; color: #00695c; }}
    .source-empty {{ text-align: center; padding: 24px; color: #bbb; font-size: 0.9rem; }}
    .source-add {{ display: flex; gap: 8px; align-items: flex-end; margin-top: 16px; flex-wrap: wrap; }}
    .source-add .form-group {{ margin: 0; }}
    .source-fetched {{ font-size: 0.78rem; color: #aaa; }}
    .btn-delete {{ background: none; border: none; color: #d32f2f; cursor: pointer; font-size: 0.85rem; padding: 4px 8px; border-radius: 4px; }}
    .btn-delete:hover {{ background: #ffebee; }}

    .toast {{
        position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
        background: #2e7d32; color: white; padding: 12px 28px; border-radius: 10px;
        font-size: 0.95rem; font-weight: 500; z-index: 9999; opacity: 0;
        transition: opacity 0.3s; pointer-events: none; box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    }}
    .toast.show {{ opacity: 1; }}
</style>
<script>
function showToast(msg) {{
    var t = document.getElementById('toast');
    if (!t) return;
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
<body><div id="toast" class="toast"></div><div class="container">{content}</div></body></html>"""


# ─── Login / Logout ──────────────────────────────────────────────────────────

@router.get("/dashboard/login")
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/dashboard", status_code=303)

    error = request.query_params.get("error", "")
    error_html = '<p style="color:#d32f2f;margin-bottom:12px;">Неверный пароль</p>' if error else ""

    content = f"""
    <div style="max-width:380px;margin:80px auto;">
        <div class="header" style="text-align:center;">
            <h1>VKAdmin</h1>
            <p>Панель управления</p>
        </div>
        {error_html}
        <div class="card">
            <form method="POST" action="/dashboard/login">
                <input type="hidden" name="_csrf" value="{get_csrf_token(request)}">
                <div class="form-group" style="margin-bottom:16px;">
                    <label style="display:block;font-weight:600;margin-bottom:6px;">Пароль</label>
                    <input type="password" name="password" placeholder="Введите пароль..." required autofocus
                           style="width:100%;padding:10px 14px;border:1.5px solid #e0e0e0;border-radius:8px;font-size:1rem;">
                </div>
                <button type="submit" class="btn" style="width:100%;">Войти</button>
            </form>
            <p class="hint" style="margin-top:12px;text-align:center;">Пароль задаётся в .env как JWT_SECRET</p>
        </div>
    </div>
    """
    response = HTMLResponse(_base_html("Вход", content))
    set_csrf_cookie(response, get_csrf_token(request))
    return response


@router.post("/dashboard/login")
async def login_submit(request: Request):
    if not await verify_csrf_token(request):
        return RedirectResponse("/dashboard/login?error=1", status_code=303)
    form = await request.form()
    password = str(form.get("password", ""))

    if password == get_dashboard_password():
        response = RedirectResponse("/dashboard", status_code=303)
        set_auth_cookie(response)
        return response

    return RedirectResponse("/dashboard/login?error=1", status_code=303)


@router.get("/dashboard/logout")
async def logout(request: Request):
    response = RedirectResponse("/dashboard/login", status_code=303)
    clear_auth_cookie(response)
    return response


# ─── Dashboard home ─────────────────────────────────────────────────────────

@router.get("/dashboard")
async def dashboard_home(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    csrf = _csrf_field(request)
    csrf_token = get_csrf_token(request)

    groups = await get_all_active_groups()

    if not groups:
        content = """
        <div class="header">
            <h1>VKAdmin</h1>
            <p>AI-администратор для групп ВКонтакте</p>
        </div>
        <div class="card">
            <div class="card-title">Подключите группу</div>
            <p style="margin-bottom:12px; color:#666;">Введите ID группы, и бот начнёт работать автоматически.</p>
            <form action="/api/vk/oauth" method="GET" class="connect-form">
                <div class="form-group">
                    <label>ID группы</label>
                    <input type="text" name="group_ids" placeholder="236517033" required>
                </div>
                <button type="submit" class="btn">Подключить</button>
            </form>
            <p class="hint">Где найти ID: откройте группу ВК, в адресе будет vk.com/club<b>123456</b> — число после club и есть ID</p>
        </div>
        """
        response = HTMLResponse(_base_html("Панель управления", content))
        set_csrf_cookie(response, csrf_token)
        return response

    groups_html = ""
    for g in groups:
        name = escape(g.group_name or f"Группа {g.group_id}")
        groups_html += f"""
        <div class="card">
            <div class="group-card">
                <div class="group-info">
                    <h3>{name}</h3>
                    <p>ID: {g.group_id} &nbsp; <span class="badge badge-green">Работает</span></p>
                </div>
                <div class="group-actions">
                    <a href="/dashboard/group/{g.group_id}" class="btn btn-sm">Настроить</a>
                    <form method="POST" action="/dashboard/group/{g.group_id}/disconnect"
                          onsubmit="return confirm('Отключить бота от этой группы?');">
                        {csrf}
                        <button type="submit" class="btn btn-danger btn-sm">Отключить</button>
                    </form>
                </div>
            </div>
        </div>
        """

    content = f"""
    <div class="header">
        <h1>VKAdmin</h1>
        <p>AI-администратор для групп ВКонтакте</p>
    </div>
    {groups_html}
    <div class="card">
        <div class="card-title">Подключить ещё группу</div>
        <form action="/api/vk/oauth" method="GET" class="connect-form">
            <div class="form-group">
                <label>ID группы</label>
                <input type="text" name="group_ids" placeholder="236517033" required>
            </div>
            <button type="submit" class="btn btn-outline btn-sm">Подключить</button>
        </form>
        <p class="hint">vk.com/club<b>123456</b> → ID = 123456</p>
    </div>
    """
    response = HTMLResponse(_base_html("Панель управления", content))
    set_csrf_cookie(response, csrf_token)
    return response


# ─── Group settings page ────────────────────────────────────────────────────

@router.get("/dashboard/group/{group_id}")
async def group_settings_page(request: Request, group_id: int):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    group = await get_group(group_id)
    if not group:
        return HTMLResponse(
            _base_html("Ошибка", '<div class="card"><p>Группа не найдена</p></div>'),
            status_code=404,
        )

    csrf = _csrf_field(request)
    csrf_token = get_csrf_token(request)

    sections_html = ""
    for section in SETTINGS_SCHEMA:
        items_html = ""
        for s in section["settings"]:
            key = s["key"]
            current_value = await get_setting(group_id, key, s.get("default", ""))
            control = _render_control(group_id, s, current_value, csrf)
            items_html += f"""
            <div class="setting">
                <div class="setting-header">
                    <div class="setting-info">
                        <div class="setting-label">{s['label']}</div>
                        <div class="setting-desc">{s['description']}</div>
                    </div>
                    <div class="setting-control">
                        {control}
                    </div>
                </div>
            </div>
            """

        sections_html += f"""
        <div class="card">
            <div class="card-title">{section['icon']} {section['title']}</div>
            {items_html}
        </div>
        """

    # ── Content sources section ──
    sources = await get_content_sources(group_id)
    sources_rows = ""
    if sources:
        for s in sources:
            type_class = {"rss": "rss", "vk_group": "vk", "api": "api", "web": "web"}.get(s.source_type, "api")
            type_label = {"rss": "RSS", "vk_group": "VK группа", "api": "API", "web": "Сайт"}.get(s.source_type, s.source_type)
            fetched = s.last_fetched_at.strftime("%d.%m %H:%M") if s.last_fetched_at else "ещё не запускался"
            keywords = s.filter_keywords or "—"
            sources_rows += f"""
            <tr>
                <td><span class="source-type source-type-{type_class}">{type_label}</span></td>
                <td><span class="source-url">{escape(s.source_url)}</span></td>
                <td><span class="source-fetched">{fetched}</span></td>
                <td>
                    <form method="POST" action="/dashboard/group/{group_id}/sources/delete"
                          onsubmit="return confirm('Удалить этот источник?');">
                        {csrf}
                        <input type="hidden" name="source_id" value="{s.id}">
                        <button type="submit" class="btn-delete">Удалить</button>
                    </form>
                </td>
            </tr>
            """
        sources_table = f"""
        <table class="source-table">
            <thead><tr><th>Тип</th><th>Адрес</th><th>Последний парсинг</th><th></th></tr></thead>
            <tbody>{sources_rows}</tbody>
        </table>
        """
    else:
        sources_table = '<p class="source-empty">Нет подключённых источников. Добавьте RSS-ленту или группу ВК, чтобы бот брал оттуда контент.</p>'

    sources_html = f"""
    <div class="card">
        <div class="card-title">📡 Источники контента</div>
        <div class="setting-desc" style="margin-bottom:8px;">
            Бот будет парсить эти источники, переписывать контент через ИИ и публиковать в вашей группе
        </div>
        {sources_table}
        <form method="POST" action="/dashboard/group/{group_id}/sources/add" class="source-add">
            {csrf}
            <div class="form-group">
                <label style="font-size:0.85rem;">Тип</label>
                <select name="source_type" style="width:140px;">
                    <option value="rss">RSS-лента</option>
                    <option value="web">Сайт (URL)</option>
                    <option value="vk_group">VK группа</option>
                    <option value="api">API (JSON)</option>
                </select>
            </div>
            <div class="form-group" style="flex:1;">
                <label style="font-size:0.85rem;">Адрес</label>
                <input type="text" name="source_url" placeholder="https://example.com/rss или короткое имя группы ВК" required>
            </div>
            <div class="form-group">
                <label style="font-size:0.85rem;">Фильтр (необязательно)</label>
                <input type="text" name="filter_keywords" placeholder="ключевые слова через запятую" style="width:220px;">
            </div>
            <button type="submit" class="btn btn-sm">Добавить</button>
        </form>
        <p class="hint">
            RSS — вставьте ссылку на RSS-ленту сайта (обычно /rss или /feed).<br>
            VK группа — вставьте короткое имя группы (например durov) или числовой ID.<br>
            API — URL, возвращающий JSON со списком статей.
        </p>
    </div>
    """

    # ── Content tasks section ──
    tasks = await get_content_tasks(group_id)
    tasks_rows = ""
    if tasks:
        for t in tasks:
            last = t.last_run_at.strftime("%d.%m %H:%M") if t.last_run_at else "ещё не запускалась"
            type_labels = {"patch_notes": "Патч-ноты", "article": "Статья", "digest": "Дайджест"}
            type_label = type_labels.get(t.task_type, t.task_type)
            tasks_rows += f"""
            <tr>
                <td><span class="source-type source-type-api">{type_label}</span></td>
                <td><span class="source-url">{escape(t.source_url or '—')}</span></td>
                <td><code style="font-size:0.82rem;">{escape(t.schedule_cron)}</code></td>
                <td><span class="source-fetched">{last}</span></td>
                <td>
                    <form method="POST" action="/dashboard/group/{group_id}/tasks/delete"
                          onsubmit="return confirm('Удалить задачу?');">
                        {csrf}
                        <input type="hidden" name="task_id" value="{t.id}">
                        <button type="submit" class="btn-delete">Удалить</button>
                    </form>
                </td>
            </tr>
            """
        tasks_table = f"""
        <table class="source-table">
            <thead><tr><th>Тип</th><th>Источник</th><th>Расписание</th><th>Последний запуск</th><th></th></tr></thead>
            <tbody>{tasks_rows}</tbody>
        </table>
        """
    else:
        tasks_table = '<p class="source-empty">Нет контент-задач. Добавьте, чтобы бот сам писал статьи по расписанию.</p>'

    tasks_html = f"""
    <div class="card">
        <div class="card-title">📋 Контент-задачи</div>
        <div class="setting-desc" style="margin-bottom:8px;">
            Бот автоматически создаёт контент по расписанию: патч-ноты из GitHub, статьи с сайтов, дайджесты
        </div>
        {tasks_table}
        <form method="POST" action="/dashboard/group/{group_id}/tasks/add" class="source-add">
            {csrf}
            <div class="form-group">
                <label style="font-size:0.85rem;">Тип</label>
                <select name="task_type" style="width:140px;">
                    <option value="patch_notes">Патч-ноты</option>
                    <option value="article">Статья</option>
                    <option value="digest">Дайджест</option>
                </select>
            </div>
            <div class="form-group" style="flex:1;">
                <label style="font-size:0.85rem;">Источник (URL)</label>
                <input type="text" name="source_url" placeholder="https://github.com/user/repo">
            </div>
            <div class="form-group">
                <label style="font-size:0.85rem;">Расписание (cron)</label>
                <input type="text" name="schedule_cron" placeholder="0 18 * * 5" style="width:140px;" required>
            </div>
            <button type="submit" class="btn btn-sm">Добавить</button>
        </form>
        <p class="hint">
            Cron формат: минута час день месяц день_недели.<br>
            Примеры: <code>0 18 * * 5</code> = пятница 18:00 UTC, <code>0 10 * * 1</code> = понедельник 10:00 UTC
        </p>
    </div>
    """

    # ── AI refresh section ──
    ai_desc = await get_setting(group_id, "ai_group_description", "")
    if ai_desc:
        ai_status = f'<span style="color:#2e7d32;">✓ Настроен: {escape(ai_desc[:100])}</span>'
    else:
        ai_status = '<span style="color:#d32f2f;">✗ Не настроен — нажмите кнопку ниже</span>'

    ai_refresh_html = f"""
    <div class="card">
        <div class="card-title">🔄 ИИ-профиль группы</div>
        <p style="font-size:0.9rem;margin-bottom:8px;">{ai_status}</p>
        <p style="font-size:0.82rem;color:#888;margin-bottom:14px;">
            Бот сканирует группу (описание, последние посты) и автоматически настраивает свою личность,
            правила модерации и темы контента под тематику группы.
        </p>
        <form method="POST" action="/dashboard/group/{group_id}/ai-refresh">
            {csrf}
            <button type="submit" class="btn">Обновить ИИ-профиль</button>
        </form>
    </div>
    """

    name = escape(group.group_name or f"Группа {group_id}")
    content = f"""
    <a href="/dashboard" class="back">← Назад</a>
    <div class="header">
        <h1>{name}</h1>
        <p>ID: {group_id} &nbsp; <span class="badge badge-green" style="font-size:0.75rem;">Работает</span></p>
    </div>
    {sections_html}
    {sources_html}
    {tasks_html}
    {ai_refresh_html}
    """
    response = HTMLResponse(_base_html(name, content))
    set_csrf_cookie(response, csrf_token)
    return response


def _render_control(group_id: int, setting: dict, current_value: str, csrf: str = "") -> str:
    """Render the appropriate input control for a setting."""
    key = setting["key"]
    stype = setting.get("type", "text")

    if stype == "toggle":
        checked = "checked" if current_value.lower() == "true" else ""
        return f"""
        <form method="POST" action="/dashboard/group/{group_id}/settings" onsubmit="return ajaxSubmit(this)" style="display:flex;align-items:center;justify-content:flex-end;gap:8px;">
            {csrf}
            <input type="hidden" name="key" value="{key}">
            <input type="hidden" name="value" value="false">
            <label class="toggle">
                <input type="checkbox" name="value" value="true" {checked}
                       onchange="ajaxSubmit(this.form)">
                <span class="toggle-slider"></span>
            </label>
        </form>
        """

    if stype == "select":
        options = setting.get("options", [])
        opts_html = ""
        for val, label in options:
            selected = "selected" if val == current_value else ""
            opts_html += f'<option value="{val}" {selected}>{label}</option>'
        return f"""
        <form method="POST" action="/dashboard/group/{group_id}/settings" onsubmit="return ajaxSubmit(this)">
            {csrf}
            <input type="hidden" name="key" value="{key}">
            <select name="value" onchange="ajaxSubmit(this.form)" style="cursor:pointer;">
                {opts_html}
            </select>
        </form>
        """

    if stype == "textarea":
        placeholder = setting.get("placeholder", "")
        return f"""
        <form method="POST" action="/dashboard/group/{group_id}/settings" onsubmit="return ajaxSubmit(this)">
            {csrf}
            <input type="hidden" name="key" value="{key}">
            <textarea name="value" placeholder="{escape(placeholder)}"
                      onfocus="this.nextElementSibling.classList.add('show')">{escape(current_value)}</textarea>
            <button type="submit" class="save-btn">Сохранить</button>
        </form>
        """

    # text
    placeholder = setting.get("placeholder", "")
    return f"""
    <form method="POST" action="/dashboard/group/{group_id}/settings" onsubmit="return ajaxSubmit(this)">
        {csrf}
        <input type="hidden" name="key" value="{key}">
        <input type="text" name="value" value="{escape(current_value, quote=True)}" placeholder="{escape(placeholder, quote=True)}"
               onfocus="this.nextElementSibling.classList.add('show')">
        <button type="submit" class="save-btn">Сохранить</button>
    </form>
    """


# ─── Actions ─────────────────────────────────────────────────────────────────

@router.post("/dashboard/group/{group_id}/settings")
async def update_group_setting(request: Request, group_id: int):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    if not await verify_csrf_token(request):
        return RedirectResponse(f"/dashboard/group/{group_id}", status_code=303)
    form = await request.form()
    key = str(form.get("key", "")).strip()

    # For toggles with checkbox: if unchecked, hidden input sends "false"
    # if checked, checkbox sends "true" (overrides hidden)
    values = form.getlist("value")
    value = values[-1] if values else ""
    value = str(value).strip()

    if key:
        await set_setting(group_id, key, value)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JSONResponse({"ok": True})

    return RedirectResponse(f"/dashboard/group/{group_id}?msg=saved", status_code=303)


@router.post("/dashboard/group/{group_id}/sources/add")
async def add_source(request: Request, group_id: int):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    if not await verify_csrf_token(request):
        return RedirectResponse(f"/dashboard/group/{group_id}", status_code=303)
    form = await request.form()
    source_type = str(form.get("source_type", "rss")).strip()
    source_url = str(form.get("source_url", "")).strip()
    filter_keywords = str(form.get("filter_keywords", "")).strip()

    if source_url:
        await add_content_source(group_id, source_type, source_url, filter_keywords)

    return RedirectResponse(f"/dashboard/group/{group_id}?msg=saved", status_code=303)


@router.post("/dashboard/group/{group_id}/sources/delete")
async def remove_source(request: Request, group_id: int):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    if not await verify_csrf_token(request):
        return RedirectResponse(f"/dashboard/group/{group_id}", status_code=303)
    form = await request.form()
    source_id = int(form.get("source_id", 0))
    if source_id:
        await delete_content_source(source_id)

    return RedirectResponse(f"/dashboard/group/{group_id}?msg=saved", status_code=303)


@router.post("/dashboard/group/{group_id}/tasks/add")
async def add_task(request: Request, group_id: int):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    if not await verify_csrf_token(request):
        return RedirectResponse(f"/dashboard/group/{group_id}", status_code=303)
    form = await request.form()
    task_type = str(form.get("task_type", "article")).strip()
    source_url = str(form.get("source_url", "")).strip()
    schedule_cron = str(form.get("schedule_cron", "")).strip()

    if schedule_cron:
        try:
            from croniter import croniter
            croniter(schedule_cron)
        except Exception:
            pass  # let it fail at runtime
        name = f"{task_type}_{source_url.split('/')[-1] if source_url else 'manual'}"
        await create_content_task(
            group_id=group_id, name=name, task_type=task_type,
            schedule_cron=schedule_cron, source_url=source_url,
        )

    return RedirectResponse(f"/dashboard/group/{group_id}?msg=saved", status_code=303)


@router.post("/dashboard/group/{group_id}/tasks/delete")
async def remove_task(request: Request, group_id: int):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    if not await verify_csrf_token(request):
        return RedirectResponse(f"/dashboard/group/{group_id}", status_code=303)
    form = await request.form()
    task_id = int(form.get("task_id", 0))
    if task_id:
        await delete_content_task(task_id)
    return RedirectResponse(f"/dashboard/group/{group_id}?msg=saved", status_code=303)


@router.post("/dashboard/group/{group_id}/ai-refresh")
async def ai_refresh(request: Request, group_id: int):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    if not await verify_csrf_token(request):
        return RedirectResponse(f"/dashboard/group/{group_id}", status_code=303)

    group = await get_group(group_id)
    if group:
        from core.crypto import decrypt_token
        from core.group_setup import setup_group_ai
        try:
            token = decrypt_token(group.access_token)
            await setup_group_ai(group_id, token)
        except Exception as e:
            logger.error(f"AI refresh failed for group {group_id}: {e}")

    return RedirectResponse(f"/dashboard/group/{group_id}?msg=saved", status_code=303)


@router.post("/dashboard/group/{group_id}/disconnect")
async def disconnect_group(request: Request, group_id: int):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    if not await verify_csrf_token(request):
        return RedirectResponse("/dashboard", status_code=303)
    await deactivate_group(group_id)
    return RedirectResponse("/dashboard", status_code=303)
