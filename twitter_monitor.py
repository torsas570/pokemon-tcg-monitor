#!/usr/bin/env python3
"""
Monitor de Twitter/X — usa Nitter RSS para vigilar cuentas que avisan de drops
Pokémon TCG en España (sin necesidad de API oficial de X).

⚠️ Nitter es frágil: muchos mirrors caen sin aviso. Este script intenta varios
en cadena y avisa por Telegram si TODOS fallan (para que sepas que necesitas
actualizar la lista de mirrors).
"""
import json
import os
import sys
import re
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

BASE = Path(__file__).parent
CONFIG = json.load(open(BASE / "config.json"))
TWEETS_STATE = BASE / "tweets_state.json"

# Cuentas a monitorizar
ACCOUNTS = [
    "pokestock_es",       # avisos drops Pokémon ES
    "PokeStockAlert",     # alertas restock
    "PokeBeach",          # noticias oficiales
]

# Nitter mirrors — ORDEN IMPORTA, prueba el primero que responda
NITTER_MIRRORS = [
    "nitter.net",
    "nitter.privacydev.net",
    "nitter.poast.org",
    "xcancel.com",
]

# Keywords para filtrar (solo notifica tweets que matcheen alguna)
KEYWORDS = [w.lower() for w in CONFIG.get("required_keywords", [])] + [
    "drop", "restock", "stock", "disponible", "preventa", "preorder",
    "pokemon day", "30 aniv", "mega evolution",
]

bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or CONFIG["telegram_bot_token"]
chat_id = os.environ.get("TELEGRAM_CHAT_ID") or CONFIG["telegram_chat_id"]


def load_state():
    return json.load(open(TWEETS_STATE)) if TWEETS_STATE.exists() else {}


def save_state(s):
    json.dump(s, open(TWEETS_STATE, "w"), indent=2)


def fetch_rss(account):
    """Intenta cada mirror hasta encontrar uno que responda."""
    for mirror in NITTER_MIRRORS:
        url = f"https://{mirror}/{account}/rss"
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and "<rss" in r.text[:500]:
                return r.text, mirror
        except Exception:
            continue
    return None, None


def parse_rss(xml_text):
    """Devuelve lista de tweets [(id, text, link, pubdate)]."""
    tweets = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.iter("item"):
            link = (item.findtext("link") or "").strip()
            title = (item.findtext("title") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            tid = link.split("/status/")[-1].split("#")[0] if "/status/" in link else link
            tweets.append((tid, title, link, pub))
    except ET.ParseError:
        pass
    return tweets


def matches(text):
    t = text.lower()
    return any(k in t for k in KEYWORDS)


def send_telegram(msg):
    requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=15,
    )


def main():
    state = load_state()
    new_alerts = []
    all_failed = []

    for acc in ACCOUNTS:
        xml, mirror = fetch_rss(acc)
        if not xml:
            all_failed.append(acc)
            print(f"[FAIL] @{acc} — todos los mirrors caídos")
            continue
        tweets = parse_rss(xml)
        seen = set(state.get(acc, []))
        is_first = not seen
        new_for_acc = []
        for tid, text, link, pub in tweets:
            if tid in seen:
                continue
            seen.add(tid)
            if is_first:
                continue  # primer run: baseline silent
            if matches(text):
                new_for_acc.append((acc, text, link))
        state[acc] = list(seen)[-100:]  # mantener últimos 100 ids
        new_alerts.extend(new_for_acc)
        print(f"[OK]   @{acc} via {mirror}: {len(tweets)} tweets, {len(new_for_acc)} matchean")

    save_state(state)

    if all_failed and len(all_failed) == len(ACCOUNTS):
        send_telegram(
            "⚠️ <b>Twitter monitor caído</b>\n"
            "Todos los Nitter mirrors están abajo. Actualiza NITTER_MIRRORS en twitter_monitor.py."
        )

    for acc, text, link in new_alerts:
        msg = f"🐦 <b>@{acc}</b>\n\n{text[:500]}\n\n🔗 {link}"
        send_telegram(msg)
        print(f"Notificado: {acc} — {text[:80]}")


if __name__ == "__main__":
    main()
