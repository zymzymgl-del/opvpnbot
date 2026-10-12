#!/usr/bin/env bash
# ============================================================================
#  OPVPN — установщик для VPS (Debian/Ubuntu).
#
#  Быстрый старт:
#      sudo bash install.sh
#
#  Режимы:
#      sudo bash install.sh --update        обновить код и перезапустить
#      sudo bash install.sh --uninstall     удалить службу (база и .env останутся)
#      sudo bash install.sh --yes           без вопросов, значения из переменных окружения
#      sudo bash install.sh --dir /srv/opvpnbot --no-service
#      sudo bash install.sh --help
#
#  При --yes значения берутся из окружения: BOT_TOKEN, ADMIN_IDS, PAYMENT_METHODS,
#  VPN_PANEL, XUI_*, YOOKASSA_*, YOOMONEY_*, WEBHOOK_*. Полный список —
#  в .env.example. Пример:
#      sudo BOT_TOKEN=123:AA... ADMIN_IDS=100 APP=... bash install.sh --yes
# ============================================================================
set -euo pipefail

APP_NAME="opvpnbot"
SERVICE_USER="opvpn"
DEFAULT_DIR="/srv/opvpnbot"
UNIT_PATH="/etc/systemd/system/${APP_NAME}.service"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

INSTALL_DIR=""
ASSUME_YES=0
NO_SERVICE=0
MODE="install"   # install | update | uninstall

c_reset=$'\033[0m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_blu=$'\033[34m'
info() { printf '%s\n' "${c_blu}[*]${c_reset} $*"; }
ok()   { printf '%s\n' "${c_grn}[+]${c_reset} $*"; }
warn() { printf '%s\n' "${c_ylw}[!]${c_reset} $*" >&2; }
die()  { printf '%s\n' "${c_red}[x]${c_reset} $*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<'EOF'
OPVPN — установщик

Использование:
  sudo bash install.sh [опции]

Опции:
  --update            обновить код в каталоге установки и перезапустить службу
  --uninstall         остановить и удалить systemd-службу (каталог, база и .env остаются)
  --yes               без интерактивных вопросов; значения берутся из переменных окружения
  --no-service        не создавать systemd-службу (только venv + .env)
  --dir <путь>        каталог установки (по умолчанию /srv/opvpnbot)
  -h, --help          эта справка

Примеры:
  sudo bash install.sh
  sudo bash install.sh --dir /srv/opvpnbot
  sudo bash install.sh --no-service --dir "$HOME/opvpnbot"
  sudo bash install.sh --update
  sudo bash install.sh --uninstall
EOF
}

# --- разбор аргументов ------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --update)      MODE="update" ;;
    --uninstall|--remove) MODE="uninstall" ;;
    --yes|-y)      ASSUME_YES=1 ;;
    --no-service)  NO_SERVICE=1 ;;
    --dir)
      shift
      [ $# -gt 0 ] || die "После --dir нужен путь."
      INSTALL_DIR="$1"
      ;;
    --dir=*)       INSTALL_DIR="${1#*=}" ;;
    -h|--help)     usage; exit 0 ;;
    *)             die "Неизвестный аргумент: $1 (см. --help)" ;;
  esac
  shift
done

[ -n "$INSTALL_DIR" ] || INSTALL_DIR="$DEFAULT_DIR"
INSTALL_DIR="${INSTALL_DIR%/}"
VENV="$INSTALL_DIR/.venv"

require_root() {
  [ "$(id -u)" -eq 0 ] || die "Нужны права root. Запустите: sudo bash install.sh $*"
}

# --- ввод значений ----------------------------------------------------------
ask() {
  # ask VAR "вопрос" [значение по умолчанию]
  local __var="$1" __q="$2" __def="${3-}" __cur __ans __hint=""
  __cur="${!__var-}"
  if [ "$ASSUME_YES" -eq 1 ]; then
    [ -n "$__cur" ] || __cur="$__def"
    printf -v "$__var" '%s' "$__cur"
    return 0
  fi
  [ -n "$__def" ] && __hint=" [${__def}]"
  read -r -p "  ${__q}${__hint}: " __ans || true
  [ -n "$__ans" ] || __ans="${__cur:-$__def}"
  printf -v "$__var" '%s' "$__ans"
}

ask_required() {
  # ask_required VAR "вопрос"
  local __var="$1" __q="$2"
  while :; do
    ask "$__var" "$__q" ""
    if [ -n "${!__var}" ]; then return 0; fi
    if [ "$ASSUME_YES" -eq 1 ]; then
      die "Не задано обязательное значение ${__var}. Передайте его через переменную окружения."
    fi
    warn "Значение обязательно, попробуйте ещё раз."
  done
}

ask_choice() {
  # ask_choice VAR "вопрос" "по умолчанию" вариант1 вариант2 ...
  local __var="$1" __q="$2" __def="$3"; shift 3
  local __opts="$*" __val __o
  while :; do
    ask "$__var" "${__q} (${__opts// /|})" "$__def"
    __val="${!__var}"
    for __o in "$@"; do
      if [ "$__val" = "$__o" ]; then return 0; fi
    done
    if [ "$ASSUME_YES" -eq 1 ]; then
      die "Недопустимое ${__var}=${__val}. Допустимо: ${__opts}"
    fi
    warn "Допустимые значения: ${__opts}"
  done
}

gen_secret() {
  if have openssl; then
    openssl rand -hex 32
  elif [ -r /dev/urandom ]; then
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  else
    printf 'change-me-%s' "$(date +%s)"
  fi
}

# --- .env -------------------------------------------------------------------
set_env() {
  # set_env FILE KEY VALUE  — заменяет строку KEY=... либо добавляет её в конец
  local __f="$1" __k="$2" __v="$3" __tmp
  __tmp="$(mktemp)"
  awk -v k="$__k" -v v="$__v" 'BEGIN{FS="="} $1==k{print k"="v; next} {print}' "$__f" > "$__tmp"
  mv "$__tmp" "$__f"
}

put_env() {
  # put_env FILE KEY VALUE — как set_env, но пропускает пустые значения
  if [ -n "${3-}" ]; then set_env "$1" "$2" "$3"; fi
}

collect_config() {
  printf '\n%s\n' "${c_blu}=== Настройка бота ===${c_reset}"
  ask_required BOT_TOKEN "Токен бота от @BotFather"
  ask_required ADMIN_IDS "Ваш Telegram ID (из @userinfobot), можно несколько через запятую"
  ask BOT_NAME "Название сервиса" "OPVPN"
  ask SUPPORT_USERNAME "Поддержка, без @" "support"
  ask PAYMENT_METHODS "Способы оплаты (через запятую)" "yookassa,yoomoney,stars,balance,manual"

  printf '\n%s\n' "${c_blu}=== Выдача ключей ===${c_reset}"
  ask_choice VPN_PANEL "Режим выдачи ключей" "pool" pool xui
  case "$VPN_PANEL" in
    xui)
      ask_required XUI_URL "Адрес панели 3x-ui (вместе с путём, если изменён)"
      ask XUI_API_TOKEN "API-токен 3x-ui (Enter — использовать логин/пароль)" ""
      if [ -z "${XUI_API_TOKEN:-}" ]; then
        ask XUI_USERNAME "Логин 3x-ui" "admin"
        ask_required XUI_PASSWORD "Пароль 3x-ui"
      fi
      ask XUI_INBOUND_IDS "ID inbound'ов (через запятую, можно несколько)" "1"
      ask XUI_PUBLIC_HOST "Публичный адрес сервера для ссылок (необязательно)" ""
      ask_choice CLAIM_ENABLED "Включить привязку готовых клиентов 3x-ui по QR/ссылке" "false" false true
      if [ "${CLAIM_ENABLED:-false}" = "true" ] && [ -z "${CLAIM_SECRET:-}" ]; then
        CLAIM_SECRET="$(gen_secret)"
        info "Сгенерирован CLAIM_SECRET для подписи ссылок привязки."
      fi
      ;;
    pool) : ;;
  esac

  case ",${PAYMENT_METHODS}," in
    *,yookassa,*)
      printf '\n%s\n' "${c_blu}=== ЮKassa ===${c_reset}"
      ask YOOKASSA_SHOP_ID "shopId ЮKassa (Enter — пропустить)" ""
      if [ -n "${YOOKASSA_SHOP_ID:-}" ]; then
        ask_required YOOKASSA_SECRET_KEY "Секретный ключ ЮKassa"
        ask_choice WEBHOOK_ENABLED "Включить вебхуки ЮKassa (нужен публичный HTTPS)" "false" false true
        if [ "${WEBHOOK_ENABLED:-false}" = "true" ]; then
          ask_required WEBHOOK_PUBLIC_URL "Публичный URL вебхука, например https://bot.example.com/yookassa/webhook"
          ask_choice WEBHOOK_TRUST_PROXY "Перед ботом стоит reverse-proxy (nginx/caddy)" "false" false true
        fi
      fi
      ;;
  esac

  case ",${PAYMENT_METHODS}," in
    *,yoomoney,*)
      printf '\n%s\n' "${c_blu}=== ЮMoney ===${c_reset}"
      ask YOOMONEY_WALLET "Номер кошелька ЮMoney (Enter — пропустить)" ""
      if [ -n "${YOOMONEY_WALLET:-}" ]; then
        ask_required YOOMONEY_TOKEN "OAuth-токен ЮMoney (право operation-history)"
      fi
      ;;
  esac
}

write_env() {
  local env_file="$INSTALL_DIR/.env" template
  template="$INSTALL_DIR/.env.example"
  [ -f "$template" ] || template="$SCRIPT_DIR/.env.example"
  [ -f "$template" ] || die ".env.example не найден — не могу создать .env"
  cp "$template" "$env_file"
  chmod 600 "$env_file"

  set_env "$env_file" BOT_TOKEN "$BOT_TOKEN"
  set_env "$env_file" ADMIN_IDS "$ADMIN_IDS"
  put_env "$env_file" BOT_NAME "${BOT_NAME:-}"
  put_env "$env_file" SUPPORT_USERNAME "${SUPPORT_USERNAME:-}"
  put_env "$env_file" PAYMENT_METHODS "${PAYMENT_METHODS:-}"
  set_env "$env_file" VPN_PANEL "$VPN_PANEL"

  case "$VPN_PANEL" in
    xui)
      put_env "$env_file" XUI_URL "${XUI_URL:-}"
      put_env "$env_file" XUI_API_TOKEN "${XUI_API_TOKEN:-}"
      put_env "$env_file" XUI_USERNAME "${XUI_USERNAME:-}"
      put_env "$env_file" XUI_PASSWORD "${XUI_PASSWORD:-}"
      put_env "$env_file" XUI_INBOUND_IDS "${XUI_INBOUND_IDS:-}"
      put_env "$env_file" XUI_PUBLIC_HOST "${XUI_PUBLIC_HOST:-}"
      if [ "${CLAIM_ENABLED:-false}" = "true" ]; then
        set_env "$env_file" CLAIM_ENABLED true
        put_env "$env_file" CLAIM_SECRET "${CLAIM_SECRET:-}"
      fi
      ;;
  esac

  put_env "$env_file" YOOKASSA_SHOP_ID "${YOOKASSA_SHOP_ID:-}"
  put_env "$env_file" YOOKASSA_SECRET_KEY "${YOOKASSA_SECRET_KEY:-}"
  put_env "$env_file" YOOMONEY_WALLET "${YOOMONEY_WALLET:-}"
  put_env "$env_file" YOOMONEY_TOKEN "${YOOMONEY_TOKEN:-}"
  if [ "${WEBHOOK_ENABLED:-false}" = "true" ]; then
    set_env "$env_file" WEBHOOK_ENABLED true
    put_env "$env_file" WEBHOOK_PUBLIC_URL "${WEBHOOK_PUBLIC_URL:-}"
    if [ "${WEBHOOK_TRUST_PROXY:-false}" = "true" ]; then
      set_env "$env_file" WEBHOOK_TRUST_PROXY true
    fi
  fi
}

# --- система / зависимости --------------------------------------------------
install_system_deps() {
  [ "$(id -u)" -eq 0 ] || return 0
  if have apt-get; then
    if ! python3 -c 'import venv' >/dev/null 2>&1 || ! have python3; then
      info "Устанавливаю системные зависимости (python3, venv, pip)..."
      apt-get update -y
      DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip ca-certificates
    fi
  else
    warn "apt-get не найден — позаботьтесь, чтобы были установлены python3, python3-venv и python3-pip."
  fi
}

check_python() {
  have python3 || die "python3 не найден. Установите Python 3.10+ и повторите."
  local ver
  ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  python3 - <<'PY' || die "Нужен Python 3.10 или новее."
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  info "Использую Python ${ver}."
}

# --- код и окружение --------------------------------------------------------
sync_code() {
  mkdir -p "$INSTALL_DIR" 2>/dev/null \
    || die "Не могу создать ${INSTALL_DIR} (нет прав?). Используйте доступный каталог или запустите через sudo."
  if [ "$SCRIPT_DIR" = "$INSTALL_DIR" ]; then
    info "Код уже находится в ${INSTALL_DIR} — копирование не требуется."
    return 0
  fi
  info "Копирую файлы проекта в ${INSTALL_DIR}..."
  local it
  for it in bot.py app tools docs deploy \
            requirements.txt requirements-dev.txt .env.example README.md LICENSE install.sh; do
    [ -e "$SCRIPT_DIR/$it" ] || continue
    rm -rf "${INSTALL_DIR:?}/$it"
    cp -a "$SCRIPT_DIR/$it" "$INSTALL_DIR/"
  done
}

update_code() {
  if [ -d "$INSTALL_DIR/.git" ] && have git; then
    info "Обновляю код через git..."
    if ! git -C "$INSTALL_DIR" pull --ff-only; then
      warn "git pull не удался — копирую файлы из ${SCRIPT_DIR}."
      sync_code
    fi
  else
    sync_code
  fi
}

setup_venv() {
  if [ ! -x "$VENV/bin/python" ]; then
    info "Создаю виртуальное окружение в ${VENV}..."
    python3 -m venv "$VENV"
  fi
  info "Обновляю pip и устанавливаю зависимости..."
  "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
  "$VENV/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"
}

# --- systemd ----------------------------------------------------------------
write_service() {
  info "Создаю systemd-службу ${APP_NAME}..."
  if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" 2>/dev/null \
      || useradd --system --home-dir "$INSTALL_DIR" --shell /bin/false "$SERVICE_USER"
  fi
  chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

  cat > "$UNIT_PATH" <<EOF
[Unit]
Description=OPVPN (Telegram bot for VPN subscriptions)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV/bin/python $INSTALL_DIR/bot.py
Restart=always
RestartSec=5
TimeoutStopSec=20
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$APP_NAME
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "$APP_NAME" >/dev/null 2>&1 || true
  systemctl restart "$APP_NAME"
  ok "Служба ${APP_NAME} запущена."
  systemctl --no-pager --lines=0 status "$APP_NAME" || true
}

do_uninstall() {
  require_root
  info "Останавливаю и удаляю службу ${APP_NAME}..."
  systemctl stop "$APP_NAME" 2>/dev/null || true
  systemctl disable "$APP_NAME" 2>/dev/null || true
  rm -f "$UNIT_PATH"
  systemctl daemon-reload 2>/dev/null || true
  ok "Служба удалена. Каталог ${INSTALL_DIR} (база и .env) сохранён."
  info "Полное удаление: rm -rf ${INSTALL_DIR} && userdel ${SERVICE_USER}"
}

# --- основной поток ---------------------------------------------------------
main() {
  if [ "$MODE" = "uninstall" ]; then
    do_uninstall
    exit 0
  fi

  if [ "$NO_SERVICE" -eq 0 ]; then
    require_root
  fi

  install_system_deps
  check_python

  if [ "$MODE" = "update" ]; then
    [ -d "$INSTALL_DIR" ] || die "Каталог ${INSTALL_DIR} не найден — сначала выполните установку."
    update_code
  else
    sync_code
  fi

  [ -f "$INSTALL_DIR/requirements.txt" ] \
    || die "В ${INSTALL_DIR} нет requirements.txt — похоже, неверный каталог установки."

  setup_venv

  if [ -f "$INSTALL_DIR/.env" ]; then
    info "Файл .env уже существует — оставляю без изменений (правьте его вручную при необходимости)."
  else
    collect_config
    write_env
  fi

  if [ "$NO_SERVICE" -eq 1 ]; then
    ok "Установка завершена (без systemd)."
    info "Запуск вручную:"
    printf '    %s/bin/python %s/bot.py\n' "$VENV" "$INSTALL_DIR"
    info "Фоновый запуск: nohup %s/bin/python %s/bot.py >> %s/data/bot.log 2>&1 &" \
      "$VENV" "$INSTALL_DIR" "$INSTALL_DIR"
    return 0
  fi

  write_service

  cat <<EOF

$(ok "Готово!")

  Каталог:   ${INSTALL_DIR}
  Конфиг:    ${INSTALL_DIR}/.env
  Данные:    ${INSTALL_DIR}/data/

  Управление:
    systemctl status ${APP_NAME}
    journalctl -u ${APP_NAME} -f      # живые логи
    systemctl restart ${APP_NAME}

  Обновление:  sudo bash ${INSTALL_DIR}/install.sh --update
EOF
}

# Запускаем поток только при прямом исполнении (позволяет `source install.sh`
# для тестов/переиспользования функций без запуска установки).
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main
fi
