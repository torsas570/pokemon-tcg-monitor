#!/usr/bin/env python3
"""Heartbeat diario — manda a Telegram resumen del estado del bot."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = Path(__file__).parent
CONFIG = json.load(open(BASE / "config.json"))
STATE_PATH = BASE / "state.json"

bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or CONFIG["telegram_bot_token"]
chat_id = os.environ.get("TELEGRAM_CHAT_ID") or CONFIG["telegram_chat_id"]

state = json.load(open(STATE_PATH)) if STATE_PATH.exists() else {}

RESERVED = ("__health__", "__health_meta__")
health = state.get("__health__", {})
sites = {k: v for k, v in state.items() if k not in RESERVED and isinstance(v, dict)}

n_sites_cfg = len(CONFIG["sites"])
n_sites_tracked = len(sites)
total_products = sum(len(v) for v in sites.values())
oos = sum(
    1
    for site in sites.values()
    for p in site.values() if isinstance(p, dict) and not p.get("in_stock", True)
)
in_stock = total_products - oos

# Resumen de salud: aquí es donde debe vivir el estado de las tiendas caídas.
# El monitor solo interrumpe en el momento si algo es grave; el repaso tranquilo
# de "qué llevo roto" va una vez al día, en este mensaje.
caidas = sorted(n for n, h in health.items() if h.get("fails", 0) >= 3)
ciegas = sorted(n for n, h in health.items() if h.get("empty_streak", 0) >= 3)


def _lista(nombres, limite=8):
    txt = " · ".join(nombres[:limite])
    resto = len(nombres) - limite
    return txt + (f" y {resto} más" if resto > 0 else "")


salud = ""
if caidas:
    salud += f"\n⚠️ <b>Sin responder ({len(caidas)})</b>: {_lista(caidas)}"
if ciegas:
    salud += f"\n👻 <b>Ciegas, 0 productos ({len(ciegas)})</b>: {_lista(ciegas)}"
if not salud:
    salud = "\n💚 Todas las tiendas responden"

msg = (
    f"💓 <b>Heartbeat Pokémon TCG 30 Aniv</b>\n"
    f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
    f"✅ Bot vivo y funcionando\n"
    f"🏪 Tiendas configuradas: {n_sites_cfg}\n"
    f"📊 Tiendas con datos: {n_sites_tracked}\n"
    f"📦 Productos 30 aniv tracked: {total_products}\n"
    f"  • En stock: {in_stock}\n"
    f"  • Agotados: {oos}\n"
    f"{salud}\n\n"
    f"Si esto no te llega cada noche → el bot está caído. Revisa GitHub Actions."
)

resp = requests.post(
    f"https://api.telegram.org/bot{bot_token}/sendMessage",
    json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
    timeout=15,
)
if resp.status_code != 200:
    print(f"Error: {resp.text}")
    sys.exit(1)
print("Heartbeat enviado")
