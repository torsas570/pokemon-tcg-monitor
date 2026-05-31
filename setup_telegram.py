#!/usr/bin/env python3
"""Configuración interactiva de Telegram para el bot Pokémon TCG."""

import json
import requests
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


def main():
    print("=" * 50)
    print("  CONFIGURACIÓN DE TELEGRAM — POKÉMON TCG BOT")
    print("=" * 50)
    print()
    print("PASO 1: Crear bot en Telegram")
    print("  1. Abre Telegram → @BotFather → /newbot")
    print("  2. Nombre: 'Pokemon TCG Monitor'")
    print("  3. Username: ej. 'pokemon_tcg_monitor_bot'")
    print("  4. Copia el token que te da BotFather")
    print()
    bot_token = input("Pega aquí tu BOT TOKEN: ").strip()

    print()
    print("PASO 2: Obtener Chat ID")
    print("  1. Busca tu bot en Telegram y envíale 'hola'")
    print("  2. Pulsa Enter aquí...")
    input()

    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    try:
        data = requests.get(url, timeout=10).json()
        if data.get("result"):
            chat_id = str(data["result"][-1]["message"]["chat"]["id"])
            name = data["result"][-1]["message"]["chat"].get("first_name", "")
            print(f"✅ Chat ID detectado: {chat_id} ({name})")
        else:
            chat_id = input("No detectado. Introduce Chat ID manualmente: ").strip()
    except Exception as e:
        print(f"Error: {e}")
        chat_id = input("Introduce Chat ID manualmente: ").strip()

    with open(CONFIG_PATH) as f:
        config = json.load(f)
    config["telegram_bot_token"] = bot_token
    config["telegram_chat_id"] = chat_id
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

    print("\nGuardado. Enviando mensaje de prueba...")
    test_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "✅ Bot Pokémon TCG configurado!\n🚨 Recibirás alertas HIGH (cases) y 📦 MEDIUM (boxes ES).\n🔥 Las que matcheen 30 aniv/Mega Evolution se marcarán como URGENTE.",
    }
    resp = requests.post(test_url, json=payload, timeout=10)
    print("✅ Mensaje de prueba enviado" if resp.status_code == 200 else f"❌ Error {resp.text}")

    print()
    print("=" * 50)
    print("USO:")
    print("  python3 monitor.py                  # 1 pasada, todas las tiendas")
    print("  python3 monitor.py --priority high  # solo cases / 30 aniv")
    print("  python3 monitor.py --loop           # bucle continuo (cada 15 min)")
    print("  python3 monitor.py --loop --priority high  # bucle solo HIGH (cada 5 min)")
    print("=" * 50)


if __name__ == "__main__":
    main()
