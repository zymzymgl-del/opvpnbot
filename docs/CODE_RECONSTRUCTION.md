# Реконструкция кода репозитория OPVPN

Документ восстанавливает архитектуру, назначение каждого модуля и основные потоки
поведения программы по исходному коду ветки `main` (коммит `7350be0`).

Проверено на текущем состоянии репозитория:
`ruff check .` — без замечаний; `tools/selftest.py` — 127/127; `tools/integration_test.py` — пройден.

---

## 1. Что это за проект

**OPVPN** — production-ready Telegram-бот для продажи VPN-подписок:

- **Стек:** Python 3.11+, `aiogram 3` (long-polling), `aiohttp`, `aiosqlite` (SQLite в WAL-режиме), `qrcode`+`pypng` (QR-коды), `python-dotenv`.
- **Оплата:** ЮKassa (карты РФ, СБП, SberPay) — создание счетов, вебхуки + поллинг как резерв.
- **Выдача ключей:** панель **3x-ui (xray)** — два режима выдачи: мгновенный (при оплате) и ручной (клиент получает ссылку-«претензию»/claim-ссылку и активирует её сам в боте).
- **Линцензия:** MIT. Проект основан на DF VPN Bot (см. `app/__init__.py`: `__author__`, `__author_url__`).

Формат деплоя: `install.sh` (systemd-служба `opvpnbot`), либо Docker (`Dockerfile`, `docker-compose.yml`).

---

## 2. Карта репозитория

```
/workspace
├── bot.py                  # точка входа: логирование, сборка диспетчера, жизненный цикл
├── app/
│   ├── __init__.py         # метаданные пакета (версия, автор)
│   ├── config.py           # Config (frozen dataclass) из .env + validate()
│   ├── runtime.py          # runtime — «горячие» настройки из БД поверх .env (singleton)
│   ├── db.py               # Database: aiosqlite-обёртка, схема, миграции, ~60 запросов
│   ├── middlewares.py      # Throttling / User / RequiredChannel / Errors
│   ├── keyboards.py        # все inline-клавиатуры бота (единая точка изменения кнопок)
│   ├── utils.py            # форматтеры дат/цен/байтов, QR, инструкции для приложений
│   ├── logctx.py           # RequestIdFilter — rid=<request_id> в логах
│   ├── health.py           # aiohttp healthcheck-сервер (/healthz) для uptime-мониторинга
│   ├── webhook.py          # HTTP-сервер приёма уведомлений ЮKassa
│   ├── handlers/
│   │   ├── user.py         # пользовательский роутер (~960 строк, витрина и клиентский флоу)
│   │   └── admin.py        # админский роутер (~990 строк, панель управления)
│   └── services/
│       ├── subs.py         # бизнес-логика подписок: выдача, продление, напоминания, воркер
│       ├── panels.py       # адаптер 3x-ui (API, инбаунды, генерация ссылок XRay)
│       ├── payments.py     # YooKassaClient — REST-клиент платежей
│       └── claims.py       # HMAC-токены claim-ссылок для ручной выдачи ключей
├── tools/
│   ├── selftest.py         # 127 офлайн-проверок логики (без сети и Telegram)
│   └── integration_test.py # прогон обновлений через диспетчер aiogram на фейковой БД
├── install.sh              # интерактивный установщик/обновщик/деинсталлятор (systemd)
├── Dockerfile, docker-compose.yml, .dockerignore
├── .env.example            # документация всех переменных окружения
├── requirements.txt / requirements.lock.txt / requirements-dev.txt
├── ruff.toml               # линтер (E,F,W,I,UP,B,SIM,C4,RET,PTH,RUF; line-length 110)
├── docs/, assets/, .github/
└── README.md, LICENSE
```

Зависимости приложения (`requirements.txt`): aiogram≥3.7,<4; aiohttp; aiosqlite; python-dotenv; qrcode[pil]; pypng.

---

## 3. Слои архитектуры

Проект строго трёхслойный; направление импортов только «сверху вниз»:

```
 Точка входа      bot.py
      │
 UI-слой          app/handlers/{user,admin}.py  →  app/keyboards.py, app/utils.py
      │            app/middlewares.py, app/webhook.py, app/health.py
 Бизнес-логика    app/services/{subs,panels,payments,claims}.py
      │            app/runtime.py
 Данные            app/db.py (SQLite), app/config.py (.env)
```

Ключевые принципы, видимые в коде:

- **Обработчики тонкие**: принимают update, вызывают `services.*`, рендерят текст через `keyboards.py`/`utils.py`.
- **Единственная точка доступа к SQL** — методы класса `Database` (~73 методов); хендлеры и сервисы не пишут SQL сами.
- **Панель выдачи изолирована** за абстракцией `BasePanel` (ABC) — реализация одна (`XuiPanel`), но интерфейс допускает другие панели; фабрика `get_panel(cfg, db)` + `close_panel()` при остановке.
- **Платёжный провайдер** изолирован за `YooKassaClient`; проверка платежа возможна и вебхуком, и поллингом (`poll_yookassa_orders`) — идемпотентность обеспечивает слой БД.
- **Конфигурация двухуровневая**: неизменяемый `Config` из `.env` (frozen dataclass) + изменяемые «горячие» настройки в `runtime` (пробный период правится админом в БД без рестарта; тарифы — через методы `Database`).

---

## 4. Модуль за модулем

### 4.1 `bot.py` — точка входа (216 строк)

| Элемент | Назначение |
|---|---|
| `setup_logging(level, log_file)` | stdout + `RotatingFileHandler` (5 МБ × 3), формат с `rid=%(request_id)s`; при отсутствии прав на запись — деградация до stdout |
| `set_commands(bot)` | регистр команд: /start, /menu, /help, /terms, /privacy |
| `build_dispatcher(cfg, db, bot, yk)` | кладёт `cfg/db/yk` в `dp[...]`, навешивает мидлвари (outer: Errors; message+callback: Throttling, User, RequiredChannel), подключает роутеры — **админский раньше пользовательского** |
| `main()` | load_config → validate (ошибки → SystemExit(2)) → `db.connect()` + миграции → `runtime.load(db, cfg)` → Bot/Dispatcher → опционально webhook-сервер и health-сервер (ошибка порта не фатальна) → `background_worker` как asyncio-task → проверка панели (`panel.health()`, алерт админам) → signal-handler'ы SIGINT/SIGTERM → `dp.start_polling(..., allowed_updates=resolve_used_update_types())` → в `finally`: отмена задач, cleanup webhook/health/yk/панели/БД/сессии |

### 4.2 `app/config.py` — конфигурация (293 строки)

- `load_config()` читает `.env` (dotenv) в frozen-датакласс `Config` c типизацией bool/int/float/list и валидными значениями по умолчанию.
- Группы настроек: токен/админы (`BOT_TOKEN`, `ADMIN_IDS`); бренд/тексты; канал обязательной подписки; тарифы по умолчанию; оплата (`YOOKASSA_*`, `PAYMENT_METHODS`); панель (`PANEL=xui`, `XUI_URL/LOGIN/PASSWORD`, inbounds, TLS: `XUI_VERIFY_SSL`, `XUI_CA_FILE`); вебхук (`WEBHOOK_ENABLED/HOST/PORT/PATH/PUBLIC_URL/TRUST_PROXY` — путь по умолчанию `/yookassa/webhook`); healthcheck; триал; напоминания; FSM-таймауты; throttling; режим выдачи (`ISSUE_MODE=auto|manual`, `CLAIM_SECRET`); пути (`DB_PATH`, `LOG_FILE`, `BACKUP_DIR`).
- `validate(cfg) -> list[str]` — человекочитаемый список проблем конфигурации (пустой = ок).

### 4.3 `app/runtime.py` — горячие настройки (39 строк)

Синглтон `runtime` (класс `Runtime`): `await runtime.load(db, cfg)` берёт значения из `.env`, а поверх накладывает правки админа из таблицы `settings` — сейчас это параметры пробного периода (`trial_enabled`, `trial_days`, метод `set_trial()`). Позволяет менять маркетинговые настройки без рестарта бота.

### 4.4 `app/db.py` — данные (817 строк)

- `Database(path)` на `aiosqlite`; `connect()` включает WAL и внешние ключи; `_migrate()` идемпотентно создаёт таблицы и досеивает тарифы (`_seed_plans`); primitives `execute/fetchone/fetchall/scalar`.
- Доменные группы методов:
  - **Пользователи:** `ensure_user`, `get_user`, `users_page`, `find_user_by_username`, `set_user_field`, `claim_trial`, `all_user_ids(audience)` (для рассылок).
  - **Тарифы:** `active_plans`, `upsert_plan`, `delete_plan`.
  - **Заказы:** `create_order`, `pending_order(s)`, `stale_orders`, `set_order_status`, промо-бронь (`set_order_promo`, `clear_order_promo_reservation`).
  - **Защита от двойной оплаты (последний коммит):** транзакционные методы `claim_order_invoice` / `release_order_invoice` / `claim_order_for_payment` — одному заказу гарантированно привязан ровно один активный счёт ЮKassa; отменённый счёт заменяется новым, повторное открытие чекаота отдаёт сохранённую `payment_url`.
  - **Подписки:** `create_subscription`, `claim_subscription` (атомарная выдача), `active_subscription`, `latest_subscription`, `update_subscription`, `subscriptions_expiring/expired`, поиск по `panel_username`.
  - **Промокоды:** `promo_valid`, `reserve_promo/apply_promo/release_promo` (бронь лимита использований на время оформления заказа).
  - **Тикеты поддержки, статистика, настройки, платежи.**
- Даты хранятся ISO-строками (UTC) — утилиты `utcnow/to_iso/from_iso`.

### 4.5 `app/middlewares.py` (163 строки)

| Мидлвар | Поведение |
|---|---|
| `ThrottlingMiddleware(rate)` | антифлуд per-user: минимальный интервал между апдейтами (`THROTTLING_RATE`, сек); частые нажатия просто молча игнорируются |
| `UserMiddleware(db,cfg)` | вызывает `db.ensure_user(...)` (регистрация/обновление пользователя), блокирует забаненных (кроме админов) с вежливым отказом, кладёт строку БД в `data["db_user"]` |
| `RequiredChannelMiddleware(bot,cfg)` | гейт обязательной подписки на канал; исключения по `SKIP_CHANNEL_CHECK` и regex claim-коллбэков `claim_<id>-<sig>` (чтобы claim работал без подписки); показывает кнопку «Проверить подписку» |
| `ErrorsMiddleware(cfg)` | catch-all: лог с traceback + уведомление админам, вместо падения хендлера |

### 4.6 `app/handlers/user.py` — клиентский флоу (963 строки)

Роутер покрывает: главное меню (`cmd_start/cmd_menu/cb_main_menu`), витрину тарифов (`_show_plans/cb_plans/cb_buy`), заказы (`_show_order/cb_cancel_order/cb_order_show`, `_is_owner` — защита чужих заказов), промокоды (`PromoStates`: ввод/применение к заказу), чекаут и оплату (`cb_pay`, `_render_checkout` —Methods: карта/СБП/SberPay), мои ключи/подписку (`cb_keys`, `cb_sub_view/copy/qr/howto/renew`), триал (`cb_trial`), профиль и историю заказов, поддержку через тикеты (`TicketStates`: тема → сообщение → ответы), FAQ/помощь, `/terms`, `/privacy`, а также «оконный» механизм `_apply_window/_remember_window` — редактирование/удаление предыдущего сообщения меню вместо спама чата.

### 4.7 `app/handlers/admin.py` — админ-панель (992 строки)

Фильтр `IsAdmin` (проверка `cfg.is_admin(user.id)` по `ADMIN_IDS`, стоит на весь роутер: `router.message.filter(IsAdmin())`) + `AdminStates`. Разделы: дашборд и статистика, тест панели, управление службой (перезапуск бота через отложенный выход процесса — systemd поднимет заново; restart xray), БД-бэкап в чат, заказы (подтвердить/отменить вручную), users (пагинация, поиск, карточка, бан, выдача подписки вручную, сообщение пользователю), промокоды (FSM-мастер создания, вкл/выкл, удаление), рассылки по аудитории, тикеты (ответ/закрытие), настройки (редактирование тарифов через `db` и триала через `runtime`).

### 4.8 `app/services/subs.py` — ядро бизнес-логики (431 строка)

- `grant_subscription(...)` — выдача/продление: вызывает панель, пишет подписку, шлёт карточку; учитывает `ISSUE_MODE` (auto/manual→claim-ссылка).
- `complete_order(...)` — идемпотентное завершение оплаченного заказа (вызывается и вебхуком, и поллингом, и ручным подтверждением админа).
- `renewal_window_days/renewal_available` — окна продления.
- Фоновый цикл `background_worker`: `process_expired` (истечение/блокировка клиента на панели), `send_reminders` (напоминания), `poll_yookassa_orders` (резервная проверка pending-платежей), `cancel_stale_orders` (автоотмена протухших заказов + освобождение брони), `run_db_backup` (ротационные бэкапы).
- `notify_admins/safe_send` — устойчивая доставка (не падает при Blocked/FloodWait).

### 4.9 `app/services/panels.py` — адаптер 3x-ui (713 строк)

ABC `BasePanel` (create/extend/set_enabled/delete/restart_xray/health/close/list_claimable — все методы `async`) + dataclass'ы `IssuedKey/ClaimedClient`. `XuiPanel`: сессия aiohttp с настраиваемым TLS (`_make_ssl_context`, CA-file), login + пере-логин по 401, generic `_api()`, два режима API — «clients API» (новые версии) и «classic» (патч inbound JSON) с автоопределением (`_uses_clients_api`); выбор inbounds, генерация ссылок протоколов (`_build_link`: vless/trojan/ss…), `list_claimable` для ручной выдачи. Фабрика-синглтон `get_panel(cfg, db)` / `close_panel()` (аргумент `db` оставлен для совместимости сигнатуры).

### 4.10 `app/services/payments.py` — ЮKassa (152 строки)

`YooKassaClient`: REST через basic-auth (shop_id/secret), `create_payment` (сумма в рублях → `amount.value="X.00"`, currency RUB, metadata с order_id, description, return_url), `get_payment`, статические предикторы `payment_is_valid/payment_is_done/payment_is_payable/is_paid` (проверка суммы и статуса — защита от подделки).

### 4.11 `app/services/claims.py` — claim-токены (179 строк)

HMAC-SHA256 подпись `sub_id|exp` секретом `CLAIM_SECRET`; `claim_token/parse_token/token_expired/build_claim_link` (deep-link `t.me/<bot>?start=claim_...`), `find_claimable` и `bind_claim` — привязка неподписанного клиента панели к новому владельцу с выдачей карточки; TTL токена и защита от чужих/просроченных токенов.

### 4.12 Инфраструктурные модули

- `webhook.py` — aiohttp-сервер приёма уведомлений ЮKassa (`payment.succeeded`). Телу уведомления **не доверяется**: сервер фильтрует источник по CIDR-диапазонам IP ЮKassa (`YOOKASSA_NETS`, X-Forwarded-For учитывается только при `WEBHOOK_TRUST_PROXY=true`), затем переспрашивает платёж через API (`get_payment`) и выдаёт доступ только при реальном `succeeded` и совпадении суммы, вызывая идемпотентный `complete_order`. Ошибки выдачи → 500/503, чтобы ЮKassa повторила доставку.
- `health.py` — `GET /healthz`: living-проверка БД + аптайм; для внешних мониторов (UptimeRobot и т.п.).
- `logctx.py` — `RequestIdFilter`: контекстный `rid=` для корреляции логов.
- `utils.py` — `esc` (HTML-экран), `fmt_dt/human_left/price/human_gb`, `make_qr_png`, `pick_link`, `apps_instructions` (V2RayNG/StreetsOfRage/Shadowrocket/Hiddify), `chunks`.
- `keyboards.py` — все разметки; константы кнопок и текстов меню собраны здесь.

---

## 5. Основные сквозные потоки

### 5.1 Покупка с оплатой (auto-issue)

```
/start → главное меню → 🛒 Купить → cb_plans (витрина тарифов)
  → cb_buy(plan) → create_order (+резерв промо) → окно заказа
  → выбор способа → cb_pay:
       db.claim_order_for_payment(order)            ← ровно один счёт на заказ
       yk.create_payment(...)                       ← счёт ЮKassa
       db.claim_order_invoice(order, payment_id, confirmation_url)
       _render_checkout (ссылка + кнопки «Я оплатил», «Отмена»)
  ← вебхук payment.succeeded ИЛИ poll_yookassa_orders ИЛИ ручное подтверждение админа
       subs.complete_order(order_id)                ← идемпотентно
         → panels.create_client(days/devices/traffic)
         → db.claim_subscription(...)
         → send_subscription_card (ключ, ссылка, QR, инструкция)
```

Отказ/таймаут → `cancel_stale_orders` отменяет заказ, освобождает invoice и бронь промо.

### 5.2 Ручная выдача (ISSUE_MODE=manual, claim-ссылка)

```
Админ/воркер создаёт клиента на панели → build_claim_link(sub_id)
→ клиент открывает t.me/bot?start=claim_<id>-<sig>
→ UserMiddleware пропускает мимо channel-gate → bind_claim(user_id, token):
     parse_token + HMAC-проверка + TTL → db.claim_subscription → карточка подписки
Чужой/просроченный токен — вежливый отказ.
```

### 5.3 Фоновый воркер (цикл по таймерам)

истечение подписок (disable клиента на панели) → напоминания о продлении → поллинг платежей → автоотмена старых заказов → ночной бэкап БД.

---

## 6. Схема данных (SQLite, WAL)

Схема объявлена одним SQL-блоком в начале `db.py` и идемпотентно применяется в `_migrate()`. Таблицы (11 шт.):

| Таблица | Назначение / ключевые поля |
|---|---|
| `users` | telegram_id PK, username, full_name, trial_used, is_banned, created_at, last_seen_at |
| `plans` | код тарифа, title, цена, days, devices, traffic_gb, active |
| `orders` | пользователь, план, amount/base_amount, promo_code, статус, method, payment_id, payment_url |
| `subscriptions` | user, plan, vpn_key, sub_url, panel, panel_username, expires_at, enabled |
| `promos` + `promo_uses` | промокод (тип/value/max_uses/active) и учёт использований по пользователям (бронь) |
| `tickets` + `ticket_messages` | обращения поддержки и переписка |
| `payments_log` | журнал платежей ЮKassa (привязка платёжа к заказу) |
| `broadcasts` | история рассылок |
| `settings` | ключ-значение для «горячего» `runtime` |

Временные метки — ISO UTC-строки (`utcnow/to_iso/from_iso`).

---

## 7. Качество, тесты, инструменты

- **Линтинг:** `ruff.toml` (E,F,W,I,UP,B,SIM,C4,RET,PTH,RUF; py311; line-length 110) — сейчас чисто.
- **selftest.py (786 строк):** офлайн-проверки логики без сети: конфиг, БД-запросы, промо-математика, идемпотентность complete_order, claim-токены, форматтеры, оконный механизм меню, защита от дублей счетов ЮKassa (секция 15) — **127 проверок**.
- **integration_test.py (458 строк):** строит настоящий `Dispatcher` с фейковыми Bot/API и in-memory БД, прогоняет последовательности апдейтов (меню→покупка→оплата→выдача, claim-флоу) через `dp.feed_update`.
- **CI (.github):** workflow запускает линтер и оба теста (см. `.github/workflows`).

---

## 8. Наблюдения (для будущего рефакторинга)

1. `handlers/user.py` и `handlers/admin.py` крупнее остальных слоёв (~1k строк каждый) — логично разнести по под-роутерам (shop / orders / subscriptions / support; admin: users / promos / tickets / settings).
2. Часть текстов сообщений захардкожена в хендлерах — вынос в `keyboards.py`/словарь i18n упростит брендинг.
3. Панель и платежи полностью асинхронные (aiohttp); сетевые таймауты зашиты константами в модулях `panels.py`/`payments.py` — их вынос в `.env` упростил бы тюнинг под медленные панели.
4. Схема БД версионируется «на глаз» через `_migrate()` — таблица `schema_version` сделала бы миграции явными.

Ничего из этого не является дефектом текущего состояния: сборка, линтер и оба тестовых набора проходят полностью.
