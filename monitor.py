#!/usr/bin/env python3
"""
Monitor Pokémon TCG — 30 ANIVERSARIO.

Features:
- Filtro por keywords (solo notifica matches en `required_keywords`)
- Detección de RESTOCK (producto agotado vuelve a stock)
- Filtro de productos out-of-stock (configurable con notify_only_in_stock)
- Doble prioridad para ordenar el chequeo (high=cases, medium=ES)
- Prioridad de PRODUCTO: las UPC / booster box / cases van marcadas 🔥 y SIEMPRE
  las primeras del aviso, para que un drop grande no las deje fuera del corte.
- Chequeo en PARALELO: una pasada de ~120 tiendas baja de ~60s a ~6s, que es lo
  que de verdad marca la cadencia real del bucle continuo.
- Envío a Telegram a prueba de fallos: reintentos con `retry_after`, troceo de
  mensajes largos y el state de un sitio SOLO se persiste si su aviso salió.
"""

import json
import hashlib
import time
import logging
import os
import sys
import argparse
import html as html_mod
from concurrent.futures import ThreadPoolExecutor
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
HEALTH_KEY = "__health__"  # clave reservada en state para la salud de las tiendas (no es un sitio)
DEFAULT_HEALTH_FAIL_THRESHOLD = 3  # fallos seguidos antes de avisar de posible bloqueo/caída
DEFAULT_EMPTY_THRESHOLD = 5        # pasadas a 0 productos (habiendo tenido catálogo) antes de avisar
DEFAULT_MAX_WORKERS = 12           # peticiones simultáneas
DEFAULT_TIMEOUT = 20               # segundos por petición
DEFAULT_MAX_ALERTS = 20            # productos como mucho por aviso de tienda
TELEGRAM_MAX_CHARS = 3800          # el límite real son 4096; dejamos margen


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


def _record_health(state, name, ok, error=None, n_products=None):
    """Actualiza el contador de fallos consecutivos de una tienda en el state.

    `n_products` es el número CRUDO de productos devueltos (antes del filtro de
    keywords) y sirve para detectar la tienda CIEGA por API: la petición va bien
    (HTTP 200, JSON válido) pero la colección devuelve 0 productos. Si esa tienda
    llegó a tener catálogo alguna vez, es que el handle murió o Shopify Markets
    nos está sirviendo una lista vacía -> 0 avisos para siempre, en silencio y
    con el run en verde. Una colección que SIEMPRE estuvo vacía (p. ej. una
    preventa que aún no ha abierto) no dispara nada: nunca tuvo max_products.
    """
    health = state.setdefault(HEALTH_KEY, {})
    h = health.setdefault(name, {"fails": 0, "alerted": False, "last_error": None})
    if ok:
        h["fails"] = 0
        h["last_error"] = None
        if n_products is not None:
            best = h.get("max_products", 0)
            if n_products > 0:
                h["max_products"] = max(best, n_products)
                h["empty_streak"] = 0
            elif best > 0:
                h["empty_streak"] = h.get("empty_streak", 0) + 1
    else:
        h["fails"] = h.get("fails", 0) + 1
        h["last_error"] = error


def _collect_health_alerts(state, config):
    """Devuelve [(mensaje, deshacer)] SOLO en las transiciones (cae -> avisa /
    se recupera -> avisa), para no repetir el aviso en cada pasada. Muta los flags
    en el state; `deshacer` los restaura si el envío a Telegram falla, para que el
    aviso se reintente en la próxima pasada en vez de perderse."""
    threshold = config.get("health_fail_threshold", DEFAULT_HEALTH_FAIL_THRESHOLD)
    empty_threshold = config.get("health_empty_threshold", DEFAULT_EMPTY_THRESHOLD)
    health = state.get(HEALTH_KEY, {})
    out = []

    def flip(h, key, value):
        prev = h.get(key)
        h[key] = value
        return lambda: h.__setitem__(key, prev)

    for name, h in health.items():
        fails = h.get("fails", 0)
        if fails >= threshold and not h.get("alerted", False):
            out.append((
                f"⚠️ <b>Aviso de monitor</b>\n\n"
                f"<b>{name}</b> no responde tras {fails} intentos seguidos "
                f"(posible bloqueo de IP o caída de la web).\n"
                f"⚠️ Puede que te estés perdiendo restocks de esta tienda.\n"
                f"Último error: <code>{h.get('last_error')}</code>",
                flip(h, "alerted", True),
            ))
        elif fails == 0 and h.get("alerted", False):
            out.append((f"✅ <b>{name}</b> vuelve a responder con normalidad.",
                        flip(h, "alerted", False)))

        # Tienda CIEGA: responde OK pero lleva N pasadas devolviendo 0 productos
        # habiendo tenido catálogo antes.
        empty = h.get("empty_streak", 0)
        if empty >= empty_threshold and not h.get("empty_alerted", False):
            out.append((
                f"👻 <b>Aviso de monitor</b>\n\n"
                f"<b>{name}</b> responde correctamente pero lleva {empty} pasadas "
                f"devolviendo <b>0 productos</b> (antes llegó a tener "
                f"{h.get('max_products', 0)}).\n"
                f"⚠️ Probablemente la colección se renombró o la tienda ya no la sirve: "
                f"está CIEGA, no te avisará de nada. Revisa la URL en config.json.",
                flip(h, "empty_alerted", True),
            ))
        elif empty == 0 and h.get("empty_alerted", False):
            out.append((f"✅ <b>{name}</b> vuelve a devolver productos.",
                        flip(h, "empty_alerted", False)))
    return out


def build_headers(user_agent, is_api=False):
    # Accept-Encoding sin "br": brotli no siempre está instalado y dejaría el
    # cuerpo sin descomprimir (parseo JSON fallaría con "Expecting value").
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    }
    if is_api:
        # OJO: aquí NO se manda Accept-Language. Las tiendas Shopify con "Markets"
        # activado resuelven el mercado por ese header y, si pides es-ES en una tienda
        # US/UK, devuelven {"products": []} con HTTP 200 -> la tienda queda CIEGA sin
        # que salte ningún error (medido: Flipside Gaming 0 vs 124 productos,
        # Card-Binder 24 vs 28). Sin el header sirven el catálogo completo.
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
        # En HTML sí interesa el idioma (tiendas ES con páginas traducidas).
        headers.update({
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
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

    # Un elemento sin título es inservible: los bots filtran por keyword sobre el
    # título, así que nunca casaría. Si NINGUNO tiene título, los selectores están
    # obsoletos y la tienda está ciega: fallar para que salte el aviso de salud,
    # en vez de aparentar "0 productos relevantes" para siempre.
    usable = [p for p in products if p["title"] and p["title"] != "Sin título"]
    if products and not usable:
        raise ValueError(
            f"selectores obsoletos: {len(products)} elementos, ninguno con título"
        )
    if len(usable) < len(products):
        log.warning(
            f"  {len(products) - len(usable)} de {len(products)} elementos sin título "
            f"(title_selector incompleto), descartados"
        )
    return usable


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


def send_telegram(bot_token, chat_id, message, attempts=4):
    """Envía un mensaje y devuelve True/False según haya salido.

    Devolver el resultado es lo que permite NO dar por avisado un producto cuyo
    mensaje no llegó: quien llama solo persiste el state si esto devuelve True.
    Reintenta respetando el `retry_after` de los 429 (rate limit), que es el
    fallo más probable cuando un drop grande genera avisos de muchas tiendas.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False}
    for attempt in range(attempts):
        try:
            resp = requests.post(url, json=payload, timeout=20)
        except Exception as e:
            log.warning(f"Telegram, error de red ({e}), reintento {attempt + 1}/{attempts}")
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 200:
            log.info("Notificación Telegram enviada")
            return True
        if resp.status_code == 429:
            try:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                wait = 5
            log.warning(f"Telegram rate limit, esperando {wait}s")
            time.sleep(min(wait, 60) + 1)
            continue
        if 500 <= resp.status_code < 600:
            log.warning(f"Telegram {resp.status_code}, reintento {attempt + 1}/{attempts}")
            time.sleep(2 * (attempt + 1))
            continue
        # 400 y demás: el mensaje es inválido, reintentar no arregla nada
        log.error(f"Error enviando Telegram: {resp.status_code} {resp.text[:300]}")
        return False
    log.error("Telegram: agotados los reintentos, mensaje NO enviado")
    return False


def send_telegram_chunks(bot_token, chat_id, messages):
    """Envía una lista de trozos. True solo si TODOS salen."""
    ok = True
    for msg in messages:
        if not send_telegram(bot_token, chat_id, msg):
            ok = False
    return ok


def product_rank(p):
    """Orden de interés: 0 = UPC/booster box/case, 1 = ETB/premium, 2 = resto.

    El aviso se corta a `max_alerts_per_site` productos, así que sin ordenar una
    booster box podía quedar fuera del corte por detrás de un llavero.
    """
    if p.get("top_priority"):
        return 0
    if p.get("high_value"):
        return 1
    return 2


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


def fetch_site(site_cfg, config):
    """SOLO red y parseo. No toca el state, así puede correr en paralelo.

    Devuelve (site_cfg, productos|None, error). Sacar la parte de red fuera del
    state es lo que permite lanzar las ~120 tiendas a la vez: una pasada pasa de
    ~60s (secuencial, y varios minutos si hay tiendas caídas reintentando) a ~6s,
    que es lo que de verdad fija la cadencia real del bucle continuo.
    """
    name = site_cfg["name"]
    url = site_cfg["url"]
    is_api = site_cfg.get("type", "html") == "api"
    timeout = config.get("request_timeout_seconds", DEFAULT_TIMEOUT)

    headers = build_headers(config["user_agent"], is_api=is_api)
    if is_api:
        from urllib.parse import urlparse
        p = urlparse(url)
        headers["Referer"] = f"{p.scheme}://{p.netloc}/"

    last_err = None
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            if is_api:
                ctype = resp.headers.get("Content-Type", "").lower()
                if "json" not in ctype:
                    # Cloudflare/anti-bot devolvió HTML en vez del JSON
                    raise ValueError(f"respuesta no-JSON (Content-Type: {ctype or 'desconocido'})")
                return site_cfg, extract_products_api(resp.json(), base_url=url), None
            return site_cfg, extract_products_html(resp.text, site_cfg), None
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(2)
    log.warning(f"  {name} no disponible: {last_err}")
    return site_cfg, None, str(last_err)


def process_site(site_cfg, products, state, config):
    """Filtra por keywords y compara con el state.

    Devuelve (alertas, nuevo_state_del_sitio). NO escribe en `state`: quien llama
    solo lo persiste si el aviso de esta tienda llegó a Telegram; si el envío
    falla, el state viejo se conserva y el producto se vuelve a avisar en la
    siguiente pasada en vez de darse por visto y perderse para siempre.
    """
    name = site_cfg["name"]
    required_keywords = config.get("required_keywords", [])
    exclude_keywords = config.get("exclude_keywords", [])
    notify_only_in_stock = config.get("notify_only_in_stock", True)
    top_priority_keywords = config.get("top_priority_keywords", [])
    high_value_keywords = config.get("high_value_keywords", [])

    if required_keywords:
        filtered = [
            p for p in products
            if matches_keywords(p["title"], required_keywords)
            and not (exclude_keywords and matches_keywords(p["title"], exclude_keywords))
        ]
        n_excl = sum(
            1 for p in products
            if matches_keywords(p["title"], required_keywords)
            and exclude_keywords and matches_keywords(p["title"], exclude_keywords)
        )
        log.info(f"  {name}: {len(products)} detectados, {len(filtered)} matchean 30 aniv"
                 + (f" ({n_excl} descartados por idioma)" if n_excl else ""))
        products = filtered
    else:
        log.info(f"  {name}: {len(products)} productos detectados")

    raw_prev = state.get(name)
    is_first_run = raw_prev is None
    # COPIA: normalize_state devuelve el mismo dict que está dentro de `state`
    # cuando ya es un dict. Sin copiar, marcar un producto como visto mutaría el
    # state en el sitio aunque luego el envío a Telegram fallara, y el aviso se
    # perdería igualmente (que es justo lo que esto viene a evitar).
    site_state = dict(normalize_state(raw_prev))
    if not products:
        return [], site_state

    alerts = []
    for p in products:
        uid = p["uid"]
        prev = site_state.get(uid)
        p["top_priority"] = matches_keywords(p["title"], top_priority_keywords)
        p["high_value"] = matches_keywords(p["title"], high_value_keywords)
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

    return alerts, site_state


def format_notification(site_name, priority, alerts, config=None):
    """Devuelve una LISTA de mensajes (troceados para no pasar del límite de
    Telegram: un mensaje de más de 4096 caracteres se rechaza con un 400 y el
    aviso entero se perdía). Los productos van ordenados por interés."""
    config = config or {}
    max_alerts = config.get("max_alerts_per_site", DEFAULT_MAX_ALERTS)
    emoji = PRIORITY_EMOJI.get(priority, "🔔")
    has_restock = any(a["alert_type"] == "restock" for a in alerts)
    header = "🔄 RESTOCK + " if has_restock else ""
    # Lo más gordo primero: una booster box nunca debe caerse del corte.
    ordered = sorted(alerts, key=product_rank)
    shown, extra = ordered[:max_alerts], len(ordered) - max_alerts

    title_line = f"🔥 {header}<b>30 ANIV — {site_name}</b> {emoji} [{priority.upper()}]\n"
    blocks = []
    for p in shown:
        tag = "🔄 VUELVE" if p["alert_type"] == "restock" else "🆕 NUEVO"
        rank_mark = "🔥 " if p.get("top_priority") else ("🚨 " if p.get("high_value") else "")
        stock_mark = "" if p["in_stock"] else " ⚠️ AGOTADO"
        b = [f"• {rank_mark}{tag}{stock_mark} <b>{p['title']}</b>", f"  💰 {p['price']}"]
        if p["link"]:
            b.append(f"  🔗 {p['link']}")
        b.append("")
        blocks.append("\n".join(b))
    if extra > 0:
        blocks.append(f"... y {extra} más")

    # Troceo por bloques enteros: nunca se parte un producto por la mitad.
    msgs, cur = [], title_line
    for b in blocks:
        if len(cur) + len(b) + 1 > TELEGRAM_MAX_CHARS and cur != title_line:
            msgs.append(cur)
            cur = f"{title_line}<i>(continuación {len(msgs) + 1})</i>\n" + b + "\n"
        else:
            cur += b + "\n"
    msgs.append(cur)
    return msgs


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

    # --- 1) Red en PARALELO (sin tocar el state) ---
    workers = max(1, min(config.get("max_workers", DEFAULT_MAX_WORKERS), len(sites_sorted) or 1))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda s: fetch_site(s, config), sites_sorted))
    log.info(f"{len(sites_sorted)} tiendas consultadas en {time.time() - t0:.1f}s ({workers} hilos)")

    # --- 2) Proceso SECUENCIAL contra el state (evita carreras) ---
    pending = []
    for site_cfg, products, err in results:
        name = site_cfg["name"]
        if products is None:
            _record_health(state, name, ok=False, error=err)
            continue
        _record_health(state, name, ok=True, n_products=len(products))
        alerts, new_site_state = process_site(site_cfg, products, state, config)
        pending.append((name, site_cfg.get("priority", "medium"), alerts, new_site_state))

    # --- 3) Envío; el state de un sitio solo se persiste si su aviso salió ---
    n_alertas = 0
    for name, priority, alerts, new_site_state in pending:
        if not alerts:
            state[name] = new_site_state
            continue
        n_new = sum(1 for a in alerts if a["alert_type"] == "new")
        n_re = sum(1 for a in alerts if a["alert_type"] == "restock")
        log.info(f"Alertas {name} [{priority}]: {n_new} nuevos + {n_re} restock")
        msgs = format_notification(name, priority, alerts, config)
        if send_telegram_chunks(bot_token, chat_id, msgs):
            state[name] = new_site_state
            n_alertas += len(alerts)
        else:
            log.error(f"{name}: aviso NO enviado -> no se marca como visto, "
                      f"se reintenta en la próxima pasada")

    # --- 4) Avisos de SALUD (caídas y tiendas ciegas): solo en las transiciones ---
    for msg, deshacer in _collect_health_alerts(state, config):
        if not send_telegram(bot_token, chat_id, msg):
            deshacer()

    save_state(state)
    if not n_alertas:
        log.info("Sin alertas en esta revisión")


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
