# Архитектура и реконструкция OPVPN

Документ описывает текущую архитектуру проекта (результат аудита и реконструкции кода, октябрь 2026) и правила, которым код должен следовать.

## 1. Обзор

**OPVPN** (`app/__init__.py: __version__ = "2.0.0"`) — Telegram-бот для продажи VPN-подписок на **aiogram 3** (long-polling или вебхук Telegram) с хранением состояния в **SQLite** (`aiosqlite`, WAL).

Слои:

```
bot.py                     — точка входа: конфиг → БД → диспетчер → транспорт (вебхук/polling) + фоновый воркер
app/config.py              — неизменяемая конфигурация из .env (dataclass Config, frozen)
app/runtime.py             — изменяемые «на лету» настройки (триал, реф-%), поверх .env
app/db.py                  — единственный слой доступа к данным (класс Database)
app/keyboards.py           — все клавиатуры (чистые функции, без состояния)
app/utils.py               — форматирование, QR, время (без I/O)
app/middlewares.py         — троттлинг, регистрация/бан, обязательный канал, ошибки
app/handlers/user.py       — пользовательский Router (магазин, оплата, ключи, профиль, тикеты)
app/handlers/admin.py      — админский Router (фильтр IsAdmin на уровне роутера)
app/services/panels.py     — выдача ключей: BasePanel → XuiPanel (3x-ui)
app/services/payments.py   — YooKassaClient (REST v3: создание платежа, проверка статуса)
app/services/subs.py       — бизнес-логика: grant/complete/expired/reminders + background_worker
app/webhook.py             — aiohttp-сервер вебхуков: уведомления ЮKassa + апдейты Telegram (опционально)
app/health.py              — aiohttp-сервер GET /healthz для внешнего мониторинга (uptime)
app/logctx.py              — request_id (contextvars) для корреляции логов одного апдейта
tools/selftest.py          — проверки логики без сети (100+, идемпотентность, вебхук, бэкапы, claim)
tools/integration_test.py  — маршрутизация апдейтов через Dispatcher.feed_update
install.sh / deploy/       — установка на VPS: systemd-служба opvpnbot (install.sh --update/--uninstall/--no-service)
Dockerfile / docker-compose.yml — контейнерный запуск (непривилегированный пользователь, том под data/)
requirements-dev.txt       — инструменты разработки (ruff, pytest)
```

Правило зависимостей: `handlers → services → db/config/utils`; сервисы не импортируют handlers; `config`/`db` не импортируют ничего выше себя. Единственный цикл, допустимый исторически: `middlewares → services.subs` (для `notify_admins`).

## 2. Поток данных

1. `load_config()` читает `.env` (python-dotenv с ручным fallback), возвращает frozen `Config`. `validate(cfg)` печатает список проблем и `SystemExit(2)`.
2. `Database.connect()` создаёт схему (`SCHEMA`), сидирует `DEFAULT_PLANS`. Одна долгоживущая коннекция + `asyncio.Lock`; хелперы `execute/fetchone/fetchall/scalar`.
3. `runtime.load(db, cfg)` накладывается поверх `.env`: значения из таблицы `settings` (правки админки) приоритетнее.
4. `build_dispatcher()` кладёт `cfg/db/yk/bot_username` в `dp` (workflow data) и подключает outer-middleware `ErrorsMiddleware`, затем `Throttling → User → RequiredChannel` на messages и callbacks. Роутеры: сначала `admin`, затем `user` (последний содержит перехватчик `adm:*` → «недостаточно прав»).
5. Фоновый `background_worker` (цикл 180 с): `process_expired → send_reminders → poll_yookassa_orders → cancel_stale_orders`.
5a. **Транспорт апдейтов** (`TG_WEBHOOK_ENABLED`, по умолчанию выключен):
   - *выключен* — `dp.start_polling()`; на старте вызывается `bot.delete_webhook()`,
     иначе `getUpdates` падает с «Conflict: can't use getUpdates method while webhook is active»;
   - *включён* — HTTP-сервер вебхуков регистрирует маршрут `TG_WEBHOOK_URL` (его путь),
     бот вызывает `set_webhook(url, secret_token, allowed_updates)` и ждёт `stop_event`
     (вебхук намеренно НЕ снимается при остановке: Telegram переживёт перезапуск службы);
   - `TG_WEBHOOK_FAST_ACK=true` — при верном заголовке `X-Telegram-Bot-Api-Secret-Token`
     отвечаем 200 и обрабатываем апдейт в фоне (`_spawn_update`), чтобы долгая выдача
     в панели (до 20 с) не приводила к повторной доставке; при остановке бота фоновые
     задачи дожидаются `drain_pending_updates()`;
   - при `fast_ack=false` обработка синхронная (ошибка → 500, Telegram повторит);
   - если `set_webhook` не удался (битый URL/сертификат) — автоматический откат на polling.
6. Остановка: SIGINT/SIGTERM → `stop_event` → `dp.stop_polling()` (только в режиме polling), в `finally` дожидаются фоновые апдейты, закрываются health-runner, webhook-runner, платёжные клиенты, панель, БД, сессия бота.

## 3. Модель БД (SQLite)

| Таблица                      | Назначение        | Ключевые поля                                                                                                     |
| ---------------------------- | ----------------- | ----------------------------------------------------------------------------------------------------------------- |
| `users`                      | клиенты           | telegram_id PK, trial_used, is_banned                                                                             |
| `plans`                      | витрина           | code PK, price, days, traffic_gb (0 = безлимит), devices, is_active, sort_order                                   |
| `orders`                     | заказы            | kind = subscription, status ∈ pending/paid/canceled/refunded, method, payment_id/payment_ids (история счетов), base_amount/discount/promo_code |
| `subscriptions`              | выданные ключи    | vpn_key, sub_url, panel, panel_username, expires_at (ISO UTC), status, notified («3d,1d»); активная — ровно одна на пользователя (`idx_subs_one_active`) |
| `promos`, `promo_uses`       | промокоды         | discount_type percent/fixed, max_uses, expires_at                                                                 |
| `tickets`, `ticket_messages` | поддержка         | status open/closed, from_admin                                                                                    |
| `payments_log`               | история денег     | user_id, amount, method, kind, note                                                                               |
| `broadcasts`                 | журнал рассылок   | audience, sent/failed                                                                                             |
| `settings`                   | рантайм-настройки | key/value                                                                                                         |

Время везде хранится как ISO-8601 UTC (`to_iso/from_iso`, микросекунды отбрасываются).

## 4. Панели выдачи (Strategy)

`BasePanel` — контракт: `create_client / extend_client / set_enabled / delete_client / restart_xray / health / close`. Возвращает `IssuedKey(vpn_key, sub_url, panel, panel_username)`.

- **XuiPanel (3x-ui)** — логин (`/login` cookie-сессия или Bearer-токен), `/panel/api/inbounds/addClient|updateClient|delClient`; ссылка vless/vmess/trojan собирается `_build_link()` из streamSettings (reality/tls/ws/grpc/httpupgrade/xhttp). Поддерживает несколько inbound'ов (`XUI_INBOUND_IDS`): один клиент с общим `email`/`subId` создаётся сразу во всех, а панель собирает их в одну подписку `/sub/{subId}` (актуальные сборки через `/panel/api/clients/*`). Проверка TLS-сертификата панели включена по умолчанию (`XUI_VERIFY_SSL`); для self-signed — свой CA через `XUI_CA_FILE`, полное отключение — только явным `XUI_VERIFY_SSL=false`.
- Синглтон панели: `get_panel(cfg, db)` / `close_panel()` (сбрасывается при остановке).

Кастомная панель: наследник `BasePanel` + регистрация в `get_panel()`.

### Жизненный цикл подписки (`grant_subscription`)

При оплате/выдаче бот решает, что делать, в таком порядке:

1. **Активна подписка той же панели** → `extend_client` (тот же клиент, срок сдвигается вперёд).
2. **Активной нет** → берётся последняя **истёкшая** подписка той же панели
   (`db.latest_subscription(user_id, panel, status="expired")`) и возобновляется тем
   же клиентом: `extend_client` + `set_enabled(True)`. Срок считается от
   `max(прошлое окончание, сейчас)`, чтобы простой не «съедал» новый период.
3. **Клиента продлить не удалось** (например, удалён в панели: `extend_client` вернул
   `False`) → **перевыдача**: создаётся новый клиент (`create_client`), но его данные
   пишутся в **ту же строку** `subscriptions` (`_apply_reissue`) — второй подписки у
   пользователя не появляется, а конфиг заменяется рабочим. Пользователю уходит
   карточка с заголовком «♻️ Подписка перевыдана» (конфиг сменился — нужно
   переимпортировать ссылку/QR).

**Инвариант «одна активная подписка на клиента»** закреплён и на уровне БД: частичный
уникальный индекс `idx_subs_one_active ON subscriptions(user_id) WHERE status='active'`.
Перед его созданием `_migrate()` сводит возможные дубликаты (оставляя ту подписку, что
истекает позже). `db.create_subscription()` при конфликте возвращает `0`, и выдача
переходит в перевыдачу в существующую строку. Продление, когда активна подписка
другой панели, запрещено (защитный `PanelError`).

Так повторная покупка после истечения возвращает пользователю **тот же конфиг**,
а не плодит нового клиента в панели. Статусы подписки:
`active` → `expired` (истечение в `process_expired`) → снова `active` при возобновлении;
админский отзыв (`adm:user:wipe`) удаляет подписку полностью — все строки
(включая `expired`) из БД и клиенты из панели; триал-флаг и история заказов остаются.

## 5. Платежи

- **ЮKassa** (`YooKassaClient`): REST v3, Basic Auth, идемпотентность через `Idempotence-Key`; подтверждение **только** через `GET /payments/{id}` (status=succeeded + сверка суммы ±0.01) — телу вебхука не доверяем (`app/webhook.py` + IP-allowlist сетей ЮKassa). Без вебхуков — опрос раз в 3 минуты и кнопка «Проверить оплату».
  - **Один заказ — все его счета видны**: выставленные счета копятся в `orders.payment_ids`; вебхук, поллер и кнопка «Проверить оплату» ищут заказ по любому из них, а если счёт «отвязался» от заказа — по `metadata.order_id` платежа (с алертом админам).
  - **Повторное нажатие «Оплатить» по уже оплаченному заказу** не выставляет новый счёт: пользователю отвечают «✅ Заказ уже оплачен». При сбое чтения счёта в ЮKassa новый счёт тоже не создаётся (просим повторить через минуту) — иначе оплата по первому счёту потерялась бы.
  - **«Одно окно» заказа**: у пользователя не более одного незакрытого заказа. Пока он есть, «Купить» показывает окно именно этого заказа (а не список тарифов), а тариф меняется **внутри того же заказа** (`order:plans:` → `switch:<code>` → `db.update_order_plan`) — второй заказ не создаётся. Промокод при смене тарифа переносится с пересчётом скидки от новой базовой цены. Если счёт уже выставлен, смена тарифа и применение промокода **запрещены** (изменение суммы сломало бы сверку оплаты — просим сначала отменить заказ). Устаревший незакрытый заказ при оплате переводит на самый свежий (`user_pending_order`). (`MAX_PENDING_ORDERS` остался как запасной лимит.)
- **Ручное подтверждение админом**: ожидающий заказ можно провести через `adm:order:approve` → `complete_order(payment_id="admin:{admin_id}")`.

Единая точка завершения — `complete_order()`: помечает paid → выдаёт подписку (`grant_subscription`) → уведомления админам. Идемпотентна по статусу заказа.

## 6. Состояния FSM (aiogram StatesGroup)

- Пользователь: `PromoStates.enter`, `TicketStates.subject/message/reply`.
- Режим одного окна: подсказки и результаты ввода правят то же сообщение — его id хранится в FSM-данных под ключом `ui_window_id` (хелперы `_prompt_window`/`_apply_window` в `app/handlers/user.py`). Сейчас покрыты пользовательские потоки (промокод, поддержка); админские мастера пока шлют новые сообщения.
- Экраны подписки: у клиента активная подписка ровно одна, поэтому «🔑 Моя подписка» при единственной записи открывает карточку сразу (`_render_sub_card`), а список (`keys_kb`) показывается только если записей больше одной.
- Админ: `AdminStates.*` (мастер промокода из 5 шагов, рассылка, поиск/сообщение/выдача пользователя, ответ в тикет, редактирование тарифа, дни триала, привязка клиента панели).

## 7. Известные технические долги (зафиксированы при реконструкции)

1. ~~**Дублирование** `sub:view/copy/qr/howto` в `msg_my_keys` и `show_subscription`~~ — **неактуально**: отдельного `msg_my_keys`/`show_subscription` в коде нет, подэкраны подписки идут через общие хелперы `_load_own_sub`/`_sub_back_kb`.
2. ~~**Нет транзакций** в критичных местах~~ — **исправлено**: `complete_order` использует атомарный `db.claim_order_for_payment` (pending → paid под `_lock`, двойная выдача невозможна); `take_key` — один `UPDATE ... RETURNING`; `apply_promo` выполняется под одним захватом `_lock` с единым коммитом. Покрыто проверками 64–71 в `tools/selftest.py` (раздел «Конкурентность и идемпотентность выдачи»).
3. ~~`poll_yookassa_orders` каждый цикл создаёт новый `YooKassaClient`~~ — **исправлено**: фоновый воркер получает переиспользуемый клиент из `bot.py` (`background_worker(..., yk=yk)` → `poll_yookassa_orders(..., client=yk)`); локальный клиент создаётся только как fallback.
4. ~~Прямые SQL из хендлеров (например, `SELECT * FROM subscriptions WHERE id = ?`) в обход методов `Database`~~ — **исправлено**: SQL вынесен в методы `Database` (`get_subscription`, `set_order_promo`, `set_order_payment`, `clear_order_promo_reservation`, `count_users`, `users_page`, `find_user_by_username`).
5. `required_channels` проверяются через `get_chat_member` с кэшем 120 c; при ошибке API — «разрешить» (fail-open, осознанно).

## 8. Как менять код безопасно

- Любая правка — после `python tools/selftest.py` (71 проверка) и `python tools/integration_test.py`.
- Новые кнопки: колбэк ≤ 64 байт, префикс `домен:действие[:arg]`; новые префиксы добавлять в соответствующий Router раньше catch-all.
- Новые поля БД: миграция через `ALTER TABLE ... ADD COLUMN` внутри `Database.connect()` (idempotent), плюс белый список в `set_user_field/update_subscription`.
- Версия: `app.__version__` — semver-релиз, бампается вручную (`git tag vX.Y.Z`); фактическая сборка — `app.__build__` (env `APP_VERSION` → `git describe` → `__version__`). Значение читается на импорте, поэтому env должен быть установлен до старта процесса (systemd `EnvironmentFile`, Docker `ENV`); `install.sh` пишет `APP_VERSION` в `.env` при каждом запуске, включая `--update`.
- Деньги: любые изменения списаний/начислений — только через `Database` +Ideмпотентность `complete_order` сохранять обязательно.
