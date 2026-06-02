#!/usr/bin/env python3
"""
Monitor Pokémon TCG — 30 ANIVERSARIO.

Features:
- Filtro por keywords (solo notifica matches en `required_keywords`)
- Detección de RESTOCK (producto agotado vuelve a stock)
- Filtro de productos out-of-stock (configurable con notify_only_in_stock)
- Doble prioridad para ordenar el chequeo (high=cases, medium=ES)
"""

import json
import hashlib
import time
import logging
import os
import sys
import argparse
import html as html_mod
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
OOS_KEYWORDS = ["agotado", "sold out", "out of stock", "vendido", "no disponible", "rupture de stock"]


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


def build_headers(user_agent, is_api=False):
    # Accept-Encoding sin "br": brotli no siempre está instalado y dejaría el
    # cuerpo sin descomprimir (parseo JSON fallaría con "Expecting value").
    headers = {
        "User-Agent": user_agent,
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    }
    if is_api:
        # Petición tipo XHR: muchas tiendas tras Cloudflare/anti-bot solo sirven
        # el JSON si la cabecera parece una llamada AJAX y no una navegación.
        headers.update({
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })
    else:
        headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })
    return headers


def detect_html_in_stock(item):
    """Detecta in_stock en un nodo HTML buscando marcadores típicos."""
    classes = " ".join(item.get("class", [])).lower()
    if any(k in classes for k in ["out-of-stock", "sold-out", "outofstock", "agotado"]):
        return False
    text = item.get_text(" ", strip=True).lower()
    if any(k in text for k in OOS_KEYWORDS):
        return False
    return True


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

        in_stock = detect_html_in_stock(item)
        uid = hashlib.md5(f"{title}{link}".encode()).hexdigest()
        products.append({"uid": uid, "title": title, "link": link, "price": price, "in_stock": in_stock})
    return products


def extract_products_api(data, base_url=""):
    """Detección automática: Shopify products.json o WooCommerce Store API."""
    products = []

    # Shopify products.json
    if isinstance(data, dict) and "products" in data and data["products"] and "handle" in data["products"][0]:
        from urllib.parse import urlparse
        base = ""
        if base_url:
            p = urlparse(base_url)
            base = f"{p.scheme}://{p.netloc}"
        for item in data["products"]:
            title = html_mod.unescape(item.get("title", "Sin título"))
            handle = item.get("handle", "")
            link = f"{base}/products/{handle}" if handle else ""
            variants = item.get("variants") or []
            price = "Precio no disponible"
            in_stock = False
            if variants:
                p_raw = variants[0].get("price", "")
                if p_raw:
                    price = f"{p_raw}€"
                in_stock = any(v.get("available", False) for v in variants)
            uid = hashlib.md5(f"{item.get('id', '')}{title}".encode()).hexdigest()
            products.append({"uid": uid, "title": title, "link": link, "price": price, "in_stock": in_stock})
        return products

    # WooCommerce Store API
    items = data if isinstance(data, list) else data.get("products", [])
    for item in items:
        title = html_mod.unescape(item.get("name", "Sin título"))
        link = item.get("permalink") or item.get("url", "")
        prices = item.get("prices", {}) or {}
        raw_price = prices.get("price") or "0"
        currency = prices.get("currency_symbol", "€")
        try:
            price = f"{int(raw_price) / 100:.2f}{currency}"
        except (ValueError, TypeError):
            price = "Precio no disponible"
        in_stock = item.get("is_in_stock", item.get("has_stock", True))
        uid = hashlib.md5(f"{item.get('id', '')}{title}".encode()).hexdigest()
        products.append({"uid": uid, "title": title, "link": link, "price": price, "in_stock": in_stock})
    return products


def send_telegram(bot_token, chat_id, message):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False}
    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        log.error(f"Error enviando Telegram: {resp.status_code} {resp.text}")
    else:
        log.info("Notificación Telegram enviada")


def matches_keywords(title, keywords):
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


def normalize_state(raw):
    """Migra state antigua (list de uids) al nuevo schema {uid: {in_stock: bool}}."""
    if isinstance(raw, list):
        return {uid: {"in_stock": True} for uid in raw}
    if isinstance(raw, dict):
        return raw
    return {}


def check_site(site_cfg, state, config):
    name = site_cfg["name"]
    url = site_cfg["url"]
    site_type = site_cfg.get("type", "html")
    is_api = site_type == "api"
    required_keywords = config.get("required_keywords", [])
    notify_only_in_stock = config.get("notify_only_in_stock", True)
    log.info(f"[{site_cfg.get('priority', 'medium').upper()}] {name}: {url}")

    headers = build_headers(config["user_agent"], is_api=is_api)
    if is_api:
        from urllib.parse import urlparse
        p = urlparse(url)
        headers["Referer"] = f"{p.scheme}://{p.netloc}/"

    products = None
    last_err = None
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            if is_api:
                ctype = resp.headers.get("Content-Type", "").lower()
                if "json" not in ctype:
                    # Cloudflare/anti-bot devolvió HTML en vez del JSON
                    raise ValueError(f"respuesta no-JSON (Content-Type: {ctype or 'desconocido'})")
                products = extract_products_api(resp.json(), base_url=url)
            else:
                products = extract_products_html(resp.text, site_cfg)
            break
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(2)

    if products is None:
        # Fallo persistente (normalmente bloqueo por IP de la web): aviso, no error
        log.warning(f"  {name} no disponible: {last_err}")
        return []

    if required_keywords:
        filtered = [p for p in products if matches_keywords(p["title"], required_keywords)]
        log.info(f"  {name}: {len(products)} detectados, {len(filtered)} matchean 30 aniv")
        products = filtered
    else:
        log.info(f"  {name}: {len(products)} productos detectados")

    if not products:
        return []

    raw_prev = state.get(name)
    is_first_run = raw_prev is None
    site_state = normalize_state(raw_prev)

    alerts = []
    for p in products:
        uid = p["uid"]
        prev = site_state.get(uid)
        if prev is None:
            # Producto nuevo
            if not is_first_run:
                if p["in_stock"] or not notify_only_in_stock:
                    alerts.append({**p, "alert_type": "new"})
            else:
                # Primera ejecución: solo notifica los que están en stock (baseline)
                if p["in_stock"]:
                    alerts.append({**p, "alert_type": "new"})
        else:
            # Producto conocido — detectar restock
            was_oos = not prev.get("in_stock", True)
            if was_oos and p["in_stock"]:
                alerts.append({**p, "alert_type": "restock"})
        site_state[uid] = {"in_stock": p["in_stock"]}

    state[name] = site_state
    return alerts


def format_notification(site_name, priority, alerts):
    emoji = PRIORITY_EMOJI.get(priority, "🔔")
    has_restock = any(a["alert_type"] == "restock" for a in alerts)
    header = "🔄 RESTOCK + " if has_restock else ""
    lines = [f"🔥 {header}<b>30 ANIV — {site_name}</b> {emoji} [{priority.upper()}]\n"]
    for p in alerts[:10]:
        tag = "🔄 VUELVE" if p["alert_type"] == "restock" else "🆕 NUEVO"
        stock_mark = "" if p["in_stock"] else " ⚠️ AGOTADO"
        lines.append(f"• {tag}{stock_mark} <b>{p['title']}</b>")
        lines.append(f"  💰 {p['price']}")
        if p["link"]:
            lines.append(f"  🔗 {p['link']}")
        lines.append("")
    if len(alerts) > 10:
        lines.append(f"... y {len(alerts) - 10} más")
    return "\n".join(lines)


def run_once(priority_filter=None):
    config = load_config()
    state = load_state()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or config["telegram_bot_token"]
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or config["telegram_chat_id"]

    if bot_token in ("TU_BOT_TOKEN_AQUI", "USE_GITHUB_SECRET", "", None):
        log.error("⚠️  Falta TELEGRAM_BOT_TOKEN (env o config.json)")
        sys.exit(1)

    sites = config["sites"]
    if priority_filter:
        sites = [s for s in sites if s.get("priority", "medium") == priority_filter]
        log.info(f"Filtro de prioridad activo: solo '{priority_filter}' ({len(sites)} sitios)")

    sites_sorted = sorted(sites, key=lambda s: 0 if s.get("priority") == "high" else 1)

    all_alerts = {}
    for site_cfg in sites_sorted:
        alerts = check_site(site_cfg, state, config)
        if alerts:
            all_alerts[site_cfg["name"]] = (site_cfg.get("priority", "medium"), alerts)

    save_state(state)

    if not all_alerts:
        log.info("Sin alertas en esta revisión")
        return

    for site_name, (priority, alerts) in all_alerts.items():
        msg = format_notification(site_name, priority, alerts)
        n_new = sum(1 for a in alerts if a["alert_type"] == "new")
        n_re = sum(1 for a in alerts if a["alert_type"] == "restock")
        log.info(f"Alertas {site_name} [{priority}]: {n_new} nuevos + {n_re} restock")
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
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--priority", choices=["high", "medium"])
    args = parser.parse_args()
    if args.loop:
        run_loop(priority_filter=args.priority)
    else:
        run_once(priority_filter=args.priority)
