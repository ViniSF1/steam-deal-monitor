from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
DB_PATH = ROOT / "data" / "monitor.sqlite3"
API_BASE = "https://api.isthereanydeal.com"
LOG = logging.getLogger("steam-monitor")

# ITAD tag labels can differ from Steam's displayed genre labels.
# Keep this mapping centralized so it is easy to tune after the first API run.
GENRE_TAGS = {
    "RPG": ["RPG"],
    "Open World": ["Open World"],
    "Survival": ["Survival"],
    "Adventure": ["Adventure"],
    "Strategy": ["Strategy"],
    "Souls-like": ["Souls-like", "Soulslike"],
    "Racing": ["Racing"],
}


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def api_session(api_key: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "key": api_key,
        "User-Agent": "PersonalSteamDealMonitor/1.0 (personal use)",
        "Accept": "application/json",
    })
    return session


def request_json(session: requests.Session, method: str, path: str, **kwargs: Any) -> Any:
    response = session.request(method, f"{API_BASE}{path}", timeout=30, **kwargs)
    if response.status_code == 429:
        raise RuntimeError("IsThereAnyDeal limitou temporariamente as consultas (HTTP 429).")
    response.raise_for_status()
    return response.json()


def build_filter(config: dict[str, Any]) -> dict[str, Any]:
    tags = []
    for genre in config["genres"]:
        tags.extend(GENRE_TAGS.get(genre, [genre]))
    # Steam rating is supplied as a percentage by ITAD's deal filters.
    return {
        "cut": {"min": int(config["min_discount"]), "max": None},
        "steamPerc": {"min": int(config["min_steam_rating"]), "max": 100},
        "tagsUnion": sorted(set(tags)),
    }


def fetch_deals(session: requests.Session, config: dict[str, Any]) -> list[dict[str, Any]]:
    all_deals: list[dict[str, Any]] = []
    for page in range(int(config.get("max_pages", 5))):
        params = {
            "key": os.environ["ITAD_API_KEY"],
            "country": config["country"],
            "offset": page * int(config["deals_per_page"]),
            "limit": int(config["deals_per_page"]),
            "sort": "-cut",
            "shops": str(config["steam_shop_id"]),
            "filter": json.dumps(build_filter(config), separators=(",", ":")),
        }
        payload = request_json(session, "GET", "/deals/v2", params=params)
        if not isinstance(payload, list) or not payload:
            break
        all_deals.extend(payload)
        if len(payload) < int(config["deals_per_page"]):
            break
    return all_deals


def amount(obj: Any) -> float | None:
    if not isinstance(obj, dict):
        return None
    try:
        return float(obj.get("amount"))
    except (TypeError, ValueError):
        return None


def currency(obj: Any) -> str | None:
    return obj.get("currency") if isinstance(obj, dict) else None


def normalize_deal(raw: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    game = raw.get("game") or {}
    price_obj = raw.get("price") or {}
    regular_obj = raw.get("regular") or {}
    price = amount(price_obj)
    regular = amount(regular_obj)
    cut = int(raw.get("cut") or 0)
    if price is None or currency(price_obj) != config["currency"]:
        return None
    if price > float(config["max_price"]) or cut < int(config["min_discount"]):
        return None

    # Deal payload's steamPerc is not guaranteed to be present in every API response.
    # The API filter is authoritative; this local check is only applied when present.
    rating = raw.get("steamPerc")
    if rating is None:
        rating = (raw.get("reviews") or {}).get("steam", {}).get("score")
    if rating is not None:
        try:
            if float(rating) < float(config["min_steam_rating"]):
                return None
        except (TypeError, ValueError):
            return None

    low_obj = raw.get("storeLow") or raw.get("historyLow")
    low = amount(low_obj)
    low_currency = currency(low_obj)
    near_pct = float(config.get("near_history_percent", 10))
    history_mode = config.get("history_mode", "near")
    if history_mode == "record":
        if low is None or low_currency != config["currency"] or price > low * 1.001:
            return None
    elif history_mode == "near":
        if low is None or low_currency != config["currency"] or price > low * (1 + near_pct / 100):
            return None

    title = game.get("title") or raw.get("title") or "Jogo sem título"
    game_id = game.get("id") or raw.get("id")
    deal_url = raw.get("url") or (game.get("urls") or {}).get("game") or "https://isthereanydeal.com/"
    # Prefer the Steam store URL when ITAD provides a Steam app ID; otherwise retain ITAD's deal link.
    app_id = game.get("steamAppID") or game.get("steamAppId") or game.get("appID")
    if app_id:
        deal_url = f"https://store.steampowered.com/app/{app_id}/"

    return {
        "id": str(game_id or title),
        "title": title,
        "price": price,
        "regular": regular,
        "discount": cut,
        "currency": config["currency"],
        "history_low": low,
        "history_currency": low_currency,
        "shop": (raw.get("shop") or {}).get("name", "Steam"),
        "url": deal_url,
        "steam_app_id": app_id,
    }


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_deals (
                game_id TEXT PRIMARY KEY,
                last_price REAL NOT NULL,
                last_discount INTEGER NOT NULL,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_observations (
                game_id TEXT NOT NULL,
                price REAL NOT NULL,
                discount INTEGER NOT NULL,
                observed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


def is_new_or_improved(deal: dict[str, Any]) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT last_price, last_discount FROM sent_deals WHERE game_id = ?",
            (deal["id"],)
        ).fetchone()
        conn.execute(
            "INSERT INTO price_observations(game_id, price, discount) VALUES (?, ?, ?)",
            (deal["id"], deal["price"], deal["discount"])
        )
        if row is None:
            conn.execute(
                "INSERT INTO sent_deals(game_id, last_price, last_discount) VALUES (?, ?, ?)",
                (deal["id"], deal["price"], deal["discount"])
            )
            return True

        old_price, old_discount = float(row[0]), int(row[1])
        improved = deal["price"] < old_price - 0.01 or deal["discount"] > old_discount
        if improved:
            conn.execute(
                "UPDATE sent_deals SET last_price = ?, last_discount = ?, last_seen = CURRENT_TIMESTAMP WHERE game_id = ?",
                (deal["price"], deal["discount"], deal["id"])
            )
        else:
            conn.execute(
                "UPDATE sent_deals SET last_seen = CURRENT_TIMESTAMP WHERE game_id = ?",
                (deal["id"],)
            )
        return improved


def send_discord(webhook_url: str, deals: list[dict[str, Any]]) -> None:
    if not deals:
        return
    if not webhook_url.startswith("https://discord.com/api/webhooks/"):
        raise ValueError("DISCORD_WEBHOOK_URL não parece ser um webhook válido do Discord.")

    embeds = []
    for deal in deals[:10]:
        low_text = (
            f"R$ {deal['history_low']:.2f}" if deal["history_low"] is not None
            and deal["history_currency"] == "BRL" else "Não informado"
        )
        embeds.append({
            "title": deal["title"][:256],
            "url": deal["url"],
            "description": (
                f"**R$ {deal['price']:.2f}**  ·  **-{deal['discount']}%**\n"
                f"Preço original: R$ {deal['regular']:.2f}\n"
                f"Menor preço de referência: {low_text}\n"
                f"Loja: {deal['shop']}"
            ),
            "color": 5763719,
        })
    payload = {
        "username": "Radar Steam",
        "content": f"🎮 **{len(deals)} nova(s) oportunidade(s) encontrada(s)!**",
        "embeds": embeds,
        "allowed_mentions": {"parse": []},
    }
    response = requests.post(webhook_url, json=payload, timeout=20)
    response.raise_for_status()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    api_key = os.getenv("ITAD_API_KEY")
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if not api_key or not webhook:
        LOG.error("Configure ITAD_API_KEY e DISCORD_WEBHOOK_URL nas variáveis de ambiente.")
        return 2

    config = load_config()
    init_db()
    session = api_session(api_key)
    raw_deals = fetch_deals(session, config)
    LOG.info("Ofertas recebidas da API: %s", len(raw_deals))

    matches = []
    for raw in raw_deals:
        deal = normalize_deal(raw, config)
        if deal is None:
            continue
        if config.get("notify_only_new_deals", True):
            if is_new_or_improved(deal):
                matches.append(deal)
        else:
            matches.append(deal)

    LOG.info("Ofertas que passaram pelos filtros: %s", len(matches))
    if matches:
        send_discord(webhook, matches)
        LOG.info("Notificações enviadas ao Discord.")
    else:
        LOG.info("Nenhuma oferta nova ou melhorada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
