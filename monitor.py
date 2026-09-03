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
HEALTH_META_KEY = "__health_meta__"  # clave reservada: control del resumen de salud
DEFAULT_HEALTH_FAIL_THRESHOLD = 10  # fallos seguidos antes de avisar (~10 min a 1 pasada/min)
DEFAULT_EMPTY_THRESHOLD = 5        # pasadas a 0 productos (habiendo tenido catálogo) antes de avisar
DEFAULT_DIGEST_COOLDOWN_MIN = 30   # minutos mínimos entre dos resúmenes de salud
DEFAULT_AVALANCHE_STORES = 8       # tiendas con alertas a partir de las cuales se agrupa todo
DEFAULT_MAX_ALERTS_AVALANCHE = 40  # productos como mucho en el mensaje de avalancha
DEFAULT_MAX_WORKERS = 12           # peticiones simultáneas
DEFAULT_TIMEOUT = 20               # segundos por petición
# Una tienda caída no debe encarecer TODAS las pasadas. Con 2 intentos y 20s de
# timeout, una sola tienda que agota el timeout mete 42s en cada pasada (medido:
# Friki Galaxy dejaba las pasadas de Naruto en 44s frente a los 8s del resto).
DEFAULT_DEGRADED_AFTER = 5         # fallos seguidos -> timeout corto y 1 solo intento
DEFAULT_DEGRADED_TIMEOUT = 8       # segundos para una tienda ya degradada
DEFAULT_BACKOFF_AFTER = 20         # fallos seguidos -> además se comprueba 1 de cada N pasadas
DEFAULT_BACKOFF_EVERY = 10         # pasadas que se salta una tienda en backoff
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
    h["skips"] = 0  # se acaba de comprobar: el contador de saltos del backoff se reinicia
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


def _fmt_store_list(names, limit=10):
    """Lista compacta separada por · y recortada, para no llenar la pantalla."""
    shown = [html_mod.escape(n) for n in names[:limit]]
    extra = len(names) - len(shown)
    txt = " · ".join(shown)
    return txt + (f" <i>y {extra} más</i>" if extra else "")


def _collect_health_alerts(state, config):
    """Devuelve (mensajes, deshacer): como mucho UN resumen por pasada.

    Antes salía un mensaje por tienda y por transición. Con 118 tiendas, un bache
    de red del runner producía decenas de mensajes de caída y otras tantas de
    recuperación, y entre ese ruido se perdía un restock de verdad. Ahora todas
    las transiciones de la pasada se agrupan en un único mensaje, que además va
    en silencio (sin notificación en el móvil) y con un tiempo mínimo entre
    resúmenes. `deshacer` restaura los flags si el envío falla, para que el aviso
    se reintente en vez de perderse.
    """
    threshold = config.get("health_fail_threshold", DEFAULT_HEALTH_FAIL_THRESHOLD)
    empty_threshold = config.get("health_empty_threshold", DEFAULT_EMPTY_THRESHOLD)
    cooldown = config.get("health_digest_cooldown_minutes", DEFAULT_DIGEST_COOLDOWN_MIN) * 60
    health = state.get(HEALTH_KEY, {})
    meta = state.setdefault(HEALTH_META_KEY, {})

    caidas, recuperadas, ciegas, vuelven = [], [], [], []
    restores = []

    def flip(h, key, value):
        prev = h.get(key)
        restores.append(lambda: h.__setitem__(key, prev))
        h[key] = value

    for name, h in sorted(health.items()):
        fails = h.get("fails", 0)
        if fails >= threshold and not h.get("alerted", False):
            flip(h, "alerted", True)
            caidas.append(name)
        elif fails == 0 and h.get("alerted", False):
            flip(h, "alerted", False)
            recuperadas.append(name)

        empty = h.get("empty_streak", 0)
        if empty >= empty_threshold and not h.get("empty_alerted", False):
            flip(h, "empty_alerted", True)
            ciegas.append((name, h.get("max_products", 0), empty))
        elif empty == 0 and h.get("empty_alerted", False):
            flip(h, "empty_alerted", False)
            vuelven.append(name)

    def deshacer():
        for r in restores:
            r()

    if not (caidas or recuperadas or ciegas or vuelven):
        return [], deshacer

    # Una tienda CIEGA es pérdida de datos silenciosa y es rara: se salta la espera.
    # Las caídas y recuperaciones son ruido de mantenimiento y sí la respetan.
    ahora = time.time()
    if not ciegas and ahora - meta.get("last_digest", 0) < cooldown:
        deshacer()
        return [], (lambda: None)

    n_total = len(health)
    lineas = []
    if ciegas:
        for name, best, streak in ciegas:
            lineas.append(f"👻 <b>{name}</b>: responde OK pero lleva {streak} pasadas a "
                          f"<b>0 productos</b> (tenía {best}). Revisa la URL en config.json.")
    if caidas:
        cabecera = f"⚠️ <b>{len(caidas)} tiendas no responden</b>"
        if n_total and len(caidas) >= max(5, n_total // 3):
            cabecera += " — son muchas a la vez, probablemente sea la red del runner"
        lineas.append(f"{cabecera}\n{_fmt_store_list(caidas)}")
    if recuperadas:
        lineas.append(f"✅ <b>Recuperadas ({len(recuperadas)})</b>: {_fmt_store_list(recuperadas)}")
    if vuelven:
        lineas.append(f"✅ <b>Vuelven a dar productos</b>: {_fmt_store_list(vuelven)}")

    flip(meta, "last_digest", ahora)
    return ["\n\n".join(lineas)], deshacer


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
        # uid ESTABLE: el enlace, que sobrevive a que la tienda retoque el título.
        uid = hashlib.md5((link or f"{title}").encode()).hexdigest()
        legacy = hashlib.md5(f"{title}{link}".encode()).hexdigest()
        products.append({"uid": uid, "legacy_uid": legacy, "title": title,
                         "link": link, "price": price, "in_stock": in_stock})

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
            # uid ESTABLE: solo el id del producto. Antes incluía el título, así que
            # cualquier retoque del título ("PREVENTA X" -> "X") cambiaba el uid y el
            # producto volvía a parecer nuevo -> el mismo enlace se avisaba otra vez
            # horas después. `legacy_uid` es el esquema viejo y solo sirve para leer
            # el state antiguo sin disparar una tanda de falsos "nuevos".
            pid = item.get("id", "")
            uid = hashlib.md5(f"shopify:{pid}".encode()).hexdigest() if pid else \
                hashlib.md5(f"{pid}{title}".encode()).hexdigest()
            legacy = hashlib.md5(f"{pid}{title}".encode()).hexdigest()
            products.append({"uid": uid, "legacy_uid": legacy, "title": title,
                             "link": link, "price": price, "in_stock": in_stock})
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
        pid = item.get("id", "")
        uid = hashlib.md5(f"woo:{pid}".encode()).hexdigest() if pid else \
            hashlib.md5(f"{pid}{title}".encode()).hexdigest()
        legacy = hashlib.md5(f"{pid}{title}".encode()).hexdigest()
        products.append({"uid": uid, "legacy_uid": legacy, "title": title,
                         "link": link, "price": price, "in_stock": in_stock})
    return products


def send_telegram(bot_token, chat_id, message, attempts=4, silent=False):
    """Envía un mensaje y devuelve True/False según haya salido.

    Devolver el resultado es lo que permite NO dar por avisado un producto cuyo
    mensaje no llegó: quien llama solo persiste el state si esto devuelve True.
    Reintenta respetando el `retry_after` de los 429 (rate limit), que es el
    fallo más probable cuando un drop grande genera avisos de muchas tiendas.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False}
    if silent:
        # Los avisos de salud llegan al chat pero NO hacen sonar el móvil: así el
        # ruido de mantenimiento no compite con un restock de verdad.
        payload["disable_notification"] = True
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


def send_telegram_chunks(bot_token, chat_id, messages, silent=False):
    """Envía una lista de trozos. True solo si TODOS salen."""
    ok = True
    for msg in messages:
        if not send_telegram(bot_token, chat_id, msg, silent=silent):
            ok = False
    return ok


def is_loud(alerts):
    """¿Merece este aviso hacer sonar el móvil? Solo si lleva algo marcado 🔥/🚨
    (UPC, booster box, case, ETB...). Una lata o un blíster llegan al chat en
    silencio: así lo gordo no se pierde entre lo flojo."""
    return any(a.get("top_priority") or a.get("high_value") for a in alerts)


def _chunk_message(title_line, blocks):
    """Trocea por bloques enteros para no pasar del límite de Telegram (un mensaje
    de más de 4096 caracteres se rechaza con un 400 y el aviso se perdía entero).
    Nunca parte un producto por la mitad."""
    msgs, cur = [], title_line
    for b in blocks:
        if len(cur) + len(b) + 1 > TELEGRAM_MAX_CHARS and cur != title_line:
            msgs.append(cur)
            cur = f"{title_line}<i>(continuación {len(msgs) + 1})</i>\n" + b + "\n"
        else:
            cur += b + "\n"
    msgs.append(cur)
    return msgs


def product_rank(p):
    """Orden de interés: 0 = top (🔥), 1 = high_value (🚨), 2 = promo (🎁), 3 = resto.

    El aviso se corta a `max_alerts_per_site` productos, así que sin ordenar lo
    gordo podía quedar fuera del corte por detrás de un llavero. Qué cae en cada
    nivel lo decide el config de cada bot (top_priority_keywords, etc.); un bot
    que no defina un nivel simplemente no lo usa.
    """
    if p.get("top_priority"):
        return 0
    if p.get("high_value"):
        return 1
    if p.get("promo"):
        return 2
    return 3


def rank_mark(p):
    if p.get("top_priority"):
        return "🔥"
    if p.get("high_value"):
        return "🚨"
    if p.get("promo"):
        return "🎁"
    return ""


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


def plan_fetch(name, health, config):
    """Decide cómo tratar a una tienda según sus fallos seguidos.

    Devuelve (comprobar, timeout, intentos). Una tienda sana va con el timeout
    normal y 2 intentos; una que lleva fallando baja a timeout corto y 1 intento
    (deja de lastrar la pasada entera); y una caída de forma persistente pasa a
    comprobarse 1 de cada N pasadas, para que siga pudiendo auto-recuperarse sin
    costar una petición por pasada. En cuanto responde, `fails` vuelve a 0 y con
    ello el trato normal.
    """
    h = health.get(name, {})
    fails = h.get("fails", 0)
    timeout = config.get("request_timeout_seconds", DEFAULT_TIMEOUT)
    if fails < config.get("degraded_fail_threshold", DEFAULT_DEGRADED_AFTER):
        return True, timeout, 2
    degradado = (True, config.get("degraded_timeout_seconds", DEFAULT_DEGRADED_TIMEOUT), 1)
    if fails < config.get("backoff_fail_threshold", DEFAULT_BACKOFF_AFTER):
        return degradado
    if h.get("skips", 0) >= config.get("backoff_every_passes", DEFAULT_BACKOFF_EVERY):
        return degradado
    return (False, 0, 0)


def fetch_site(site_cfg, config, timeout=None, attempts=2):
    """SOLO red y parseo. No toca el state, así puede correr en paralelo.

    Devuelve (site_cfg, productos|None, error). Sacar la parte de red fuera del
    state es lo que permite lanzar las ~120 tiendas a la vez: una pasada pasa de
    ~60s (secuencial, y varios minutos si hay tiendas caídas reintentando) a ~6s,
    que es lo que de verdad fija la cadencia real del bucle continuo.
    """
    name = site_cfg["name"]
    url = site_cfg["url"]
    is_api = site_cfg.get("type", "html") == "api"
    if timeout is None:
        timeout = config.get("request_timeout_seconds", DEFAULT_TIMEOUT)

    headers = build_headers(config["user_agent"], is_api=is_api)
    if is_api:
        from urllib.parse import urlparse
        p = urlparse(url)
        headers["Referer"] = f"{p.scheme}://{p.netloc}/"

    last_err = None
    for attempt in range(attempts):
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
            if attempt + 1 < attempts:
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
    promo_keywords = config.get("promo_keywords", [])
    match_label = config.get("match_label", "el filtro")

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
        log.info(f"  {name}: {len(products)} detectados, {len(filtered)} matchean {match_label}"
                 + (f" ({n_excl} descartados por exclusión)" if n_excl else ""))
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

    # Baseline de la PRIMERA pasada de una tienda. Con `silent_first_run` se absorbe
    # todo lo existente sin avisar (lo que quieren los bots cuyo juego ya tiene
    # catálogo o aún no existe); sin él, se notifica lo que esté en stock.
    if is_first_run and config.get("silent_first_run", False):
        for p in products:
            site_state[p["uid"]] = {"in_stock": p["in_stock"]}
        log.info(f"  {name}: baseline inicial silenciado ({len(products)} productos)")
        return [], site_state

    alerts = []
    for p in products:
        uid = p["uid"]
        prev = site_state.get(uid)
        if prev is None and p.get("legacy_uid"):
            # Migración silenciosa del esquema viejo de uid: si el producto ya estaba
            # en el state con la clave antigua, se hereda su estado y se reescribe con
            # la nueva. Sin esto, el cambio de esquema haría parecer NUEVO todo el
            # catálogo y dispararía una tanda enorme de avisos falsos.
            prev = site_state.pop(p["legacy_uid"], None)
        p["top_priority"] = matches_keywords(p["title"], top_priority_keywords)
        p["high_value"] = matches_keywords(p["title"], high_value_keywords)
        p["promo"] = matches_keywords(p["title"], promo_keywords)
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
    """Devuelve una LISTA de mensajes (troceados) con los productos ordenados por
    interés: lo gordo primero, para que no se caiga del corte."""
    config = config or {}
    max_alerts = config.get("max_alerts_per_site", DEFAULT_MAX_ALERTS)
    bot_emoji = config.get("bot_emoji", "🔔")
    bot_label = config.get("bot_label", "MONITOR")
    emoji = PRIORITY_EMOJI.get(priority, "🔔")
    has_restock = any(a["alert_type"] == "restock" for a in alerts)
    header = "🔄 RESTOCK + " if has_restock else ""
    ordered = sorted(alerts, key=product_rank)
    shown, extra = ordered[:max_alerts], len(ordered) - max_alerts

    title_line = (f"{bot_emoji} {header}<b>{bot_label} — {html_mod.escape(site_name)}</b> "
                  f"{emoji} [{priority.upper()}]\n")
    blocks = []
    for p in shown:
        tag = "🔄 VUELVE" if p["alert_type"] == "restock" else "🆕 NUEVO"
        mark = rank_mark(p)
        mark = f"{mark} " if mark else ""
        stock_mark = "" if p["in_stock"] else " ⚠️ AGOTADO"
        # Escapado obligatorio: un '<' o un '&' suelto en el título rompe el
        # parse_mode HTML, Telegram devuelve 400 y el aviso se pierde entero.
        b = [f"• {mark}{tag}{stock_mark} <b>{html_mod.escape(p['title'])}</b>",
             f"  💰 {html_mod.escape(p['price'])}"]
        if p["link"]:
            b.append(f"  🔗 {p['link']}")
        b.append("")
        blocks.append("\n".join(b))
    if extra > 0:
        blocks.append(f"... y {extra} más")
    return _chunk_message(title_line, blocks)


def format_avalanche(entradas, config):
    """UN solo mensaje cuando muchas tiendas avisan en la misma pasada.

    El día que abra un drop del 30 aniversario van a disparar decenas de tiendas
    casi a la vez: con un mensaje por tienda, la booster box se pierde entre
    cuarenta avisos de latas. Aquí va una línea por producto, ordenadas por
    importancia y con la tienda al lado, así lo gordo queda arriba del todo.
    """
    max_items = config.get("max_alerts_avalanche", DEFAULT_MAX_ALERTS_AVALANCHE)
    items = [(a, name) for name, _, alerts, _ in entradas for a in alerts]
    # Una misma tienda con varias colecciones vigiladas (p. ej. Pokemillon en
    # Eternals + Reservas + Novedades) repite el mismo producto. Se colapsa por
    # enlace idéntico: nunca junta tiendas distintas, porque el enlace lleva el
    # dominio. Solo afecta a lo que se muestra; el state de cada tienda se guarda
    # igual, así que ninguna se queda sin registrar el producto.
    vistos, unicos = set(), []
    for a, name in items:
        clave = a["link"] or f"{name}|{a['title']}"
        if clave in vistos:
            continue
        vistos.add(clave)
        unicos.append((a, name))
    items = unicos
    # Primero lo más gordo; a igual rango, los restock antes que los listados nuevos.
    items.sort(key=lambda t: (product_rank(t[0]), 0 if t[0]["alert_type"] == "restock" else 1))
    total = len(items)
    shown = items[:max_items]

    bot_emoji = config.get("bot_emoji", "🔔")
    bot_label = config.get("bot_label", "MONITOR")
    title_line = (f"{bot_emoji} <b>{bot_label} — {len(entradas)} tiendas con novedades</b> "
                  f"({total} productos)\n")
    blocks = []
    for a, tienda in shown:
        mark = rank_mark(a) or "•"
        tag = "🔄" if a["alert_type"] == "restock" else "🆕"
        stock_mark = "" if a["in_stock"] else " ⚠️ AGOTADO"
        b = [f"{mark} {tag} <b>{html_mod.escape(a['title'])}</b>{stock_mark}",
             f"  💰 {html_mod.escape(a['price'])} — <i>{html_mod.escape(tienda)}</i>"]
        if a["link"]:
            b.append(f"  🔗 {a['link']}")
        b.append("")
        blocks.append("\n".join(b))
    if total > len(shown):
        blocks.append(f"... y {total - len(shown)} productos más")
    return _chunk_message(title_line, blocks)


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
    # A las tiendas que llevan fallando se les acorta el timeout, y a las caídas de
    # forma persistente se las salta la mayoría de pasadas: así una sola tienda
    # muerta deja de marcar el ritmo de todas las pasadas.
    health = state.get(HEALTH_KEY, {})
    plan = {s["name"]: plan_fetch(s["name"], health, config) for s in sites_sorted}
    a_consultar = [s for s in sites_sorted if plan[s["name"]][0]]
    saltadas = [s["name"] for s in sites_sorted if not plan[s["name"]][0]]
    for name in saltadas:
        h = health.setdefault(name, {})
        h["skips"] = h.get("skips", 0) + 1
    degradadas = [s["name"] for s in a_consultar if plan[s["name"]][2] == 1]

    workers = max(1, min(config.get("max_workers", DEFAULT_MAX_WORKERS), len(a_consultar) or 1))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda s: fetch_site(s, config, timeout=plan[s["name"]][1], attempts=plan[s["name"]][2]),
            a_consultar))
    extra = ""
    if degradadas:
        extra += f", {len(degradadas)} con timeout corto por fallos"
    if saltadas:
        extra += f", {len(saltadas)} saltadas (caídas persistentes)"
    log.info(f"{len(a_consultar)} tiendas consultadas en {time.time() - t0:.1f}s "
             f"({workers} hilos){extra}")

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
    # El uid de Shopify es md5(id_de_producto + título): el MISMO artículo listado en
    # varias colecciones de la MISMA tienda (Pokemillon está en Eternals + Reservas +
    # Novedades + Cajas de Sobres) comparte uid. Sin esto llegaban hasta 4 Telegram
    # seguidos con el mismo enlace. Como `pending` va en orden de prioridad, avisa la
    # entrada más prioritaria y las demás lo dan por visto sin repetirlo.
    seen_uids = set()
    n_alertas = 0
    con_alertas = []
    for name, priority, alerts, new_site_state in pending:
        alerts = [a for a in alerts if a["uid"] not in seen_uids]
        seen_uids.update(a["uid"] for a in alerts)
        if not alerts:
            state[name] = new_site_state
            continue
        n_new = sum(1 for a in alerts if a["alert_type"] == "new")
        n_re = sum(1 for a in alerts if a["alert_type"] == "restock")
        log.info(f"Alertas {name} [{priority}]: {n_new} nuevos + {n_re} restock")
        con_alertas.append((name, priority, alerts, new_site_state))

    solo_prioritarios = config.get("sound_only_for_priority", True)
    umbral_avalancha = config.get("avalanche_store_threshold", DEFAULT_AVALANCHE_STORES)

    if len(con_alertas) > umbral_avalancha:
        # Avalancha: un único mensaje en vez de uno por tienda.
        todas = [a for _, _, alerts, _ in con_alertas for a in alerts]
        log.info(f"AVALANCHA: {len(con_alertas)} tiendas, {len(todas)} productos "
                 f"-> un solo mensaje agrupado")
        msgs = format_avalanche(con_alertas, config)
        silent = solo_prioritarios and not is_loud(todas)
        if send_telegram_chunks(bot_token, chat_id, msgs, silent=silent):
            for name, _, alerts, new_site_state in con_alertas:
                state[name] = new_site_state
                n_alertas += len(alerts)
        else:
            log.error("Aviso de avalancha NO enviado -> nada se marca como visto, "
                      "se reintenta en la próxima pasada")
    else:
        for name, priority, alerts, new_site_state in con_alertas:
            msgs = format_notification(name, priority, alerts, config)
            silent = solo_prioritarios and not is_loud(alerts)
            if send_telegram_chunks(bot_token, chat_id, msgs, silent=silent):
                state[name] = new_site_state
                n_alertas += len(alerts)
            else:
                log.error(f"{name}: aviso NO enviado -> no se marca como visto, "
                          f"se reintenta en la próxima pasada")

    # --- 4) Salud (caídas y tiendas ciegas): UN resumen, y en silencio ---
    health_msgs, deshacer_salud = _collect_health_alerts(state, config)
    for msg in health_msgs:
        if not send_telegram(bot_token, chat_id, msg, silent=True):
            deshacer_salud()

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
