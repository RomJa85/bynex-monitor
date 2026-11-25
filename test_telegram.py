import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode


SECRETS_FILE = Path(__file__).with_name("secrets.json")


def main():
    if not SECRETS_FILE.exists():
        print("ОШИБКА: рядом с test_telegram.py не найден secrets.json.")
        return 1

    try:
        with SECRETS_FILE.open("r", encoding="utf-8") as file:
            secrets = json.load(file)
    except (OSError, json.JSONDecodeError):
        print("ОШИБКА: не удалось прочитать secrets.json.")
        return 1

    token = str(secrets.get("telegram_token", "")).strip()

    # Поддерживаем распространённые варианты имени поля chat_id.
    chat_id = (
        secrets.get("chat_id")
        or secrets.get("telegram_chat_id")
        or secrets.get("telegram_chat")
    )
    chat_id = str(chat_id).strip() if chat_id is not None else ""

    if not token or not chat_id:
        print("ОШИБКА: в secrets.json не найдены telegram_token и/или chat_id.")
        print("Проверь названия полей, не публикуя их значения.")
        return 1

    message = "BYNEX Monitor: тестовое сообщение. Подключение Telegram работает."
    body = urlencode({
        "chat_id": chat_id,
        "text": message
    }).encode("utf-8")

    # Токен необходим Telegram Bot API в URL. Не выводим URL и текст исключений,
    # чтобы случайно не раскрыть секрет в консоли.
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "BYNEX-Monitor-Telegram-Test/1.0"
        },
        method="POST"
    )

    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            result = json.loads(error.read().decode("utf-8"))
            description = result.get("description", "Telegram вернул HTTP-ошибку.")
        except Exception:
            description = "Telegram вернул HTTP-ошибку."
        print(f"ОШИБКА Telegram: {description}")
        return 1
    except (URLError, TimeoutError, OSError):
        print("ОШИБКА: не удалось связаться с Telegram API. Токен не выведен.")
        return 1
    except (UnicodeDecodeError, json.JSONDecodeError):
        print("ОШИБКА: получен некорректный ответ Telegram.")
        return 1

    if result.get("ok") is True:
        print("УСПЕХ: Telegram принял сообщение.")
        print("Проверь чат с ботом Bynex_monitor.")
        return 0

    print("ОШИБКА Telegram: запрос отклонён.")
    description = result.get("description")
    if description:
        print(description)
    return 1


if __name__ == "__main__":
    sys.exit(main())
