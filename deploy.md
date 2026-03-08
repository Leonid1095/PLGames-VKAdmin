# Инструкция по развертыванию на Ubuntu 24

Этот гайд поможет в 3 простых шага запустить ВК-бота на вашем сервере под управлением Ubuntu 24.04.

## Шаг 1: Подготовка сервера

Подключитесь к вашему серверу по SSH. Вам нужно установить только **Docker** и **Git**. Выполните эти команды:

```bash
# Обновляем пакеты
sudo apt update && sudo apt upgrade -y

# Устанавливаем Git
sudo apt install -y git

# Устанавливаем Docker и Docker Compose
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

_(После последней команды желательно перезайти на сервер, чтобы применились права для Docker)_

## Шаг 2: Скачивание проекта и настройка

```bash
# Клонируем репозиторий (замените URL на ваш)
git clone https://github.com/ВАШ_ЛОГИН/vk-ai-admin-bot.git
cd vk-ai-admin-bot

# Создаем файл настроек
cp .env.example .env
```

Теперь откройте файл конфигурации:

```bash
nano .env
```

Вставьте туда ваши токены:

- `VK_TOKEN` — токен группы ВК (с правами на сообщения, стену, управление)
- `VK_GROUP_ID` — цифровой ID вашей группы (без минуса)
- `OWNER_VK_ID` — ваш цифровой ID (чтобы команды в ЛС работали только для вас)
- `OPENROUTER_API_KEY` — ключ от OpenRouter для работы ИИ

Нажмите `Ctrl+O` -> `Enter` (чтобы сохранить), затем `Ctrl+X` (чтобы выйти).

## Шаг 3: Запуск бота

Всё готово! Одной командой запускаем базу данных и самого бота:

```bash
sudo docker compose up -d --build
```

**Готово! Бот запущен и работает в фоне.** 🚀

### Полезные команды (шпаргалка):

- **Посмотреть логи (что бот делает прямо сейчас):**
  ```bash
  sudo docker compose logs -f bot
  ```
- **Перезапустить бота (если завис или обновили код):**
  ```bash
  sudo docker compose restart bot
  ```
- **Остановить бота:**
  ```bash
  sudo docker compose down
  ```
- **Обновить код (если вы внесли изменения на компьютере и залили в гит):**
  ```bash
  git pull
  sudo docker compose up -d --build
  ```
