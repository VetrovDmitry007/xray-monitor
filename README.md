# xray-monitor / Telegram Proxy Client

Небольшой проект для запуска локального Xray-клиента и выдачи SOCKS5-прокси для подключения Telegram/aiogram через VLESS-конфигурацию.

## Что делает проект

- использует готовый файл `new_config.json` с конфигурацией Xray;
- поднимает локальный SOCKS5-прокси на `127.0.0.1:1080`;
- выставляет переменную окружения `TELEGRAM_PROXY_URL=socks5://127.0.0.1:1080`;
- пишет логи Xray в `logs/xray_proxy.log`;
- умеет переиспользовать уже запущенный `xray.exe`, если порт занят именно им;
- содержит простой deploy-скрипт `start-xray-monitor.sh` для обновления проекта через `git pull` и перезапуска systemd-сервиса.

## Структура

```text
.
├── new_config.json              # готовый клиентский конфиг Xray
├── telegram_proxy_client.py     # Python-обёртка для запуска локального Xray SOCKS5
├── start-xray-monitor.sh        # скрипт обновления и перезапуска systemd-сервиса
├── .env                         # локальные секреты и настройки, не коммитить
├── .env_example                 # пример переменных окружения
├── xray/                        # директория с бинарником Xray
└── logs/                        # логи запуска Xray
```

## Переменные окружения

Скопируй пример и заполни реальные значения:

```bash
cp .env_example .env
```

Основные переменные:

| Переменная | Назначение |
|---|---|
| `xray_url` | URL подписки или источника конфигурации Xray |
| `X-Hwid` | идентификатор устройства/клиента для запроса подписки |
| `port_client_xray` | локальный порт SOCKS5, по умолчанию `1080` |
| `bin_xray` | путь к бинарному файлу Xray |
| `config_xray` | путь к системному конфигу Xray |
| `TELEGRAM_PROXY_URL` | URL локального SOCKS5-прокси для Telegram |

> В текущем `telegram_proxy_client.py` напрямую используются `port_client_xray` и `TELEGRAM_PROXY_URL`. Остальные переменные нужны для связанной логики мониторинга/генерации конфига, если она есть в проекте.

## Подготовка

Установи зависимости Python:

```bash
pip install python-dotenv
```

Также в проекте должен быть локальный модуль `log_utils.py` с функцией `get_logger`.

Скачай или положи бинарник Xray:

```text
Linux/macOS: ./xray/xray
Windows:     ./xray/xray.exe
```

Для Linux/macOS выдай права на запуск:

```bash
chmod +x ./xray/xray
```

## Запуск локального прокси

```bash
python telegram_proxy_client.py
```

После успешного запуска будет доступен прокси:

```text
socks5://127.0.0.1:1080
```

Остановить процесс можно через `Ctrl+C`.

## Использование в коде

```python
from telegram_proxy_client import TelegramProxyClient

async with TelegramProxyClient() as proxy_client:
    proxy_url = proxy_client.proxy_url
    print(proxy_url)
    # здесь можно запускать Telegram/aiogram-клиент с этим proxy_url
```

## Deploy-скрипт

Файл `start-xray-monitor.sh` выполняет:

```bash
systemctl stop xray-monitor
cd /root/Project/xray-monitor
git pull
systemctl start xray-monitor
```

Перед использованием проверь, что путь `/root/Project/xray-monitor` и имя systemd-сервиса `xray-monitor` соответствуют твоему серверу.

## Важные замечания

- Не коммить `.env`, потому что там находятся приватный URL подписки, HWID и локальные пути.
- `new_config.json` тоже может содержать приватные параметры подключения: сервер, UUID пользователя и TLS/gRPC-настройки.
- На Windows модуль умеет определить процесс, занявший порт, через `netstat` и `tasklist`.
- На Linux/macOS владелец порта сейчас не определяется, но проверка занятости порта всё равно выполняется.
- Если Xray стартует и сразу завершается, смотри лог: `logs/xray_proxy.log`.
