# Настройка оплаты: ЮKassa и ручная оплата

Список активных способов задаётся в `.env`:

```env
PAYMENT_METHODS=yookassa          # карты РФ/СБП/SberPay
PAYMENT_METHODS=yookassa,manual   # + ручной перевод с подтверждением админом
PAYMENT_METHODS=manual            # только ручная оплата
```

Уберите лишние — кнопки исчезнут из бота. Если ЮKassa включена, но ключи не заполнены,
бот сообщит об этом при старте и не запустится (защита от «молчаливых» ошибок).

## Ручная оплата (`manual`)

Никаких ключей не нужно: клиент выбирает «🧾 Оплачу вручную», бот показывает текст
инструкции из `MANUAL_PAYMENT_NOTE` (реквизиты или «напишите в поддержку»), а после
перевода клиент отправляет чек в поддержку. Админ открывает `/admin` → **💰 Заказы**,
проверяет перевод и нажимает «✅ Подтвердить оплату» — подписка выдаётся автоматически.

```env
MANUAL_PAYMENT_NOTE=Перевод на карту 0000 0000 0000 0000, получатель Иван И.
```

Заказ получает способ `manual`, в журнал платежей пишется сумма, а в `payment_id` —
`admin:<telegram_id>` подтвердившего админа.

---

## 1. ЮKassa — карты РФ, СБП, SberPay

### Шаг 1. Получите доступы

1. Зарегистрируйте магазин на [yookassa.ru](https://yookassa.ru) (можно тестовый).
2. Личный кабинет → **Интеграция → Ключи API**: скопируйте `shopId` и **секретный ключ**.
3. Впишите их в `.env`:

```env
YOOKASSA_SHOP_ID=123456
YOOKASSA_SECRET_KEY=live_xxxxxxxxxxxxxxxxxxxx
YOOKASSA_RETURN_URL=https://t.me/your_bot_username   # куда вернуть пользователя после оплаты
```

Тестовые ключи (`test_...`) работают с `YOOKASSA_TEST=true` — удобно проверить поток без реальных денег.

### Шаг 2. Выберите способ подтверждения оплат

**Вариант А. Без домена (проще).** Ничего дополнительно настраивать не нужно:
бот сам опрашивает статус платежей каждые 3 минуты, плюс пользователь может нажать
«🔄 Проверить оплату» в карточке заказа. Оплата зачисляется в течение пары минут.

**Вариант Б. Вебхуки (мгновенно, рекомендуется при наличии домена).**

1. В `.env`:

```env
WEBHOOK_ENABLED=true
WEBHOOK_PORT=8080
WEBHOOK_PATH=/yookassa/webhook
WEBHOOK_PUBLIC_URL=https://bot.example.com/yookassa/webhook
WEBHOOK_TRUST_PROXY=true   # бот за nginx — обязательно, иначе все вебхуки получат 403
```

2. Поставьте nginx и HTTPS-сертификат (если ещё не):

```bash
sudo apt update && sudo apt install -y nginx certbot python3-certbot-nginx
sudo certbot --nginx -d bot.example.com
```

3. Проксируйте вебхук (и `/healthz` для мониторинга) на локальные порты бота —
   `/etc/nginx/sites-available/opvpnbot`:

```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;

    ssl_certificate     /etc/letsencrypt/live/bot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.example.com/privkey.pem;

    # ЮKassa → бот: мгновенная выдача доступа после оплаты
    location /yookassa/webhook {
        proxy_pass http://127.0.0.1:8080/yookassa/webhook;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # X-Forwarded-For ОБЯЗАТЕЛЕН при WEBHOOK_TRUST_PROXY=true,
        # иначе бот не увидит реальный IP ЮKassa и посчитает это подделкой.
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
    }

    # Healthcheck для внешнего uptime-монитора
    location = /healthz {
        proxy_pass http://127.0.0.1:8081/healthz;
        proxy_set_header Host $host;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/opvpnbot /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

4. В кабинете ЮKassa: **Интеграция → HTTP-уведомления** → добавьте событие `payment.succeeded`
   с адресом `https://bot.example.com/yookassa/webhook`.
5. Перезапустите бота: `systemctl restart opvpnbot`. Проверьте healthcheck:
   `curl -s https://bot.example.com/healthz` (и локально — `curl -s http://127.0.0.1:8081/healthz`).

### Как бот защищается от подделки уведомлений

Телу вебхука не доверяем: получив `payment.succeeded`, бот **переспрашивает платёж**
через `GET /v3/payments/{id}` и проверяет, что:

- статус `succeeded` и `paid=true`;
- сумма совпадает с суммой заказа (±1 копейка).

Только после этого выдаётся доступ. Если обработка не удалась, бот отвечает `500`,
и ЮKassa повторит уведомление. Есть и вспомогательная проверка IP по официальным
диапазонам ЮKassa (`app/webhook.py`).

### Возвраты

Возврат оформляется в кабинете ЮKassa. Отметьте заказ отменённым в админке
(💰 Заказы) или выдайте доступ вручную — бот не списывает подписку автоматически.

### VPS за рубежом: ЮKassa недоступна (частый случай)

Если сервер стоит не в РФ, исходящие запросы к `api.yookassa.ru` нередко «зависают»:
вебхук отдаёт `504`, а кнопка «🔄 Проверить оплату» падает с `asyncio.TimeoutError`.
При этом Telegram работает (поллинг жив), потому что проблема — только в маршруте
до российского платёжного сервиса.

Проверьте на сервере:

```bash
getent ahosts api.yookassa.ru
for ip in $(getent ahosts api.yookassa.ru | awk '/STREAM/{print $1}'); do
  curl -sS --max-time 8 --resolve api.yookassa.ru:443:$ip -o /dev/null \
    -w "$ip -> http=%{http_code} connect=%{time_connect}s total=%{time_total}s\n" \
    https://api.yookassa.ru/v3/me
done
```

Если адреса `api.yookassa.ru` не отвечают (или отвечает лишь часть), включите прокси
до российского узла (маленький VPS в РФ с `3proxy`/`gost`, или `ssh -L`):

```env
# http-прокси (aiohttp умеет только http/https; SOCKS — через aiohttp-socks)
PAYMENTS_PROXY=http://user:pass@ru-relay.example.com:3128
```

Пусто — бот ходит к платёжному API напрямую.
Код при недоступности API не висит: отдаёт `503`, и ЮKassa повторяет уведомление.

---

## Что происходит после успешной оплаты

1. Заказ получает статус `paid`, в лог платежей пишется сумма и способ.
2. Бот выдаёт ключ: создаёт клиента в панели 3x-ui.
3. Если у пользователя уже есть активная подписка на той же панели — срок продлевается,
   ключ не меняется.
4. Пользователь получает карточку подписки, QR-код и инструкцию; админы — уведомление.

Если выдача не удалась (например, панель недоступна), пользователь получает
честное сообщение «оплата получена, выдадим вручную», а админам уходит тревожный алерт —
деньги не теряются.
