#!/usr/bin/env bash
# ============================================================================
#  OPVPN — установщик для VPS (Debian/Ubuntu).
#
#  Быстрый старт:
#      sudo bash install.sh
#
#  Режимы:
#      sudo bash install.sh --update        обновить код и перезапустить
#      sudo bash install.sh --reconfigure   пересобрать .env (интерактивно или из env)
#      sudo bash install.sh --uninstall     удалить службу (база и .env останутся)
#      sudo bash install.sh --yes           без вопросов, значения из переменных окружения
#      sudo bash install.sh --dir /srv/opvpnbot --no-service
#      sudo bash install.sh --help
#
#  Одна команда до прода (systemd + nginx + HTTPS + firewall):
#      sudo bash install.sh --domain bot.example.com --email you@example.com
#
#  В режиме --yes значения берутся из окружения: BOT_TOKEN, ADMIN_IDS,
#  PAYMENT_METHODS, VPN_PANEL, XUI_*, YOOKASSA_*, WEBHOOK_*, TG_WEBHOOK_*,
#  DOMAIN, LEMC_EMAIL. Если запущено в терминале, недостающие обязательные
#  значения будут доспрошены. В неинтерактивном окружении (CI, < /dev/null)
#  при --yes скрипт упадёт с подсказкой — это ожидаемо.
#
#  Пример:
#      sudo BOT_TOKEN=123:AA... ADMIN_IDS=100 bash install.sh --yes
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
RECONFIGURE=0
MODE="install"   # install | update | uninstall

DOMAIN="${DOMAIN:-}"
LEMC_EMAIL="${LEMC_EMAIL:-}"
WITH_NGINX="${WITH_NGINX:-0}"
WITH_FIREWALL="${WITH_FIREWALL:-true}"
WEBHOOK_PATH="${WEBHOOK_PATH:-/yookassa/webhook}"
WEBHOOK_PORT="${WEBHOOK_PORT:-8080}"
HEALTH_PORT="${HEALTH_PORT:-8081}"
TG_WEBHOOK_PATH="${TG_WEBHOOK_PATH:-/tg/webhook}"
TG_WEBHOOK_ENABLED="${TG_WEBHOOK_ENABLED:-}"
TG_WEBHOOK_URL="${TG_WEBHOOK_URL:-}"
TG_WEBHOOK_SECRET="${TG_WEBHOOK_SECRET:-}"
EXTRA_PORTS="${EXTRA_PORTS:-}"

c_reset=$'\033[0m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_blu=$'\033[34m'
info() { printf '%s\n' "${c_blu}[*]${c_reset} $*"; }
ok()   { printf '%s\n' "${c_grn}[+]${c_reset} $*"; }
warn() { printf '%s\n' "${c_ylw}[!]${c_reset} $*" >&2; }
die()  { printf '%s\n' "${c_red}[x]${c_reset} $*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

is_tty() { [ -t 0 ]; }

usage() {
  cat <<'EOF'
OPVPN — установщик

Использование:
  sudo bash install.sh [опции]

Опции:
  --update            обновить код в каталоге установки и перезапустить службу
  --reconfigure       пересобрать .env (по умолчанию — только если файла нет)
  --uninstall         остановить и удалить systemd-службу (каталог, база и .env остаются)
  --yes               без интерактивных вопросов; значения берутся из переменных
                      окружения (в терминале недостающие обязательные — доспрос)
  --no-service        не создавать systemd-службу (только venv + .env)
  --dir <путь>        каталог установки (по умолчанию — каталог запуска install.sh,
                      иначе /srv/opvpnbot)
  --domain <домен>    настроить nginx + Let's Encrypt для вебхуков и /healthz
  --email <адрес>     e-mail для Let's Encrypt (обязателен при --domain)
  --with-nginx        включить настройку nginx (домен спросит/возьмёт из окружения)
  --no-nginx          не трогать nginx
  --no-firewall       не включать ufw (по умолчанию включается)
  --allow-port <п>    дополнительно открыть порт в ufw (можно через запятую)
  -h, --help          эта справка

Примеры:
  sudo bash install.sh
  sudo bash install.sh --domain bot.example.com --email you@example.com
  sudo bash install.sh --dir /srv/opvpnbot
  sudo bash install.sh --no-service --dir "$HOME/opvpnbot"
  sudo bash install.sh --reconfigure
  sudo bash install.sh --update
  sudo bash install.sh --uninstall
EOF
}

# --- разбор аргументов ------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --update)      MODE="update" ;;
    --reconfigure) RECONFIGURE=1 ;;
    --uninstall|--remove) MODE="uninstall" ;;
    --yes|-y)      ASSUME_YES=1 ;;
    --no-service)  NO_SERVICE=1 ;;
    --dir)
      shift
      [ $# -gt 0 ] || die "После --dir нужен путь."
      INSTALL_DIR="$1"
      ;;
    --dir=*)       INSTALL_DIR="${1#*=}" ;;
    --domain)
      shift
      [ $# -gt 0 ] || die "После --domain нужен домен."
      DOMAIN="$1"; WITH_NGINX=1
      ;;
    --domain=*)    DOMAIN="${1#*=}"; WITH_NGINX=1 ;;
    --email)
      shift
      [ $# -gt 0 ] || die "После --email нужен адрес."
      LEMC_EMAIL="$1"
      ;;
    --email=*)     LEMC_EMAIL="${1#*=}" ;;
    --with-nginx)  WITH_NGINX=1 ;;
    --no-nginx)    WITH_NGINX=0 ;;
    --no-firewall) WITH_FIREWALL="false" ;;
    --allow-port)
      shift
      [ $# -gt 0 ] || die "После --allow-port нужен порт."
      EXTRA_PORTS="${EXTRA_PORTS}${EXTRA_PORTS:+,}$1"
      ;;
    --allow-port=*) EXTRA_PORTS="${EXTRA_PORTS}${EXTRA_PORTS:+,}${1#*=}" ;;
    -h|--help)     usage; exit 0 ;;
    *)             die "Неизвестный аргумент: $1 (см. --help)" ;;
  esac
  shift
done

DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN%%/*}"

if [ -z "$INSTALL_DIR" ]; then
  if [ -f "$SCRIPT_DIR/bot.py" ] && [ "$SCRIPT_DIR" != "$DEFAULT_DIR" ]; then
    INSTALL_DIR="$SCRIPT_DIR"
    info "Каталог установки не задан — использую каталог запуска: ${INSTALL_DIR}"
  else
    INSTALL_DIR="$DEFAULT_DIR"
  fi
fi
INSTALL_DIR="${INSTALL_DIR%/}"
VENV="$INSTALL_DIR/.venv"

require_root() {
  [ "$(id -u)" -eq 0 ] || die "Нужны права root. Запустите: sudo bash install.sh $*"
}

# --- .env helpers -----------------------------------------------------------
# env_value KEY [FILE] — значение ключа из .env (пусто, если файла/ключа нет).
env_value() {
  local key="$1" file="${2:-$INSTALL_DIR/.env}"
  [ -f "$file" ] || return 0
  awk -F= -v k="$key" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$file"
}

# seed_from_env VAR — если переменная пуста, подставить значение из .env.
seed_from_env() {
  local __var="$1" __cur __from_env
  __cur="${!__var-}"
  if [ -z "$__cur" ]; then
    __from_env="$(env_value "$__var")"
    if [ -n "$__from_env" ]; then
      printf -v "$__var" '%s' "$__from_env"
    fi
  fi
}

set_env() {
  local __f="$1" __k="$2" __v="$3" __tmp
  __tmp="$(mktemp)"
  awk -v k="$__k" -v v="$__v" '
    BEGIN { FS = "="; found = 0 }
    $1 == k { print k "=" v; found = 1; next }
    { print }
    END { if (!found) print k "=" v }
  ' "$__f" > "$__tmp"
  mv "$__tmp" "$__f"
}

put_env() {
  if [ -n "${3-}" ]; then set_env "$1" "$2" "$3"; fi
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

# --- ввод значений ----------------------------------------------------------
# ask VAR "вопрос" [значение по умолчанию]
#
# Поведение:
#   - если значение уже задано (флаг/env/seed из .env) и --yes → оставить как есть
#   - если --yes и нет TTY → взять default (или пусто) и не спрашивать
#   - иначе (интерактив) → спросить, подставив текущее/default
ask() {
  local __var="$1" __q="$2" __def="${3-}" __cur __ans __hint=""
  __cur="${!__var-}"

  if [ "$ASSUME_YES" -eq 1 ]; then
    if [ -n "$__cur" ]; then
      return 0
    fi
    if ! is_tty; then
      printf -v "$__var" '%s' "$__def"
      return 0
    fi
    # TTY + --yes + пусто → спрашиваем (гибридный режим)
  fi

  [ -n "$__def" ] && __hint=" [${__def}]"
  read -r -p "  ${__q}${__hint}: " __ans || true
  [ -n "$__ans" ] || __ans="${__cur:-$__def}"
  printf -v "$__var" '%s' "$__ans"
}

# ask_required VAR "вопрос"
ask_required() {
  local __var="$1" __q="$2" __from_env
  while :; do
    ask "$__var" "$__q" ""
    if [ -n "${!__var}" ]; then return 0; fi

    # Fallback: может быть в .env (для --reconfigure).
    __from_env="$(env_value "$__var")"
    if [ -n "$__from_env" ]; then
      printf -v "$__var" '%s' "$__from_env"
      return 0
    fi

    if [ "$ASSUME_YES" -eq 1 ] && ! is_tty; then
      die "Не задано обязательное значение ${__var}. Передайте его через переменную окружения."
    fi
    if [ "$ASSUME_YES" -eq 1 ]; then
      die "Не задано обязательное значение ${__var}. Передайте его через переменную окружения или запустите без --yes."
    fi
    warn "Значение обязательно, попробуйте ещё раз."
  done
}

# ask_choice VAR "вопрос" "по умолчанию" вариант1 вариант2 ...
ask_choice() {
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

# --- сбор конфига -----------------------------------------------------------
collect_config() {
  # Подтянуть текущие значения из .env, чтобы при --yes/--reconfigure не
  # спрашивать по второму разу то, что уже есть.
  local __k
  for __k in BOT_TOKEN ADMIN_IDS BOT_NAME PAYMENT_METHODS \
             VPN_PANEL XUI_URL XUI_API_TOKEN XUI_USERNAME XUI_PASSWORD \
             XUI_INBOUND_IDS XUI_PUBLIC_HOST CLAIM_ENABLED CLAIM_SECRET \
             YOOKASSA_SHOP_ID YOOKASSA_SECRET_KEY MANUAL_PAYMENT_NOTE \
             WEBHOOK_ENABLED WEBHOOK_PUBLIC_URL WEBHOOK_TRUST_PROXY \
             TG_WEBHOOK_ENABLED TG_WEBHOOK_URL TG_WEBHOOK_SECRET; do
    seed_from_env "$__k"
  done
  [ -n "${DOMAIN:-}" ] || DOMAIN="$(env_value DOMAIN)"
  [ -n "${LEMC_EMAIL:-}" ] || LEMC_EMAIL="$(env_value LEMC_EMAIL)"

  printf '\n%s\n' "${c_blu}=== Настройка бота ===${c_reset}"
  ask_required BOT_TOKEN "Токен бота от @BotFather"
  ask_required ADMIN_IDS "Ваш Telegram ID (из @userinfobot), можно несколько через запятую"
  ask BOT_NAME "Название сервиса" "OPVPN"
  ask PAYMENT_METHODS "Способы оплаты (через запятую)" "yookassa"

  printf '\n%s\n' "${c_blu}=== Выдача ключей ===${c_reset}"
  VPN_PANEL="xui"
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
  esac

  printf '\n%s\n' "${c_blu}=== Публичный адрес (nginx + HTTPS) ===${c_reset}"
  ask DOMAIN "Домен для вебхука и /healthz (Enter — без nginx)" "${DOMAIN:-}"
  if [ -n "${DOMAIN:-}" ]; then
    WITH_NGINX=1
    WEBHOOK_PUBLIC_URL="https://${DOMAIN}${WEBHOOK_PATH}"
    TG_WEBHOOK_URL="https://${DOMAIN}${TG_WEBHOOK_PATH}"
    ask_required LEMC_EMAIL "E-mail для Let's Encrypt (уведомления о сертификате)"
    ask_choice TG_WEBHOOK_ENABLED "Перевести апдейты Telegram на вебхук" "${TG_WEBHOOK_ENABLED:-true}" true false
    if [ "${TG_WEBHOOK_ENABLED:-false}" = "true" ] && [ -z "${TG_WEBHOOK_SECRET:-}" ]; then
      TG_WEBHOOK_SECRET="$(gen_secret)"
      info "Сгенерирован TG_WEBHOOK_SECRET (проверка запросов от Telegram)."
    fi
  elif [ "$WITH_NGINX" -eq 1 ]; then
    die "Указан --with-nginx, но домен не задан (--domain)."
  fi
  ask_choice WITH_FIREWALL "Включить firewall ufw (SSH/80/443 откроются автоматически)" "$WITH_FIREWALL" true false

  case ",${PAYMENT_METHODS}," in
    *,yookassa,*)
      printf '\n%s\n' "${c_blu}=== ЮKassa ===${c_reset}"
      ask YOOKASSA_SHOP_ID "shopId ЮKassa (Enter — пропустить)" ""
      if [ -n "${YOOKASSA_SHOP_ID:-}" ]; then
        ask_required YOOKASSA_SECRET_KEY "Секретный ключ ЮKassa"
        if [ "$WITH_NGINX" -eq 1 ]; then
          WEBHOOK_ENABLED="true"
          WEBHOOK_TRUST_PROXY="true"
          info "Вебхук ЮKassa включён автоматически: ${WEBHOOK_PUBLIC_URL}"
        else
          ask_choice WEBHOOK_ENABLED "Включить вебхуки ЮKassa (нужен публичный HTTPS)" "false" false true
          if [ "${WEBHOOK_ENABLED:-false}" = "true" ]; then
            ask_required WEBHOOK_PUBLIC_URL "Публичный URL вебхука, например https://bot.example.com/yookassa/webhook"
            ask_choice WEBHOOK_TRUST_PROXY "Перед ботом стоит reverse-proxy (nginx/caddy)" "false" false true
          fi
        fi
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
  put_env "$env_file" PAYMENT_METHODS "${PAYMENT_METHODS:-}"
  set_env "$env_file" VPN_PANEL "$VPN_PANEL"

  # DOMAIN нужен на будущее (--update/--reconfigure) — сохраняем.
  put_env "$env_file" DOMAIN "${DOMAIN:-}"
  put_env "$env_file" LEMC_EMAIL "${LEMC_EMAIL:-}"

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
  put_env "$env_file" MANUAL_PAYMENT_NOTE "${MANUAL_PAYMENT_NOTE:-Реквизиты уточняйте у поддержки.}"
  if [ "${WEBHOOK_ENABLED:-false}" = "true" ]; then
    set_env "$env_file" WEBHOOK_ENABLED true
    put_env "$env_file" WEBHOOK_PUBLIC_URL "${WEBHOOK_PUBLIC_URL:-}"
    if [ "${WEBHOOK_TRUST_PROXY:-false}" = "true" ]; then
      set_env "$env_file" WEBHOOK_TRUST_PROXY true
    fi
  fi

  if [ "$WITH_NGINX" -eq 1 ]; then
    set_env "$env_file" WEBHOOK_HOST 127.0.0.1
    set_env "$env_file" WEBHOOK_PATH "$WEBHOOK_PATH"
    set_env "$env_file" WEBHOOK_PORT "$WEBHOOK_PORT"
  fi

  if [ "${TG_WEBHOOK_ENABLED:-false}" = "true" ]; then
    set_env "$env_file" TG_WEBHOOK_ENABLED true
    put_env "$env_file" TG_WEBHOOK_URL "${TG_WEBHOOK_URL:-}"
    put_env "$env_file" TG_WEBHOOK_SECRET "${TG_WEBHOOK_SECRET:-}"
  fi
}

# --- система / зависимости --------------------------------------------------
install_system_deps() {
  [ "$(id -u)" -eq 0 ] || return 0
  if have apt-get; then
    if ! have python3 || ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
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

# --- версия сборки ----------------------------------------------------------
git_describe() {
  local dir="$1"
  have git || return 0
  [ -d "$dir/.git" ] || return 0
  git -C "$dir" describe --tags --always --dirty 2>/dev/null || true
}

pin_version() {
  local ver=""
  ver="$(git_describe "$SCRIPT_DIR")"
  [ -n "$ver" ] || ver="$(git_describe "$INSTALL_DIR")"
  [ -n "$ver" ] || return 0
  set_env "$INSTALL_DIR/.env" APP_VERSION "$ver"
  info "Версия сборки: ${ver}"
}

# --- nginx / firewall -------------------------------------------------------
apt_locked() {
  have pgrep || return 1
  pgrep -x dpkg >/dev/null 2>&1 \
    || pgrep -x apt-get >/dev/null 2>&1 \
    || pgrep -x apt >/dev/null 2>&1 \
    || pgrep -f 'unattended-upgr' >/dev/null 2>&1
}

wait_apt_lock() {
  local waited=0
  while apt_locked; do
    if [ "$waited" -eq 0 ]; then
      info "apt/dpkg занят (идёт unattended-upgrades?) — жду освобождения..."
    fi
    sleep 5
    waited=$((waited + 5))
    if [ "$waited" -ge 180 ]; then
      warn "apt/dpkg не освободился за 3 минуты."
      return 1
    fi
  done
  return 0
}

apt_install() {
  [ "$#" -gt 0 ] || return 0
  have apt-get || die "Автонастройка поддерживается только на Debian/Ubuntu (нет apt-get)."
  local attempt
  wait_apt_lock || true
  apt-get update -y >/dev/null || warn "apt-get update не удался — продолжаю с текущими индексами."
  for attempt in 1 2 3; do
    wait_apt_lock || break
    if DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"; then
      return 0
    fi
    warn "apt-get install не удался (попытка ${attempt}/3) — повторю через 10 c."
    sleep 10
  done
  die "Не удалось установить пакеты: $*"
}

port_of_url() {
  local url="$1" rest
  [ -n "$url" ] || return 0
  rest="${url#*://}"
  rest="${rest%%/*}"
  case "$rest" in
    *:*) rest="${rest##*:}" ;;
    *)   rest="" ;;
  esac
  case "$rest" in
    ''|*[!0-9]*) return 0 ;;
  esac
  printf '%s' "$rest"
}

nginx_site_path() { printf '%s' "/etc/nginx/sites-available/${APP_NAME}"; }

render_nginx_config() {
  local tmpl="$1" out="$2"
  [ -f "$tmpl" ] || die "Не найден шаблон nginx: $tmpl"
  sed -e "s|@@DOMAIN@@|${DOMAIN}|g" \
      -e "s|@@CERT@@|/etc/letsencrypt/live/${DOMAIN}/fullchain.pem|g" \
      -e "s|@@KEY@@|/etc/letsencrypt/live/${DOMAIN}/privkey.pem|g" \
      -e "s|@@WEBHOOK_PATH@@|${WEBHOOK_PATH}|g" \
      -e "s|@@TG_WEBHOOK_PATH@@|${TG_WEBHOOK_PATH}|g" \
      -e "s|@@WEBHOOK_PORT@@|${WEBHOOK_PORT}|g" \
      -e "s|@@HEALTH_PORT@@|${HEALTH_PORT}|g" \
      "$tmpl" > "$out"
}

# Подтянуть DOMAIN/LEMC_EMAIL/WEBHOOK_PATH из .env — чтобы --update не зависел
# от флагов и не парсил nginx-конфиг.
seed_infra_from_env() {
  local v
  v="$(env_value DOMAIN)";       [ -n "$v" ] && DOMAIN="$v"
  v="$(env_value LEMC_EMAIL)";   [ -n "$v" ] && LEMC_EMAIL="$v"
  v="$(env_value WEBHOOK_PATH)"; [ -n "$v" ] && WEBHOOK_PATH="$v"
  v="$(env_value WEBHOOK_PORT)"; [ -n "$v" ] && WEBHOOK_PORT="$v"
  v="$(env_value TG_WEBHOOK_PATH)"; [ -n "$v" ] && TG_WEBHOOK_PATH="$v"
}

setup_nginx() {
  local site cert_dir tmpl
  site="$(nginx_site_path)"

  seed_infra_from_env

  # Совсем не настраивать nginx, если он не просили и сайта нет.
  if [ "$WITH_NGINX" -eq 0 ] && [ ! -f "$site" ]; then
    return 0
  fi
  [ -n "${DOMAIN:-}" ] || { warn "nginx пропущен: домен не задан (--domain)."; return 0; }

  cert_dir="/etc/letsencrypt/live/${DOMAIN}"
  if [ -z "${LEMC_EMAIL:-}" ] && [ ! -d "$cert_dir" ]; then
    warn "nginx пропущен: нет сертификата для ${DOMAIN} и не задан --email."
    return 0
  fi

  info "Проверяю nginx и certbot..."
  if ! have nginx || ! have certbot || ! have curl; then
    apt_install nginx certbot python3-certbot-nginx curl
  fi

  tmpl="$INSTALL_DIR/deploy/nginx/opvpnbot.conf.tmpl"
  [ -f "$tmpl" ] || tmpl="$SCRIPT_DIR/deploy/nginx/opvpnbot.conf.tmpl"

  if [ -f "$site" ] && ! grep -q 'OPVPN (install.sh)' "$site"; then
    local bak="${site}.bak.$(date +%Y%m%d-%H%M%S)"
    cp -a "$site" "$bak"
    warn "Существующий конфиг nginx сохранён: ${bak}"
  fi

  if [ ! -d "$cert_dir" ]; then
    info "Выпускаю сертификат Let's Encrypt для ${DOMAIN} (нужны DNS на этот сервер и открытый порт 80)..."
    cat > "$site" <<EOF
# OPVPN (install.sh) — временный блок для выпуска сертификата
server {
    listen 80;
    server_name ${DOMAIN};
    location / { return 200 'ok'; add_header Content-Type text/plain; }
}
EOF
    ln -sf "$site" "/etc/nginx/sites-enabled/${APP_NAME}"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx || warn "nginx не перезапустился."
    if certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos \
         --keep-until-expiring -m "$LEMC_EMAIL"; then
      ok "Сертификат получен."
    else
      warn "certbot не выпустил сертификат — проверьте DNS домена и доступность порта 80 извне."
    fi
  else
    info "Сертификат для ${DOMAIN} уже есть — оставляю как есть."
  fi

  if [ -d "$cert_dir" ]; then
    render_nginx_config "$tmpl" "$site"
    ln -sf "$site" "/etc/nginx/sites-enabled/${APP_NAME}"
    nginx -t || die "nginx не принял конфиг ${site} — исправьте и повторите."
    systemctl reload nginx
    ok "nginx настроен: https://${DOMAIN} (вебхук ${WEBHOOK_PATH}, /healthz)"
  else
    warn "Сертификата нет — оставлен временный HTTP-блок. Повторите: sudo bash install.sh --domain ${DOMAIN} --email ${LEMC_EMAIL}"
  fi
}

ssh_port() {
  local port
  port="$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}')" || true
  if [ -z "$port" ]; then
    port="$(awk '/^[[:space:]]*Port[[:space:]]+[0-9]+/{print $2; exit}' /etc/ssh/sshd_config 2>/dev/null)" || true
  fi
  printf '%s' "${port:-22}"
}

setup_firewall() {
  [ "${WITH_FIREWALL:-true}" = "true" ] || return 0
  info "Настраиваю ufw: SSH/80/443 открываются ДО включения, чтобы не потерять доступ..."
  have ufw || apt_install ufw

  local ssh_p p url extra
  ssh_p="$(ssh_port)"
  ufw allow "${ssh_p}/tcp" >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null

  for url in "$(env_value XUI_URL)" "$(env_value XUI_SUB_BASE)"; do
    p="$(port_of_url "$url")"
    [ -n "$p" ] || continue
    case "$p" in 22|80|443) continue ;; esac
    ufw allow "${p}/tcp" >/dev/null
    ok "ufw: открыт порт ${p} (панель/подписка 3x-ui)."
  done

  for extra in ${EXTRA_PORTS//,/ }; do
    [ -n "$extra" ] || continue
    ufw allow "${extra}/tcp" >/dev/null
    ok "ufw: открыт порт ${extra} (--allow-port)."
  done

  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw --force enable >/dev/null
  ok "ufw включён: открыты SSH (${ssh_p}), 80, 443 и порты выше; порты бота (${WEBHOOK_PORT}, ${HEALTH_PORT}) — только локально."
}

verify_deploy() {
  local url
  if [ "$WITH_NGINX" -eq 1 ] && [ -n "${DOMAIN:-}" ] && [ -d "/etc/letsencrypt/live/${DOMAIN}" ]; then
    url="https://${DOMAIN}/healthz"
  else
    url="http://127.0.0.1:${HEALTH_PORT}/healthz"
  fi
  have curl || return 0
  info "Проверяю ${url} ..."
  if curl -fsS --max-time 10 --retry 10 --retry-delay 2 --retry-connrefused "$url"; then
    printf '\n'
    ok "Бот отвечает на /healthz."
  else
    warn "Проверка /healthz не прошла — смотрите: journalctl -u ${APP_NAME} -n 50"
  fi
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
StartLimitIntervalSec=600
StartLimitBurst=12

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
RestartPreventExitStatus=2
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

  # Логика .env:
  #   - --update       → .env не трогаем вообще
  #   - --reconfigure  → пересобираем (интерактивно или из env)
  #   - .env нет       → собираем
  #   - .env есть      → оставляем
  local need_env=0
  if [ "$MODE" = "install" ]; then
    if [ "$RECONFIGURE" -eq 1 ] || [ ! -f "$INSTALL_DIR/.env" ]; then
      need_env=1
    fi
  fi

  if [ "$need_env" -eq 1 ]; then
    collect_config
    write_env
  elif [ -f "$INSTALL_DIR/.env" ]; then
    info "Файл .env уже существует — оставляю без изменений (пересобрать: --reconfigure)."
  fi

  [ -f "$INSTALL_DIR/.env" ] || die "Не удалось создать ${INSTALL_DIR}/.env."

  pin_version

  if [ "$NO_SERVICE" -eq 1 ]; then
    ok "Установка завершена (без systemd)."
    info "Запуск вручную:"
    printf '    %s/bin/python %s/bot.py\n' "$VENV" "$INSTALL_DIR"
    info "Фоновый запуск: nohup %s/bin/python %s/bot.py >> %s/data/bot.log 2>&1 &" \
      "$VENV" "$INSTALL_DIR" "$INSTALL_DIR"
    return 0
  fi

  write_service

  setup_nginx
  setup_firewall
  verify_deploy

  local public="" tg_line=""
  if [ "$WITH_NGINX" -eq 1 ] && [ -n "${DOMAIN:-}" ]; then
    if [ "${TG_WEBHOOK_ENABLED:-false}" = "true" ]; then
      tg_line="
  Апдейты Telegram: https://${DOMAIN}${TG_WEBHOOK_PATH}   (вебхук: бот регистрирует его сам при старте)"
    else
      tg_line="
  Апдейты Telegram: long-polling (getUpdates)"
    fi
    public="
  Публичный адрес: https://${DOMAIN}
  Вебхук ЮKassa:   https://${DOMAIN}${WEBHOOK_PATH}   (укажите тот же адрес в кабинете ЮKassa)${tg_line}
  Healthcheck:     https://${DOMAIN}/healthz
"
  fi

  cat <<EOF

$(ok "Готово!")

  Каталог:   ${INSTALL_DIR}
  Конфиг:    ${INSTALL_DIR}/.env
  Данные:    ${INSTALL_DIR}/data/
${public}
  Управление:
    systemctl status ${APP_NAME}
    journalctl -u ${APP_NAME} -f      # живые логи
    systemctl restart ${APP_NAME}

  Обновление:     sudo bash ${INSTALL_DIR}/install.sh --update
  Пересобрать .env: sudo bash ${INSTALL_DIR}/install.sh --reconfigure
EOF
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main
fi
