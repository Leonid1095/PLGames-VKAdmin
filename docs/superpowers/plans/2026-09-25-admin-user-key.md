# Личный ключ админа — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** владелец подключает личный ключ VK-админа кнопкой в дашборде, и бот
сам грузит фото к постам, удаляет нарушения, банит и закрепляет.

**Architecture:** новый модуль `core/admin_key.py` владеет хранением
(зашифрованные настройки группы), проверкой админства и выдачей `API`.
OAuth-старт и обработка возврата — в `web/oauth.py` рядом с групповым
потоком, различение по отдельной state-куке. Потребители (`core/images`,
`handlers/comments`, `core/agent`) берут `get_admin_api()` и падают обратно на
ключ сообщества, если ключа нет.

**Tech Stack:** FastAPI, httpx, vkbottle `API`, SQLAlchemy async (settings),
Fernet (`core.crypto`), pytest + httpx.MockTransport.

**Spec:** `docs/superpowers/specs/2026-09-25-admin-user-key-design.md`

## Global Constraints

- scope ключа: `wall,photos,groups,offline`; в authorize-URL всегда `revoke=1`, `v=5.199`.
- redirect URI — существующий `{BASE_URL}/api/vk/callback` (новый не регистрируем).
- state-кука админского потока: `vkadmin_admin_oauth_state`, httponly, samesite=lax, max_age=600.
- ключи настроек: `admin_user_token` (только `encrypt_token`), `admin_user_id`, `admin_user_name`, `admin_key_error`, `admin_key_alerted`.
- ключ никогда не выводится в HTML и лог; в логах только имена параметров.
- мёртвым ключ признаётся только по ошибке VK 5.
- тесты не ходят в настоящий VK (автофикстура `_no_vk_network` + свои подмены).

## Review Focus

1. Аккаунт — админ только одной из двух групп → ключ сохраняется только для неё (Task 1).
2. VK отказал при обмене кода (ошибка в ответе `access_token`) → понятная страница, ничего не сохранено (Task 2).
3. Ключ отозван позже → ошибка 5 → помечен мёртвым один раз, одно ЛС, действия падают обратно на ссылки (Task 1, Task 4).
4. Переподключение после смерти ключа → `admin_key_error` очищен, ключ снова используется (Task 1).
5. «Отключить» без CSRF → ключ на месте (Task 3).

---

### Task 1: `core/admin_key.py` — хранение, проверка, выдача API

**Files:**
- Create: `core/admin_key.py`
- Modify: `core/escalation.py` (публичный `notify_group_admins`), `core/widgets.py` (`_notify_admin` делегирует)
- Test: `tests/test_admin_key.py`

**Interfaces:**
- Produces:
  - `class AdminKeyError(Exception)` — сообщение для владельца.
  - `async def connect_admin_key(token: str, user_id: int) -> list[tuple[int, str]]` — (group_id, name) групп, где VK подтвердил админство; иначе `AdminKeyError`.
  - `async def disconnect_admin_key(group_id: int) -> None`
  - `async def get_admin_api(group_id: int) -> API | None` — `None`, если ключа нет или он помечен мёртвым.
  - `async def admin_token(group_id: int) -> str` — расшифрованный ключ или `""` (для карточки).
  - `async def report_admin_key_failure(group_id: int, e: Exception) -> None` — реагирует только на код 5.
  - `ADMIN_KEY_HINT: str` — «подключите личный ключ в панели: Ключи и доступы → Подключить».
  - `core.escalation.notify_group_admins(group_id: int, text: str) -> bool`

- [ ] **Step 1: failing tests** — `tests/test_admin_key.py`:

```python
async def test_connect_stores_encrypted_key_only_for_groups_where_vk_confirms_admin(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("g"), ADMIN)
    await create_group(GID2, "Twitch", encrypt_token("g"), ADMIN)
    _vk(monkeypatch, admin_of=[GID])          # groups.get(filter=admin) → только GID
    connected = await admin_key.connect_admin_key("user-token", ADMIN)
    assert connected == [(GID, "WOW")]
    stored = await get_setting(GID, "admin_user_token")
    assert stored and stored != "user-token" and decrypt_token(stored) == "user-token"
    assert await get_setting(GID2, "admin_user_token", "") == ""
    assert await get_setting(GID, "admin_user_name") == "Ленар Фатыхов"

async def test_connect_refuses_when_account_admins_none_of_our_groups(db, monkeypatch):
    ... admin_of=[999] → pytest.raises(AdminKeyError); ничего не сохранено

async def test_dead_key_is_reported_once_and_no_longer_used(db, monkeypatch, notes):
    await admin_key.report_admin_key_failure(GID, _vk_error(5)) дважды →
    len(notes) == 1; await get_admin_api(GID) is None

async def test_other_errors_do_not_kill_key(db, notes):
    report_admin_key_failure(GID, _vk_error(15)) → get_admin_api(GID) is not None; notes == []

async def test_reconnect_clears_dead_mark(db, monkeypatch, notes): ... error → connect → get_admin_api not None

async def test_disconnect_forgets_key(db, monkeypatch): ... → get_admin_api is None
```

- [ ] **Step 2:** `venv/bin/python -m pytest tests/test_admin_key.py -q` → FAIL (нет модуля).
- [ ] **Step 3:** реализовать модуль: `_client()` (httpx, timeout 15), `_vk(token, method, **params)` (POST, `VKReadError` из `core.vk_read` на ошибку), `groups.get(filter="admin")` → пересечение с `get_all_active_groups()`, `users.get` → имя, запись настроек, алерт через `notify_group_admins` с флагом `admin_key_alerted` (как `widgets._alert_once`).
- [ ] **Step 4:** тесты зелёные + весь `pytest`.
- [ ] **Step 5:** коммит `feat: хранение личного ключа админа (core/admin_key)`.

### Task 2: OAuth — «Подключить» и возврат от VK

**Files:**
- Modify: `web/oauth.py`
- Test: `tests/test_admin_oauth.py`

**Interfaces:**
- Consumes: `connect_admin_key`, `AdminKeyError` (Task 1).
- Produces: `GET /api/vk/admin-oauth`; ветка админского потока в `GET /api/vk/callback` (code) и `GET /api/vk/callback/token` (фрагмент); константы `ADMIN_STATE_COOKIE`, `ADMIN_SCOPE`.

- [ ] **Step 1: failing tests:**

```python
async def test_start_requires_dashboard_login(): GET без куки → 303 на /dashboard/login
async def test_start_redirects_to_vk_with_admin_scope_and_state_cookie():
    r = GET /api/vk/admin-oauth (с сессией) → 307/302 на oauth.vk.com/authorize,
    в Location: scope=wall,photos,groups,offline, revoke=1, redirect_uri=.../api/vk/callback,
    state совпадает с кукой vkadmin_admin_oauth_state; group_ids отсутствует
async def test_callback_code_connects_admin_key(db, monkeypatch):
    VK: access_token → {"access_token": "user-token", "user_id": ADMIN};
    groups.get → [GID]; users.get → имя. GET /api/vk/callback?code=c&state=S с кукой S и сессией →
    200, в тексте «WOW»; decrypt(get_setting(GID,"admin_user_token")) == "user-token";
    group-потоковые таблицы не тронуты (access_token группы прежний)
async def test_callback_with_foreign_state_does_not_touch_admin_key(...) → 400, ключа нет
async def test_vk_refusing_code_exchange_shows_reason_and_saves_nothing(...):
    VK access_token → {"error": "invalid_grant", "error_description": "..."} → 400, текст причины, ключа нет
async def test_fragment_token_connects_admin_key(...): GET /api/vk/callback/token?access_token=..&user_id=..&state=S → 200
```

- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** реализовать `start_admin_oauth`, `_is_admin_flow(request, state)`, `_finish_admin_oauth(token, user_id)`; в `oauth_callback` после блока `if not code` — `if _is_admin_flow(...)`: требовать сессию, обменять код тем же запросом, что и группы, вызвать `_finish_admin_oauth`; в `oauth_token_callback` — ту же ветку до групповой проверки state. Ответ удаляет админскую куку. В лог — только имена групп и число.
- [ ] **Step 4:** тесты + `tests/test_oauth_security.py` + весь `pytest`.
- [ ] **Step 5:** коммит `feat: подключение личного ключа админа через VK OAuth`.

### Task 3: карточка «Ключи и доступы» — статус и кнопки

**Files:**
- Modify: `core/key_status.py` (`_check_admin_key` вместо статичного `_admin_key_status`), `web/dashboard/routes.py` (кнопки + `POST /dashboard/group/{group_id}/admin-key/disconnect`)
- Test: `tests/test_keys_card.py`

**Interfaces:**
- Consumes: `admin_token`, `disconnect_admin_key`, настройки из Task 1.
- Produces: `KeyStatus(key="admin", state in {"off","ok","fail"})`.

- [ ] **Step 1: failing tests:** нет ключа → `data-key="admin" data-state="off"` и ссылка `/api/vk/admin-oauth`; ключ есть и `users.get` отвечает → `ok`, имя в тексте, форма «Отключить» с csrf; помечен мёртвым → `fail` с текстом ошибки; `POST .../admin-key/disconnect` без CSRF → ключ на месте, с CSRF → стёрт.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** реализовать; `_render_keys_card(group_id, csrf)` дописывает действия для строки `admin`.
- [ ] **Step 4:** весь `pytest`.
- [ ] **Step 5:** коммит `feat: карточка ключей — подключить/отключить личный ключ админа`.

### Task 4: бот пользуется ключом

**Files:**
- Modify: `core/images.py` (`upload_photo_to_vk`), `handlers/comments.py` (`_moderate`), `core/agent.py` (`_exec_ban_user`, `_exec_unban_user`, `_exec_pin_post`)
- Test: `tests/test_admin_key_usage.py`

**Interfaces:**
- Consumes: `get_admin_api`, `report_admin_key_failure`, `ADMIN_KEY_HINT`.

- [ ] **Step 1: failing tests** (подмена `admin_key.get_admin_api` фейковым API, который записывает вызовы):
  - `upload_photo_to_vk(group_api, GID, b"img")` при подключённом ключе зовёт `photos.get_wall_upload_server` у ключа админа, не у группы;
  - `_moderate(...)` удаляет комментарий ключом админа и не шлёт «нужна ваша рука»;
  - нет ключа → как раньше: ссылка админу, в тексте `ADMIN_KEY_HINT`;
  - ключ админа отвечает ошибкой 5 → `report_admin_key_failure` вызван, админу уходит ссылка;
  - `_exec_ban_user` / `_exec_pin_post` идут через ключ админа; без ключа ответ содержит `ADMIN_KEY_HINT`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** в каждом месте `admin = await get_admin_api(group_id)`; `api = admin or <ключ сообщества>`; при исключении от ключа админа — `report_admin_key_failure`.
- [ ] **Step 4:** весь `pytest`.
- [ ] **Step 5:** коммит `feat: фото, модерация и баны через личный ключ админа`.

### Task 5: выкладка и живая проверка

- [ ] рестарт `vkadmin.service` (после зелёного `pytest`), страница группы в дашборде отдаёт строку `admin` со ссылкой «Подключить».
- [ ] владелец нажимает «Подключить» → смотрим лог (`OAuth callback params`, `Admin key connected`) и карточку.
- [ ] если VK отказал — записать ответ VK в память и пересмотреть подход.
- [ ] push ветки.
