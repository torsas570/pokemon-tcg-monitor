#!/usr/bin/env python3
"""
Monitor Pokémon TCG — detecta nuevos productos del 30 ANIVERSARIO en múltiples
tiendas online. Solo notifica productos cuyo título contenga alguna keyword
de `required_keywords` (Mega Evolution, Ascended Heroes, Pokemon Day 2026,
30 aniversario, ME01, etc.).

Doble prioridad solo para ordenar el chequeo:
- priority="high"  → tiendas con cases / preventas internacionales.
- priority="medium"→ tiendas españolas generalistas.
"""

import json
import hashlib
import time
import logging
import os
import sys
import argparse
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "monitor.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

PRIORITY_EMOJI = {"high": "🚨", "medium": "📦", "low": "🔍"}


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def extract_products_html(html, site_cfg):
    soup = BeautifulSoup(html, "html.parser")
    products = []
    items = soup.select(site_cfg["selector"])
    for item in items:
        title_el = item.select_one(site_cfg["title_selector"])
        title = title_el.get_text(strip=True) if title_el else "Sin título"

        link_el = item.select_one(site_cfg["link_selector"])
        link = link_el.get("href", "") if link_el else ""
        if link and not link.startswith("http"):
            link = urljoin(site_cfg["url"], link)

        price_el = item.select_one(site_cfg["price_selector"])
        price = price_el.get_text(strip=True) if price_el else "Precio no disponible"

        uid = hashlib.md5(f"{title}{link}".encode()).hexdigest()
        products.append({"uid": uid, "title": title, "link": link, "price": price})
    return products


def extract_products_api(data):
    """WooCommerce Store API genérica."""
    products = []
    items = data if isinstance(data, list) else data.get("products", [])
    for item in items:
        title = item.get("name", "Sin título")
        link = item.get("permalink") or item.get("url", "")
        prices = item.get("prices", {}) or {}
        raw_price = prices.get("price", "0")
        currency = prices.get("currency_symbol", "€")
        price = f"{int(raw_price) / 100:.2f}{currency}" if raw_price else "Precio no disponible"
        uid = hashlib.md5(f"{item.get('id', '')}{title}".encode()).hexdigest()
        products.append({"uid": uid, "title": title, "link": link, "price": price})
    return products


def send_telegram(bot_token, chat_id, message):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        log.error(f"Error enviando Telegram: {resp.status_code} {resp.text}")
    else:
        log.info("Notificación Telegram enviada")


def matches_keywords(title, keywords):
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


def check_site(site_cfg, state, config):
    name = site_cfg["name"]
    url = site_cfg["url"]
    site_type = site_cfg.get("type", "html")
    required_keywords = config.get("required_keywords", [])
    log.info(f"[{site_cfg.get('priority', 'medium').upper()}] {name}: {url}")
    try:
        headers = {"User-Agent": config["user_agent"]}
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        if site_type == "api":
            products = extract_products_api(resp.json())
        else:
            products = extract_products_html(resp.text, site_cfg)
    except Exception as e:
        log.error(f"  Error {name}: {e}")
        return []

    # Filtrar SOLO productos del 30 aniversario
    if required_keywords:
        filtered = [p for p in products if matches_keywords(p["title"], required_keywords)]
        log.info(f"  {name}: {len(products)} detectados, {len(filtered)} matchean 30 aniv")
        products = filtered
    else:
        log.info(f"  {name}: {len(products)} productos detectados")

    if not products:
        return []

    prev_uids = set(state.get(name, []))
    current_uids = {p["uid"] for p in products}
    new_products = [p for p in products if p["uid"] not in prev_uids]
    state[name] = list(current_uids)

    if not prev_uids:
        log.info(f"  {name}: primera ejecución, guardando baseline ({len(products)} prods 30 aniv)")
        # En primera ejecución sí notificar productos del 30 aniv (son los que nos importan)
        return new_products
    return new_products


def format_notification(site_name, priority, new_products):
    emoji = PRIORITY_EMOJI.get(priority, "🔔")
    lines = [f"🔥 <b>30 ANIV — {site_name}</b> {emoji} [{priority.upper()}]\n"]
    for p in new_products[:10]:
        lines.append(f"• <b>{p['title']}</b>")
        lines.append(f"  💰 {p['price']}")
        if p["link"]:
            lines.append(f"  🔗 {p['link']}")
        lines.append("")
    if len(new_products) > 10:
        lines.append(f"... y {len(new_products) - 10} más")
    return "\n".join(lines)


def run_once(priority_filter=None):
    config = load_config()
    state = load_state()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or config["telegram_bot_token"]
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or config["telegram_chat_id"]

    if bot_token == "TU_BOT_TOKEN_AQUI":
        log.error("⚠️  Configura Telegram: python3 setup_telegram.py")
        sys.exit(1)

    sites = config["sites"]
    if priority_filter:
        sites = [s for s in sites if s.get("priority", "medium") == priority_filter]
        log.info(f"Filtro de prioridad activo: solo '{priority_filter}' ({len(sites)} sitios)")

    # Procesar HIGH primero
    sites_sorted = sorted(sites, key=lambda s: 0 if s.get("priority") == "high" else 1)

    all_new = {}
    for site_cfg in sites_sorted:
        new_products = check_site(site_cfg, state, config)
        if new_products:
            all_new[site_cfg["name"]] = (site_cfg.get("priority", "medium"), new_products)

    save_state(state)

    if not all_new:
        log.info("Sin productos nuevos en esta revisión")
        return

    for site_name, (priority, products) in all_new.items():
        msg = format_notification(site_name, priority, products)
        log.info(f"Nuevos 30 aniv en {site_name} [{priority}]: {len(products)}")
        send_telegram(bot_token, chat_id, msg)


def run_loop(priority_filter=None):
    config = load_config()
    if priority_filter == "high":
        interval = config.get("check_interval_high_minutes", 5) * 60
    else:
        interval = config.get("check_interval_minutes", 15) * 60
    log.info(f"Monitor en bucle (cada {interval // 60} min, filtro={priority_filter or 'todos'})")
    while True:
        run_once(priority_filter=priority_filter)
        log.info(f"Esperando {interval // 60} minutos...")
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Ejecutar en bucle continuo")
    parser.add_argument("--priority", choices=["high", "medium"], help="Filtrar por prioridad")
    args = parser.parse_args()

    if args.loop:
        run_loop(priority_filter=args.priority)
    else:
        run_once(priority_filter=args.priority)
