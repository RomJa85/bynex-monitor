import getpass
import hashlib
import json
import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(BASE_DIR, "secrets.json")


def main():
    print("BYNEX Monitor — первоначальная настройка")
    print("Токен вводится скрыто и не отображается на экране.\n")

    token = input("Telegram bot token: ").strip()
    if not token:
        print("Токен не введён. Настройка отменена.")
        return

    chat_id = input("Telegram chat_id: ").strip()
    if not chat_id:
        print("chat_id не введён. Настройка отменена.")
        return

    password = getpass.getpass("Придумай пароль управления монитором: ")
    confirmation = getpass.getpass("Повтори пароль: ")
    if len(password) < 12:
        print("Пароль должен содержать не менее 12 символов.")
        return
    if password != confirmation:
        print("Пароли не совпадают.")
        return

    salt = secrets.token_bytes(16)
    password_hash = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1
    ).hex()

    data = {
        "telegram_token": token,
        "chat_id": chat_id,
        "password_salt": salt.hex(),
        "password_hash": password_hash,
    }

    temporary = SECRETS_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(temporary, SECRETS_FILE)

    try:
        os.chmod(SECRETS_FILE, 0o600)
    except OSError:
        pass

    print("\nНастройки сохранены в secrets.json.")
    print("Не отправляй этот файл и не загружай его в GitHub.")
    print("Теперь можно запускать server.py.")


if __name__ == "__main__":
    main()
