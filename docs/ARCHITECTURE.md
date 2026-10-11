# Архитектура и реконструкция OPVPN

Документ описывает текущую архитектуру проекта (результат аудита и реконструкции кода, октябрь 2026) и правила, которым код должен следовать.

## 1. Обзор

**OPVPN** (`app/__init__.py: __version__ = "2.0.0"`) — Telegram-бот для продажи VPN-подписок на **aiogram 3** (long-polling) с хранением состояния в **SQLite** (`aiosqlite`, WAL).

Слои:

```
bot.py                     — точка входа: конфиг → БД → диспетчер → polling + фоновый воркер
app/config.py              — неизменяемая конфигурация из .env (dataclass Config, frozen)
app/runtime.py             — изменяемые «на лету» настройки (триал, реф-%), поверх .env
app/db.py                  — единственный слой доступа к данным (класс Database)
app/keyboards.py           — все клавиатуры (чистые функции, без состояния)
app/utils.py               — форматирование, QR, время (без I/O)
app/middlewares.py         — троттлинг, регистрация/бан, обязательный канал, ошибки
app/handlers/user.py       — пользовательский Router (магазин, оплата, ключи, профиль, тикеты)
app/handlers/admin.py      — админский Router (фильтр IsAdmin на уровне роутера)
app/services/panels.py     — выдача ключей: BasePanel → PoolPanel / XuiPanel / MarzbanPanel
app/services/payments.py   — YooKassaClient, YooMoneyClient, звёзды (send_invoice/refund)
app/services/subs.py       — бизнес-логика: grant/complete/expired/reminders + background_worker
app/webhook.py             — aiohttp-сервер уведомлений ЮKassa (опционально)
tools/selftest.py          — проверки логики без сети (100+, идемпотентность, вебхук, бэкапы, claim)
tools/integration_test.py  — маршрутизация апдейтов через Dispatcher.feed_update
install.sh / deploy/       — установка на VPS: systemd-служба dfvpnbot (install.sh --update/--uninstall/--no-service)
Dockerfile / docker-compose.yml — контейнерный запуск (непривилегированный пользователь, том под data/)
requirements-dev.txt       — инструменты разработки (ruff, pytest)
```

Правило зависимостей: `handlers → services → db/config/utils`; сервисы не импортируют handlers; `config`/`db` не импортируют ничего выше себя. Единственный цикл, допустимый исторически: `middlewares → services.subs` (для `notify_admins`).

## 2. Поток данных

1. `load_config()` читает `.env` (python-dotenv с ручным fallback), возвращает frozen `Config`. `validate(cfg)` печатает список проблем и `SystemExit(2)`.
2. `Database.connect()` создаёт схему (`SCHEMA`), сидирует `DEFAULT_PLANS`. Одна долгоживущая коннекция + `asyncio.Lock`; хелперы `execute/fetchone/fetchall/scalar`.
3. `runtime.load(db, cfg)` накладывается поверх `.env`: значения из таблицы `settings` (правки админки) приоритетнее.
4. `build_dispatcher()` кладёт `cfg/db/yk/ym/bot_username` в `dp` (workflow data) и подключает outer-middleware `ErrorsMiddleware`, затем `Throttling → User → RequiredChannel` на messages и callbacks. Роутеры: сначала `admin`, затем `user` (последний содержит перехватчик `adm:*` → «недостаточно прав»).
5. Фоновый `background_worker` (цикл 180 с): `process_expired → send_reminders → poll_yookassa_orders → poll_yoomoney_orders → cancel_stale_orders`.
6. Остановка: SIGINT/SIGTERM → `stop_event` → `dp.stop_polling()`, в `finally` закрываются webhook-runner, платёжные клиенты, панель, БД, сессия бота.

## 3. Модель БД (SQLite)

| Таблица | Назначение | Ключевые поля |
|---|---|---|
| `users` | клиенты | telegram_id PK, balance (коп.), referrer_id, ref_earned, trial_used, is_banned |
| `plans` | витрина | code PK, price, days, traffic_gb (0 = безлимит), devices, is_active, sort_order |
| `orders` | заказы/пополнения | kind ∈ subscription/topup, status ∈ pending/paid/canceled/refunded, method, payment_id, base_amount/discount/promo_code |
| `subscriptions` | выданные ключи | vpn_key, sub_url, panel, panel_username, expires_at (ISO UTC), status, notified («3d,1d») |
| `key_pool` | пул конфигов | content UNIQUE, plan_code (NULL = любой), is_used/used_by |
| `promos`, `promo_uses` | промокоды | discount_type percent/fixed, max_uses, expires_at |
| `tickets`, `ticket_messages` | поддержка | status open/closed, from_admin |
| `payments_log` | история денег | user_id, amount, method, kind, note |
| `broadcasts` | журнал рассылок | audience, sent/failed |
| `settings` | рантайм-настройки | key/value |

Время везде хранится как ISO-8601 UTC (`to_iso/from_iso`, микросекунды отбрасываются).

## 4. Панели выдачи (Strategy)

`BasePanel` — контракт: `create_client / extend_client / set_enabled / delete_client / health / close`. Возвращает `IssuedKey(vpn_key, sub_url, panel, panel_username)`.

* **PoolPanel** — выдаёт готовый конфиг из `key_pool` (приоритет: под тариф → plan_code IS NULL → любой свободный). Продление — только в БД.
* **XuiPanel (3x-ui)** — логин (`/login` cookie-сессия или Bearer-токен), `/panel/api/inbounds/addClient|updateClient|delClient`; ссылка vless/vmess/trojan собирается `_build_link()` из streamSettings (reality/tls/ws/grpc/httpupgrade/xhttp). Поддерживает несколько inbound'ов (`XUI_INBOUND_IDS`): один клиент с общим `email`/`subId` создаётся сразу во всех, а панель собирает их в одну подписку `/sub/{subId}` (актуальные сборки через `/panel/api/clients/*`).
* **MarzbanPanel** — OAuth-токен `/api/admin/token`, CRUD `/api/user`, повтор авторизации при 401/403, продление через PUT `expire/status`.
* Синглтон панели: `get_panel(cfg, db)` / `close_panel()` (сбрасывается при остановке).

Кастомная панель: наследник `BasePanel` + регистрация в `get_panel()`.

### Жизненный цикл подписки (`grant_subscription`)

При оплате/выдаче бот решает, что делать, в таком порядке:

1. **Активна подписка той же панели** → `extend_client` (тот же клиент, срок сдвигается вперёд).
2. **Активной нет** → берётся последняя **истёкшая** подписка той же панели
   (`db.latest_subscription(user_id, panel, status="expired")`) и возобновляется тем
   же клиентом: `extend_client` + `set_enabled(True)`. Срок считается от
   `max(прошлое окончание, сейчас)`, чтобы простой не «съедал» новый период.
3. **Клиента продлить не удалось** (например, удалён в панели: `extend_client` вернул
   `False`) → создаётся новый клиент (`create_client`).

Так повторная покупка после истечения возвращает пользователю **тот же конфиг**
(в `pool` — тот же ключ), а не плодит нового клиента в панели. Статусы подписки:
`active` → `expired` (истечение в `process_expired`) → снова `active` при возобновлении;
`disabled` — админский отзыв (`adm:user:wipe`, клиент удаляется в панели).


## 5. Платежи

* **ЮKassa** (`YooKassaClient`): REST v3, Basic Auth, идемпотентность через `Idempotence-Key`; подтверждение **только** через `GET /payments/{id}` (status=succeeded + сверка суммы ±0.01) — телу вебхука не доверяем (`app/webhook.py` + IP-allowlist сетей ЮKassa). Без вебхуков — опрос раз в 3 минуты и кнопка «Проверить оплату».
* **ЮMoney** (`YooMoneyClient`): форма quickpay с `label=dfvpn-order-{id}`; автоподтверждение поиском входящей операции в operation-history по label и сумме. Домен не нужен.
* **Telegram Stars**: `send_invoice(currency="XTR", provider_token="")`, payload `order:{id}`; обработка `pre_checkout_query` / `successful_payment`; возврат — `refund_star_payment`.
* **Баланс**: атомарный `UPDATE users SET balance = balance - ?`, при сбое выдачи — автоматический возврат.
* **Manual**: заказ создаётся, админ подтверждает через `adm:order:approve` → `complete_order(payment_id="manual:{admin_id}")`.

Единая точка завершения — `complete_order()`: помечает paid → начисляет топ-ап или выдаёт подписку (`grant_subscription`) → реферальный бонус → уведомления админам. Идемпотентна по статусу заказа.

## 6. Состояния FSM (aiogram StatesGroup)

* Пользователь: `PromoStates.enter`, `TopupStates.custom_amount`, `TicketStates.subject/message/reply`.
* Админ: `AdminStates.*` (добавление ключей, мастер промокода из 5 шагов, рассылка, поиск/сообщение/баланс/выдача пользователя, ответ в тикет, редактирование тарифа, триал, реф-%).

## 7. Известные технические долги (зафиксированы при реконструкции)

1. **Дублирование** `sub:view/copy/qr/howto` в `msg_my_keys` и `show_subscription` — требует унификации callback'ов (`sub:*` принимает и id подписки, и order_id временного ключа).
2. ~~**Нет транзакций** в критичных местах~~ — **исправлено**: `complete_order` использует атомарный `db.claim_order_for_payment` (pending → paid под `_lock`, двойная выдача невозможна); `take_key` — один `UPDATE ... RETURNING`; `add_balance` и `apply_promo` выполняются под одним захватом `_lock` с единым коммитом. Покрыто проверками 64–71 в `tools/selftest.py` (раздел «Конкурентность и идемпотентность выдачи»).
3. ~~`poll_yookassa_orders` каждый цикл создаёт новый `YooKassaClient`~~ — **исправлено**: фоновый воркер получает переиспользуемый клиент из `bot.py` (`background_worker(..., yk=yk)` → `poll_yookassa_orders(..., client=yk)`); локальный клиент создаётся только как fallback.
4. В `user.py` остались заглушки «Оплата пока недоступна» для `buy:yoomoney:` / `buy:stars:` / `buy:balance:` / `buy:manual:` — основной путь оплаты идёт через `pay:<method>:<order_id>`.
5. Прямые SQL из хендлеров (например, `SELECT * FROM subscriptions WHERE id = ?`) в обход методов `Database`.
6. `required_channels` проверяются через `get_chat_member` с кэшем 120 c; при ошибке API — «разрешить» (fail-open, осознанно).

## 8. Как менять код безопасно

* Любая правка — после `python tools/selftest.py` (71 проверка) и `python tools/integration_test.py`.
* Новые кнопки: колбэк ≤ 64 байт, префикс `домен:действие[:arg]`; новые префиксы добавлять в соответствующий Router раньше catch-all.
* Новые поля БД: миграция через `ALTER TABLE ... ADD COLUMN` внутри `Database.connect()` (idempotent), плюс белый список в `set_user_field/update_subscription`.
* Деньги: любые изменения списаний/начислений — только через `Database` +Ideмпотентность `complete_order` сохранять обязательно.
