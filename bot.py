#!/usr/bin/env python3
"""
Cuba Exchange Rate Bot — @nautaii
==================================
Credits: Iroennys | Telegram: @nautaii

Monitors the informal Cuban exchange rate (USD/EUR/MLC → CUP)
from El Toque's Telegram channel and sends daily updates
+ change notifications to subscribers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, time as dtime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import httpx
import requests
from bs4 import BeautifulSoup
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, InlineQueryHandler, MessageHandler, filters

from database import init_db, ensure_user, get_user, set_github_token, set_github_user, \
    set_mode, subscribe as db_sub, unsubscribe as db_unsub, get_subscribers, is_subscribed
from github_client import GitHubClient, GitHubError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("BOT_TOKEN", "")
if not TOKEN:
    raise SystemExit("BOT_TOKEN env var is required")

CHANNEL_URL = "https://t.me/s/eltoquecom"
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
RATES_FILE = DATA_DIR / "rates.json"
CHECK_INTERVAL_MIN = 30  # how often to check for rate changes
DAILY_HOUR = 9           # daily summary hour (UTC)

# Conversation state for multi-step workflows (in-memory, OK if bot restarts)
conv: dict[int, dict] = {}

# Optional — Zen AI (opencode.ai/zen) + autoping
ZEN_API_KEY = os.environ.get("ZEN_API_KEY", "")
ZEN_API_URL = os.environ.get("ZEN_API_URL", "https://opencode.ai/zen/v1/chat/completions")
RENDER_URL = os.environ.get("RENDER_URL", "")  # for autoping (set on deploy)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("cuba_bot")

# ---------------------------------------------------------------------------
# Data helpers — JSON file storage, no DB
# ---------------------------------------------------------------------------
# ponytail: JSON file storage, fine for <1K subscribers. SQLite if scale matters.

def _load_json(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}

def _save_json(path: Path, data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def load_rates() -> dict[str, Any]:
    return _load_json(RATES_FILE)

def save_rates(rates: dict[str, Any]) -> None:
    _save_json(RATES_FILE, rates)

# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------
def parse_rate_message(text: str) -> dict[str, Any] | None:
    """Parse the rate update message from El Toque Telegram channel."""
    m_date = re.search(r"Fecha:\s*(\d{2}/\d{2}/\d{4})", text)
    if not m_date:
        return None

    rates: dict[str, Any] = {"date_raw": m_date.group(1)}

    for coin in ("USD", "EUR", "MLC"):
        m = re.search(rf"{coin}:\s*([\d,.]+)\s*CUP", text)
        if m:
            rates[coin.lower()] = float(m.group(1).replace(",", ""))

    for coin in ("USD", "EUR", "MLC"):
        pattern = rf"{coin}:\s*de\s*([\d,.]+)\s*a\s*([\d,.]+)\s*CUP"
        m = re.search(pattern, text)
        if m:
            rates[f"{coin.lower()}_min"] = float(m.group(1).replace(",", ""))
            rates[f"{coin.lower()}_max"] = float(m.group(2).replace(",", ""))

    return rates if any(k in rates for k in ("usd", "eur", "mlc")) else None


# ---------------------------------------------------------------------------
# Zen AI — OpenCode API (OpenAI-compatible)
# ---------------------------------------------------------------------------
# ponytail: single-shot non-streaming. Streaming + context memory if users ask.
async def call_zen_ai(prompt: str, model: str = "big-pickle") -> str:
    """Send a prompt to the OpenCode Zen API and return the response text."""
    if not ZEN_API_KEY:
        return "❌ ZEN_API_KEY no configurada."
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                ZEN_API_URL,
                headers={"Authorization": f"Bearer {ZEN_API_KEY}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1024},
            )
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            return msg["content"] or msg.get("reasoning_content", "") or "Sin respuesta"
    except httpx.HTTPStatusError as e:
        return f"❌ Error HTTP {e.response.status_code}" if e.response else "❌ Error de conexión"
    except (KeyError, IndexError, TypeError):
        return "❌ Respuesta inesperada de la API"
    except Exception as e:
        return f"❌ {e}"


async def fetch_latest_rates() -> dict[str, Any] | None:
    """Scrape the latest rates from the Telegram channel."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(CHANNEL_URL)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("HTTP error fetching channel: %s", e)
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    for msg in soup.find_all("div", class_="tgme_widget_message_text"):
        text = msg.get_text()
        if "Actualización" in text and "CUP" in text:
            rates = parse_rate_message(text)
            if rates:
                return rates
    return None


def rates_changed(old: dict | None, new: dict) -> bool:
    """Compare two rate dicts — True if any rate differs."""
    if old is None:
        return True
    for coin in ("usd", "eur", "mlc"):
        if old.get(coin) != new.get(coin):
            return True
    return False


def _fmt_date(raw: str) -> str:
    """Turn DD/MM/YYYY into 'DD de mes de YYYY'."""
    meses = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio",
             "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    try:
        d, m, y = raw.split("/")
        return f"{int(d)} de {meses[int(m)]} de {y}"
    except (ValueError, IndexError):
        return raw


# ponytail: box-drawing table with fixed col widths. If Telegram changes monospace
# rendering, fall back to pipe-separated plain text.
def _row_inner(label: str, sym: str, val_str: str, range_str: str) -> str:
    """Build inner content of a data row (without ││ frame)."""
    return f" {label} {sym}{val_str} CUP {range_str}"


def format_rates(rates: dict, *, header: str = "") -> str:
    """Format rates into a clean monospace table."""
    W = 32  # total width including frame
    IW = W - 2  # inner width between ││
    sep = "─" * IW

    date_str = _fmt_date(rates.get("date_raw", "hoy"))
    t = header or "TASAS DEL DÍA"

    rows = [
        f"┌{sep}┐",
        f"│{t:^{IW}}│",
        f"│{date_str:^{IW}}│",
        f"├{sep}┤",
    ]
    SYM = {"usd": "$", "eur": "€", "mlc": ""}
    for coin, label in (("usd", "USD"), ("eur", "EUR"), ("mlc", "MLC")):
        val = rates.get(coin)
        lo = rates.get(f"{coin}_min")
        hi = rates.get(f"{coin}_max")
        sym = SYM[coin]
        if val is not None:
            r = f"[{lo:.0f}–{hi:.0f}]" if lo and hi else ""
            inner = _row_inner(label, sym, f"{val:>7.2f}", r)
            rows.append(f"│{inner:<{IW}}│")
        else:
            inner = f" {label} {sym} —"
            rows.append(f"│{inner:<{IW}}│")
    rows.append(f"└{sep}┘")
    rows.append(f"@eltoquecom · {datetime.now().strftime('%H:%M UTC')}")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Bot commands
# ---------------------------------------------------------------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    ensure_user(chat_id)
    if not is_subscribed(chat_id):
        db_sub(chat_id)
        log.info("New subscriber: %s", chat_id)

    rates = load_rates()
    date_str = rates.get('date_raw', '—')
    msg = (
        "👋 *Bienvenido al Bot de Tasas de Cuba*\n\n"
        "Consultá el precio del dólar, euro y MLC en el mercado "
        "informal cubano, actualizado desde @eltoquecom.\n\n"
        "📌 *Comandos:*\n"
        "  💱 /tasas — Cotizaciones USD, EUR, MLC\n"
        "  🪙 /moneda USD — Cotización específica\n"
        "  🔄 /convertir 100 USD — Conversión CUP ↔ moneda\n"
        "  🧘 /asesor — Análisis IA de tasas\n"
        "  🐙 /explore django — Buscar repos en GitHub\n"
        "  📥 /clone user/repo — Descargar ZIP de GitHub\n"
        "  🐙 /github — Menú GitHub completo\n"
        "  🔔 /sub — Notificaciones diarias\n"
        "  🔕 /unsub — Desactivar notificaciones\n"
        "  ⚙️ /mode — Cambiar modo Normal/GitHub\n"
        "  ❓ /ayuda — Ayuda completa\n\n"
        f"_{date_str}_\n"
        "Creado por @nautaii"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def tasas(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current rates."""
    rates = await _ensure_fresh_rates(update)
    if rates:
        await update.message.reply_text(
            f"```\n{format_rates(rates)}\n```",
            parse_mode=ParseMode.MARKDOWN,
        )
    # _ensure_fresh_rates already replied on error


async def moneda(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show rate for a specific currency."""
    coin = (ctx.args[0] if ctx.args else "").upper()
    if coin not in ("USD", "EUR", "MLC"):
        await update.message.reply_text(
            "Usá: `/moneda USD`, `/moneda EUR` o `/moneda MLC`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    rates = await _ensure_fresh_rates(update)
    if not rates:
        return

    key = coin.lower()
    val = rates.get(key)
    lo = rates.get(f"{key}_min")
    hi = rates.get(f"{key}_max")

    if val is None:
        await update.message.reply_text(f"⚠️ No hay datos para {coin} hoy.")
        return

    SYM = {"usd": "$", "eur": "€", "mlc": ""}
    sym = SYM[key]
    sep = "─" * 22
    lines = [
        f"┌{sep}┐",
        f"│ 💱 {coin}{' '+sym if sym else ''}               │",
        f"├{sep}┤",
        f"│ Tasa:  {sym}{val:>7.2f} CUP    │",
    ]
    if lo and hi:
        lines.append(f"│ Rango: {sym}{lo:.0f}–{sym}{hi:.0f} CUP   │")
    lines.append(f"└{sep}┘")
    lines.append(f"@eltoquecom · {_fmt_date(rates.get('date_raw', '—'))}")

    await update.message.reply_text(
        f"```\n{chr(10).join(lines)}\n```",
        parse_mode=ParseMode.MARKDOWN,
    )


async def convertir(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Convert CUP ↔ USD/EUR/MLC at current rate."""
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usá: `/convertir 100 USD` o `/convertir 5000 CUP USD`\n"
            "Convierte entre CUP y USD, EUR o MLC.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    rates = await _ensure_fresh_rates(update)
    if not rates:
        return

    try:
        amount = float(args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ El monto no es válido. Ej: `/convertir 100 USD`")
        return

    from_coin = args[1].upper()
    to_coin = args[2].upper() if len(args) > 2 else "CUP"

    SYM = {"usd": "$", "eur": "€", "mlc": "", "cup": "$"}

    if from_coin == "CUP" and to_coin in ("USD", "EUR", "MLC"):
        key = to_coin.lower()
        rate = rates.get(key)
        if not rate or rate == 0:
            await update.message.reply_text(f"❌ No hay tasa disponible para {to_coin}.")
            return
        result = amount / rate
        from_sym, to_sym = SYM["cup"], SYM[key]
        reply = (
            f"💱 *{from_coin} → {to_coin}*\n"
            f"```\n"
            f"{from_sym}{amount:>10.2f} CUP\n"
            f"  →  {to_sym}{result:>9.2f} {to_coin}\n"
            f"```\n"
            f"_Tasa: {SYM[key] if to_sym else ''}{rate:.2f} CUP por {to_coin}_"
        )
    elif to_coin == "CUP" and from_coin in ("USD", "EUR", "MLC"):
        key = from_coin.lower()
        rate = rates.get(key)
        if not rate:
            await update.message.reply_text(f"❌ No hay tasa disponible para {from_coin}.")
            return
        result = amount * rate
        from_sym, to_sym = SYM[key], SYM["cup"]
        reply = (
            f"💱 *{from_coin} → {to_coin}*\n"
            f"```\n"
            f"{from_sym if from_sym else from_coin}{amount:>9.2f} {from_coin}\n"
            f"  →  {to_sym}{result:>9.2f} CUP\n"
            f"```\n"
            f"_Tasa: {rate:.2f} CUP por {from_coin}_"
        )
    else:
        await update.message.reply_text(
            "❌ Solo soporto CUP ↔ USD/EUR/MLC.\n"
            "Ej: `/convertir 50 USD` o `/convertir 5000 CUP EUR`"
        )
        return

    await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)


async def subscribe(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db_sub(chat_id)
    await update.message.reply_text(
        "✅ Notificaciones activadas. Recibirás la tasa cada día "
        f"~{DAILY_HOUR:02d}:00 UTC y cuando haya cambios."
    )


async def unsubscribe(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db_unsub(chat_id)
    await update.message.reply_text("🔕 Notificaciones desactivadas.")


async def help_cmd(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, _ctx)


async def cmd_asesor(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """AI-powered rate analysis via Zen API."""
    rates = await _ensure_fresh_rates(update)
    if not rates:
        return
    q = update.message.text[len("/asesor"):].strip()
    prompt = (
        f"Eres un analista financiero experto en el mercado informal cubano (CUP).\n\n"
        f"Tasas actuales del día ({rates.get('date_raw', 'hoy')}):\n"
        f"- USD: ${rates.get('usd', 'N/A'):.2f} CUP (rango: ${rates.get('usd_min', 0):.0f}–${rates.get('usd_max', 0):.0f})\n"
        f"- EUR: €{rates.get('eur', 'N/A'):.2f} CUP (rango: €{rates.get('eur_min', 0):.0f}–€{rates.get('eur_max', 0):.0f})\n"
        f"- MLC: {rates.get('mlc', 'N/A'):.2f} CUP (rango: {rates.get('mlc_min', 0):.0f}–{rates.get('mlc_max', 0):.0f})\n\n"
    )
    if q:
        prompt += f"Pregunta del usuario: {q}\n\nResponde de forma clara y concisa."
    else:
        prompt += "Dame un análisis breve del mercado hoy: tendencias, qué moneda conviene más, y recomendaciones."

    msg = await update.message.reply_text("🧘 Analizando tasas con Zen IA…")
    resp = await call_zen_ai(prompt)
    await msg.edit_text(f"🧘 *Análisis de tasas*\n\n{resp}", parse_mode=ParseMode.MARKDOWN)


async def _ensure_fresh_rates(update: Update) -> dict | None:
    """Try to fetch live rates; fall back to cached. Reply on failure."""
    rates = await fetch_latest_rates()
    if rates:
        if rates_changed(load_rates(), rates):
            save_rates(rates)
        return rates

    rates = load_rates()
    if rates:
        await update.message.reply_text(
            "⚠️ No pude actualizar desde @eltoquecom. Mostrando última "
            "cotización disponible.",
        )
        return rates

    await update.message.reply_text(
        "❌ No hay datos disponibles. Intentalo más tarde.",
    )
    return None


# ---------------------------------------------------------------------------
# Mode system + GitHub mode
# ---------------------------------------------------------------------------
# ponytail: in-memory conversation states, fine for single-process. Persist if multi-worker.

async def cmd_mode(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch between Normal and GitHub mode."""
    chat_id = update.effective_chat.id
    ensure_user(chat_id)
    user = get_user(chat_id)
    cur = user["mode"] if user else "normal"
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{'✅ ' if cur == 'normal' else ''}🌐 Normal", callback_data="mode:normal"),
            InlineKeyboardButton(f"{'✅ ' if cur == 'github' else ''}🐙 GitHub", callback_data="mode:github"),
        ]
    ])
    await update.message.reply_text(
        f"⚙️ *Modo actual:* {cur}\n\nSeleccioná el modo:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def cmd_github(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch to GitHub mode and show dashboard."""
    chat_id = update.effective_chat.id
    ensure_user(chat_id)
    set_mode(chat_id, "github")
    await _gh_show_menu(update.effective_chat.id, update.message, _ctx)


async def cmd_settoken(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set GitHub personal access token."""
    chat_id = update.effective_chat.id
    ensure_user(chat_id)
    args = ctx.args
    if args:
        token = args[0]
        gh = GitHubClient(token)
        user_data = gh.validate_token()
        if user_data:
            set_github_token(chat_id, token)
            set_github_user(chat_id, user_data["login"])
            await update.message.reply_text(
                f"✅ Token configurado para **{user_data['login']}** 🐙\n\n"
                f"📦 Repos: {user_data['public_repos']} públicos · {user_data.get('owned_private_repos', 0)} privados\n"
                f"👥 Seguidores: {user_data['followers']} · Siguiendo: {user_data['following']}",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text("❌ Token inválido. Generá uno en GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens")
    else:
        conv[chat_id] = {"action": "set_token"}
        await update.message.reply_text(
            "🔑 Enviame tu *GitHub Personal Access Token* (classic o fine-grained con repo scope).\n\n"
            "📌 Crear uno: GitHub.com → Settings → Developer settings → Personal access tokens → Tokens (classic)\n"
            "✅ Marcá el scope `repo` para acceso completo.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cmd_mode_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cb = update.callback_query
    await cb.answer()
    _, mode = cb.data.split(":", 1)
    chat_id = update.effective_chat.id
    set_mode(chat_id, mode)
    if mode == "github":
        await _gh_show_menu(chat_id, cb.message, _ctx, edit=True)
    else:
        await cb.message.edit_text(
            "🌐 *Modo Normal activado*\n\nUsá /mode para volver a cambiar o /github para ir a GitHub.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── GitHub Dashboard / Menú ──────────────────────────────────────────────

# ponytail: button-only navigation, no state machine. If user wants breadcrumbs, add path stack.

async def _gh_show_menu(
    chat_id: int, msg_or_update: Any, _ctx: ContextTypes.DEFAULT_TYPE,
    *, edit: bool = False, text: str = "",
) -> None:
    """Show the GitHub main dashboard with action buttons."""
    user = get_user(chat_id)
    token = user["github_token"] if user else ""
    gh_user = user["github_user"] if user else ""

    if not token:
        lines = [
            "🐙 *GitHub Dashboard*\n",
            "⚠️ *No hay token configurado.*\n",
            "Usá /settoken o presioná el botón de abajo para conectar tu cuenta de GitHub.",
        ]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Configurar Token", callback_data="gh:settoken")],
            [InlineKeyboardButton("🌐 Volver a Normal", callback_data="mode:normal")],
        ])
        msg = "\n".join(lines)
    else:
        profile_line = f"👤 **{gh_user}**" if gh_user else "👤 *Conectado*"
        lines = [
            f"🐙 *GitHub Dashboard*\n",
            f"{profile_line}\n",
            f"📌 *Acciones:*",
        ]
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📁 Mis Repos", callback_data="gh:list"),
                InlineKeyboardButton("🔍 Buscar", callback_data="gh:search"),
            ],
            [
                InlineKeyboardButton("➕ Crear Repo", callback_data="gh:create"),
                InlineKeyboardButton("📥 Clonar Todo", callback_data="gh:cloneall"),
            ],
            [
                InlineKeyboardButton("👤 Perfil", callback_data="gh:profile"),
                InlineKeyboardButton("🔑 Cambiar Token", callback_data="gh:settoken"),
            ],
            [InlineKeyboardButton("🌐 Volver a Normal", callback_data="mode:normal")],
        ])
        msg = "\n".join(lines)

    kwargs = dict(parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    if edit:
        await msg_or_update.edit_text(text or msg, **kwargs)
    else:
        await msg_or_update.reply_text(text or msg, **kwargs)


# ── GitHub callback router ───────────────────────────────────────────────

async def _gh_main_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Route gh:* callbacks."""
    cb = update.callback_query
    await cb.answer()
    data = cb.data
    chat_id = update.effective_chat.id
    user = get_user(chat_id)
    token = user["github_token"] if user else ""

    if data == "gh:menu":
        return await _gh_show_menu(chat_id, cb.message, ctx, edit=True)

    if data == "gh:settoken":
        conv[chat_id] = {"action": "set_token"}
        return await cb.message.edit_text(
            "🔑 Enviame tu *GitHub Personal Access Token* en un mensaje.",
            parse_mode=ParseMode.MARKDOWN,
        )

    if data == "gh:profile":
        return await _gh_profile(chat_id, cb.message, token, ctx)

    if data == "gh:list":
        return await _gh_list_repos(chat_id, cb.message, token, ctx)

    if data.startswith("gh:list:"):
        page = int(data.split(":")[-1])
        return await _gh_list_repos(chat_id, cb.message, token, ctx, page=page)

    if data == "gh:search":
        conv[chat_id] = {"action": "search_repo"}
        return await cb.message.edit_text(
            "🔍 Decime qué querés buscar en GitHub (nombre, lenguaje, tema...):",
            parse_mode=ParseMode.MARKDOWN,
        )

    if data == "gh:create":
        conv[chat_id] = {"action": "create_repo"}
        return await cb.message.edit_text(
            "✏️ Enviame el *nombre del repositorio* a crear.",
            parse_mode=ParseMode.MARKDOWN,
        )

    if data.startswith("gh:create:confirm:"):
        return await _gh_do_create(chat_id, cb.message, token, data, ctx)

    if data.startswith("gh:detail:"):
        fn = data.split(":", 2)[-1]
        return await _gh_show_repo(chat_id, cb.message, token, fn, ctx)

    if data.startswith("gh:delete:"):
        fn = data.split(":", 2)[-1]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Sí, eliminar", callback_data=f"gh:delete:confirm:{fn}")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="gh:menu")],
        ])
        return await cb.message.edit_text(
            f"⚠️ *¿Eliminar `{fn}`?*\n\nEsta acción no se puede deshacer.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    if data.startswith("gh:delete:confirm:"):
        return await _gh_do_delete(chat_id, cb.message, token, data, ctx)

    if data.startswith("gh:upload:"):
        fn = data.split(":", 2)[-1]
        conv[chat_id] = {"action": "upload_file", "full_name": fn}
        return await cb.message.edit_text(
            f"📤 Enviame el *archivo* a subir a `{fn}`.\n\n"
            f"Luego te pediré la ruta (path) y el mensaje del commit.",
            parse_mode=ParseMode.MARKDOWN,
        )

    if data.startswith("gh:clone:"):
        fn = data.split(":", 2)[-1]
        return await _gh_do_clone(chat_id, cb.message, token, fn, ctx)

    if data == "gh:cloneall":
        return await _gh_do_clone_all(chat_id, cb.message, token, ctx)

    if data.startswith("gh:branches:"):
        fn = data.split(":", 2)[-1]
        return await _gh_list_branches(chat_id, cb.message, token, fn, ctx)

    if data.startswith("gh:commits:"):
        fn = data.split(":", 2)[-1]
        return await _gh_list_commits(chat_id, cb.message, token, fn, ctx)

    if data.startswith("gh:fork:"):
        fn = data.split(":", 2)[-1]
        return await _gh_do_fork(chat_id, cb.message, token, fn, ctx)


# ── GitHub action implementations ────────────────────────────────────────

async def _gh_profile(chat_id: int, msg, token: str, ctx) -> None:
    if not token:
        return await msg.edit_text("⚠️ No hay token. Usá /settoken.")
    try:
        gh = GitHubClient(token)
        u = gh.get_user()
        lines = [
            f"🐙 *Perfil de GitHub*",
            f"",
            f"👤 **{u['login']}**",
            f"📛 {u.get('name') or '—'}",
            f"📝 {u.get('bio') or '—'}",
            f"",
            f"📦 {u['public_repos']} públicos · {u.get('owned_private_repos', 0)} privados",
            f"👥 {u['followers']} seguidores · {u['following']} siguiendo",
            f"⭐ {u.get('total_starred', '?')} estrellas",
            f"",
            f"📅 Creado: {u['created_at'][:10]}",
        ]
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="gh:menu")]])
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except GitHubError as e:
        await msg.edit_text(f"❌ {e}")


async def _gh_list_repos(chat_id: int, msg, token: str, ctx, page: int = 1) -> None:
    if not token:
        return await msg.edit_text("⚠️ No hay token. Usá /settoken.")
    try:
        gh = GitHubClient(token)
        repos = gh.list_repos(per_page=10)
        if not repos:
            return await msg.edit_text("📭 No tenés repositorios.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Volver", callback_data="gh:menu")],
            ]))

        lines = ["📁 *Tus repositorios*\n"]
        buttons = []
        for r in repos:
            name = r["full_name"]
            priv = "🔒" if r["private"] else "🌍"
            lang = r.get("language") or "?"
            lines.append(f"{priv} **{name}**  ⭐{r['stargazers_count']}  📐{lang}")
            buttons.append([InlineKeyboardButton(f"📁 {name.split('/')[1][:30]}", callback_data=f"gh:detail:{name}")])

        buttons.append([InlineKeyboardButton("⬅️ Volver", callback_data="gh:menu")])
        kb = InlineKeyboardMarkup(buttons)
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except GitHubError as e:
        await msg.edit_text(f"❌ {e}")


async def _gh_show_repo(chat_id: int, msg, token: str, full_name: str, ctx) -> None:
    if not token:
        return await msg.edit_text("⚠️ No hay token.")
    try:
        gh = GitHubClient(token)
        r = gh.get_repo(full_name)
        priv = "🔒" if r["private"] else "🌍"
        desc = (r.get("description") or "Sin descripción")[:200]
        lines = [
            f"{priv} **{full_name}**",
            f"📝 {desc}",
            f"",
            f"⭐ {r['stargazers_count']}  🍴 {r['forks_count']}  📐 {r.get('language') or '?'}",
            f"📋 {r.get('open_issues_count', 0)} issues  🛠 {r.get('default_branch', 'main')}",
            f"📅 Último push: {r.get('pushed_at', '?')[:10]}",
        ]
        if r.get("homepage"):
            lines.append(f"🌐 {r['homepage']}")

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📥 Clonar", callback_data=f"gh:clone:{full_name}"),
                InlineKeyboardButton("🌿 Branches", callback_data=f"gh:branches:{full_name}"),
            ],
            [
                InlineKeyboardButton("📤 Subir Archivo", callback_data=f"gh:upload:{full_name}"),
                InlineKeyboardButton("📋 Commits", callback_data=f"gh:commits:{full_name}"),
            ],
            [
                InlineKeyboardButton("🔄 Fork", callback_data=f"gh:fork:{full_name}"),
                InlineKeyboardButton("❌ Eliminar", callback_data=f"gh:delete:{full_name}"),
            ],
            [InlineKeyboardButton("⬅️ Volver", callback_data="gh:list")],
        ])
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except GitHubError as e:
        await msg.edit_text(f"❌ {e}")


async def _gh_do_create(chat_id: int, msg, token: str, data: str, ctx) -> None:
    # data = "gh:create:confirm:name:private"
    parts = data.split(":")
    name = parts[3] if len(parts) > 3 else ""
    private = len(parts) > 4 and parts[4] == "private"
    if not name:
        return await msg.edit_text("❌ Nombre inválido.")
    try:
        gh = GitHubClient(token)
        r = gh.create_repo(name, private=private)
        await msg.edit_text(
            f"✅ Repo creado: **{r['full_name']}**\n"
            f"🌐 {r['html_url']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📁 Ver repo", callback_data=f"gh:detail:{r['full_name']}")],
                [InlineKeyboardButton("⬅️ Volver", callback_data="gh:menu")],
            ]),
        )
    except GitHubError as e:
        await msg.edit_text(f"❌ {e}")


async def _gh_do_delete(chat_id: int, msg, token: str, data: str, ctx) -> None:
    fn = data.split(":", 2)[-1]
    try:
        gh = GitHubClient(token)
        gh.delete_repo(fn)
        await msg.edit_text(f"✅ Repositorio `{fn}` eliminado.", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="gh:menu")]]))
    except GitHubError as e:
        await msg.edit_text(f"❌ {e}")


async def _gh_do_clone(chat_id: int, msg, token: str, full_name: str, ctx) -> None:
    try:
        gh = GitHubClient(token)
        path, filename = gh.download_repo(full_name)
        await msg.edit_text("⬆️ Subiendo ZIP…")
        await ctx.bot.send_document(
            chat_id,
            document=open(path, "rb"),
            filename=filename,
            caption=f"📦 `{full_name}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="gh:menu")]]),
        )
        await msg.delete()
        if os.path.exists(path):
            os.unlink(path)
    except GitHubError as e:
        await msg.edit_text(f"❌ {e}")
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


async def _gh_do_clone_all(chat_id: int, msg, token: str, ctx) -> None:
    try:
        gh = GitHubClient(token)
        user = gh.get_user()
        username = user["login"]
        await msg.edit_text(f"⏳ Descargando todos los repos de **{username}**…", parse_mode=ParseMode.MARKDOWN)
        results = gh.download_all_user_repos(username)
        if not results:
            return await msg.edit_text("📭 No se pudo descargar ningún repo.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="gh:menu")]]))
        await msg.edit_text(f"📦 Subiendo {len(results)} repositorios…")
        for path, filename in results[:10]:  # ponytail: max 10 ZIPs per batch
            try:
                await ctx.bot.send_document(
                    chat_id, document=open(path, "rb"),
                    filename=filename,
                    caption=f"📦 `{filename}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
            finally:
                if os.path.exists(path):
                    os.unlink(path)
        await ctx.bot.send_message(chat_id,
            f"✅ {len(results)} repos descargados.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="gh:menu")]]))
        await msg.delete()
    except GitHubError as e:
        await msg.edit_text(f"❌ {e}")


async def _gh_list_branches(chat_id: int, msg, token: str, full_name: str, ctx) -> None:
    try:
        gh = GitHubClient(token)
        branches = gh.list_branches(full_name)
        lines = [f"🌿 *Branches de {full_name}*\n"]
        for b in branches[:30]:
            lines.append(f"• `{b['name']}`")
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data=f"gh:detail:{full_name}")]]))
    except GitHubError as e:
        await msg.edit_text(f"❌ {e}")


async def _gh_list_commits(chat_id: int, msg, token: str, full_name: str, ctx) -> None:
    try:
        gh = GitHubClient(token)
        commits = gh.list_commits(full_name)
        lines = [f"📋 *Últimos commits — {full_name}*\n"]
        for c in commits[:10]:
            sha = c["sha"][:7]
            author = c["commit"]["author"]["name"]
            msg_text = (c["commit"]["message"] or "").split("\n")[0][:60]
            lines.append(f"`{sha}` {msg_text} — {author}")
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data=f"gh:detail:{full_name}")]]))
    except GitHubError as e:
        await msg.edit_text(f"❌ {e}")


async def _gh_do_fork(chat_id: int, msg, token: str, full_name: str, ctx) -> None:
    try:
        gh = GitHubClient(token)
        r = gh.fork_repo(full_name)
        await msg.edit_text(
            f"✅ Fork creado: **{r['full_name']}**\n🌐 {r['html_url']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="gh:menu")]]),
        )
    except GitHubError as e:
        await msg.edit_text(f"❌ {e}")


# ── Text message router (covers conversation states + GitHub mode) ────────

async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages: conversation workflows and GitHub mode commands."""
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    user_data = get_user(chat_id)
    token = user_data["github_token"] if user_data else ""

    # Check active conversation
    state = conv.get(chat_id)
    if state:
        action = state["action"]

        if action == "set_token":
            gh = GitHubClient(text)
            user_data = gh.validate_token()
            if user_data:
                set_github_token(chat_id, text)
                set_github_user(chat_id, user_data["login"])
                del conv[chat_id]
                await update.message.reply_text(
                    f"✅ Token configurado para **{user_data['login']}**",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🐙 Ir al Dashboard", callback_data="gh:menu")],
                    ]),
                )
            else:
                await update.message.reply_text("❌ Token inválido. Intentá de nuevo o /cancel.")
            return

        if action == "create_repo":
            name = text.strip().replace(" ", "-").lower()
            if not name:
                return await update.message.reply_text("❌ Nombre inválido.")
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🌍 Público", callback_data=f"gh:create:confirm:{name}:public"),
                    InlineKeyboardButton("🔒 Privado", callback_data=f"gh:create:confirm:{name}:private"),
                ],
                [InlineKeyboardButton("❌ Cancelar", callback_data="gh:menu")],
            ])
            del conv[chat_id]
            await update.message.reply_text(
                f"📁 Crear `{name}` como…",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
            )
            return

        if action == "search_repo":
            del conv[chat_id]
            await _gh_do_search(chat_id, update.message, token, text, ctx)
            return

        if action == "upload_file":
            full_name = state["full_name"]
            # User sent a file? Check if it's a document
            doc = update.message.document
            if doc:
                # Save file, then ask for path and commit message
                conv[chat_id] = {"action": "upload_confirm", "full_name": full_name, "file_id": doc.file_id, "file_name": doc.file_name}
                await update.message.reply_text(
                    f"📄 Archivo `{doc.file_name}` recibido.\n\n"
                    f"Ahora enviame la *ruta* donde guardarlo en el repo (ej: `docs/guia.txt`) "
                    f"y el *mensaje del commit* separados por un salto de línea.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await update.message.reply_text("❌ Enviame un *archivo* (PDF, imagen, código...).", parse_mode=ParseMode.MARKDOWN)
            return

        if action == "upload_confirm":
            full_name = state["full_name"]
            file_id = state["file_id"]
            file_name = state.get("file_name", "file")
            lines = text.split("\n", 1)
            path = lines[0].strip() or file_name
            commit_msg = lines[1].strip() if len(lines) > 1 else f"Add {path}"

            # Download file from Telegram
            try:
                tg_file = await ctx.bot.get_file(file_id)
                file_bytes = await tg_file.download_as_bytearray()
                import base64
                b64 = base64.b64encode(file_bytes).decode()

                gh = GitHubClient(token)
                gh.create_or_update_file(full_name, path, b64, commit_msg)
                del conv[chat_id]
                await update.message.reply_text(
                    f"✅ Archivo subido a `{full_name}/{path}`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ Volver", callback_data=f"gh:detail:{full_name}")],
                    ]),
                )
            except GitHubError as e:
                await update.message.reply_text(f"❌ {e}")
            except Exception as e:
                await update.message.reply_text(f"❌ Error: {e}")
            return

    # GitHub mode: non-command text shows menu
    user = get_user(chat_id)
    if user and user["mode"] == "github" and not text.startswith("/"):
        await _gh_show_menu(chat_id, update.message, ctx)


async def document_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document uploads for GitHub file upload workflow."""
    chat_id = update.effective_chat.id
    state = conv.get(chat_id)
    if state and state["action"] == "upload_file":
        full_name = state["full_name"]
        doc = update.message.document
        conv[chat_id] = {
            "action": "upload_confirm",
            "full_name": full_name,
            "file_id": doc.file_id,
            "file_name": doc.file_name,
        }
        await update.message.reply_text(
            f"📄 Archivo `{doc.file_name}` recibido.\n\n"
            f"Ahora enviame la *ruta* donde guardarlo (ej: `docs/guia.txt`) "
            f"y el *mensaje del commit* separados por un salto de línea.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def _gh_do_search(chat_id: int, msg, token: str, query: str, ctx) -> None:
    try:
        if token:
            gh = GitHubClient(token)
            repos = gh.search_repos(query, per_page=5)
        else:
            repos = search_repos(query, per_page=5)
        if not repos:
            return await msg.reply_text("🔍 Sin resultados.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="gh:menu")]]))
        for r in repos:
            fn = r["full_name"]
            desc = (r.get("description") or "")[:150]
            info = (
                f"📦 **{fn}**\n{desc}\n"
                f"⭐ {r['stargazers_count']}  🍴 {r['forks_count']}  📐 {r.get('language') or '?'}"
            )
            await msg.reply_text(
                info, parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔍 Ver detalle", callback_data=f"gh:detail:{fn}"),
                ]]),
            )
        await msg.reply_text("✅ Resultados arriba.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="gh:menu")]]))
    except GitHubError as e:
        await msg.reply_text(f"❌ {e}")


async def cmd_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel current conversation workflow."""
    chat_id = update.effective_chat.id
    if chat_id in conv:
        del conv[chat_id]
        await update.message.reply_text("❌ Operación cancelada.")
    else:
        await update.message.reply_text("🤷 No hay ninguna operación en curso.")


# ── Error handler ─────────────────────────────────────────────────────────

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Exception while handling update: %s", ctx.error)


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------
async def check_rates(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic rate check — notify subscribers on change."""
    new = await fetch_latest_rates()
    if not new:
        return

    old = load_rates()
    if not rates_changed(old, new):
        return

    save_rates(new)
    log.info("Rates changed – notifying subscribers")
    subs = get_subscribers()
    msg = (
        f"🔄 *Tasas actualizadas*\n\n"
        f"```\n{format_rates(new, header='🔄 ACTUALIZACIÓN')}\n```"
    )
    for sid in subs:
        try:
            await ctx.bot.send_message(int(sid), msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.warning("Failed to notify %s: %s", sid, e)


async def daily_summary(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily rate summary to all subscribers."""
    rates = load_rates()
    if not rates:
        rates = await fetch_latest_rates()
        if rates:
            save_rates(rates)
        else:
            log.warning("No rates for daily summary")
            return

    msg = (
        f"🌅 *Resumen diario — Tasas de Cuba*\n\n"
        f"```\n{format_rates(rates, header='🌅 RESUMEN DIARIO')}\n```"
        f"\n_Actualizado: {datetime.now().strftime('%d/%m/%Y %H:%M UTC')}_\n"
        f"@nautaii"
    )
    subs = get_subscribers()
    for sid in subs:
        try:
            await ctx.bot.send_message(int(sid), msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.warning("Failed daily %s: %s", sid, e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def post_init(app: Application) -> None:
    """Register bot commands with Telegram on startup."""
    commands = [
        BotCommand("start", "Bienvenida"),
        BotCommand("tasas", "Cotizaciones USD, EUR, MLC"),
        BotCommand("moneda", "Cotización específica"),
        BotCommand("convertir", "Convertir CUP ↔ USD/EUR/MLC"),
        BotCommand("asesor", "Análisis IA de tasas"),
        BotCommand("explore", "Buscar repos en GitHub"),
        BotCommand("clone", "Descargar ZIP de GitHub"),
        BotCommand("branches", "Branches de un repo"),
        BotCommand("github", "Menú GitHub completo"),
        BotCommand("mode", "Cambiar modo Normal/GitHub"),
        BotCommand("settoken", "Configurar token de GitHub"),
        BotCommand("sub", "Activar notificaciones"),
        BotCommand("unsub", "Desactivar notificaciones"),
        BotCommand("ayuda", "Ayuda completa"),
    ]
    await app.bot.set_my_commands(commands)
    log.info("Bot commands registered")


# ponytail: health-check server for platforms that kill apps without an open port
# (Koyeb, Render, etc.). No framework, stdlib http.server. One thread, no io.
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *_: Any) -> None:
        pass  # silence logs


def _start_health_server() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("Health server on port %d", port)


# ---------------------------------------------------------------------------
# Autoping — keep free hosts alive
# ---------------------------------------------------------------------------
# ponytail: simple GET loop. Add exponential backoff if host rate-limits.
def _start_autoping() -> None:
    if not RENDER_URL:
        log.info("Autoping disabled (no RENDER_URL)")
        return
    def _ping():
        while True:
            try:
                httpx.get(f"{RENDER_URL}/health", timeout=10)
            except Exception:
                pass
            time.sleep(600)  # 10 min
    t = threading.Thread(target=_ping, daemon=True)
    t.start()
    log.info("Autoping every 10min → %s", RENDER_URL)


# ---------------------------------------------------------------------------
# GitHub DL — buscar y descargar repos
# ---------------------------------------------------------------------------
# ponytail: sin rate-limit backoff. Agregar token+retry si aparecen 403s.
GITHUB_API = "https://api.github.com"
MAX_DL = int(os.environ.get("MAX_DL_MB", "200")) << 20

def gh_get(path: str, **kw: Any) -> Any:
    r = requests.get(f"{GITHUB_API}{path}", timeout=15, **kw)
    r.raise_for_status()
    return r.json()

def search_repos(query: str, per_page: int = 10) -> list:
    return gh_get("/search/repositories", params={"q": query, "per_page": per_page}).get("items", [])

def get_repo(full_name: str) -> dict:
    return gh_get(f"/repos/{full_name}")

def get_branches(full_name: str) -> list:
    return gh_get(f"/repos/{full_name}/branches")

def parse_repo(text: str) -> str:
    s = text.split(maxsplit=1)[-1] if " " in text else text
    s = s.strip().replace("https://github.com/", "").replace("http://github.com/", "").rstrip("/")
    parts = s.split("/")
    return f"{parts[0]}/{parts[1]}" if len(parts) >= 2 and parts[0] and parts[1] else ""

async def download_repo(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                        full_name: str, branch: str | None = None) -> None:
    msg = update.effective_message
    status = await msg.reply_text(f"⏳ `{full_name}`…", parse_mode=ParseMode.MARKDOWN)
    try:
        repo = get_repo(full_name)
    except requests.HTTPError as e:
        return await status.edit_text(f"❌ `{full_name}` — {e.response.status_code}")
    if repo.get("size", 0) * 2048 > MAX_DL:
        return await status.edit_text(f"❌ Muy grande ({repo['size']>>10}MB)")
    branch = branch or repo.get("default_branch", "main")
    dl_url = f"https://github.com/{full_name}/archive/refs/heads/{branch}.zip"
    await status.edit_text(f"📥 `{full_name}` / `{branch}`…", parse_mode=ParseMode.MARKDOWN)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        r = requests.get(dl_url, stream=True, timeout=300)
        r.raise_for_status()
        for chunk in r.iter_content(1 << 14):
            if chunk:
                tmp.write(chunk)
        tmp.close()
        sz = os.path.getsize(tmp.name)
        if sz > MAX_DL:
            return await _cleanup(tmp.name, status, f"❌ ZIP muy grande ({sz>>20}MB)")
        await status.edit_text("⬆️ Subiendo…")
        await msg.reply_document(
            tmp.name,
            filename=f"{full_name.replace('/', '_')}_{branch}.zip",
            caption=f"📦 `{full_name}` (`{branch}`)",
            parse_mode=ParseMode.MARKDOWN)
        await status.delete()
    except requests.HTTPError as e:
        err = "Branch no existe" if e.response.status_code == 404 else f"HTTP {e.response.status_code}"
        await status.edit_text(f"❌ {err}")
    except Exception as e:
        await status.edit_text(f"❌ {e}")
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)

async def _cleanup(path: str, msg, text: str) -> None:
    if os.path.exists(path):
        os.unlink(path)
    await msg.edit_text(text)

# ── GitHub command handlers ──

async def cmd_explore(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.message.text[len("/explore"):].strip()
    if not q:
        return await update.message.reply_text("Usá `/explore nombre-de-repo`", parse_mode=ParseMode.MARKDOWN)
    repos = search_repos(q)
    if not repos:
        return await update.message.reply_text("Sin resultados.")
    for r in repos[:5]:
        fn = r["full_name"]
        desc = (r.get("description") or "")[:200]
        info = (
            f"📦 **{fn}**\n{desc}\n"
            f"⭐ {r['stargazers_count']}  🍴 {r['forks_count']}  📐 {r.get('language') or '?'}"
        )
        await update.message.reply_text(
            info, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📥 Download", callback_data=f"dl|{fn}"),
                InlineKeyboardButton("🌿 Branches", callback_data=f"br|{fn}"),
            ]]))

async def cmd_clone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    parts = update.message.text.split(maxsplit=2)
    fn = parse_repo(parts[1]) if len(parts) > 1 else ""
    branch = parts[2].strip() if len(parts) > 2 else None
    if not fn:
        return await update.message.reply_text("Usá `/clone user/repo [branch]`", parse_mode=ParseMode.MARKDOWN)
    await download_repo(update, ctx, fn, branch)

async def cmd_branches(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    fn = parse_repo(update.message.text[len("/branches"):])
    if not fn:
        return await update.message.reply_text("Usá `/branches user/repo`", parse_mode=ParseMode.MARKDOWN)
    try:
        branches = get_branches(fn)
    except requests.HTTPError as e:
        return await update.message.reply_text(f"❌ `{fn}` — {e.response.status_code}")
    text = "🌿 **" + fn + "** branches:\n" + "\n".join(f"• `{b['name']}`" for b in branches[:30])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def inline_search(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.inline_query.query.strip()
    if not q:
        return await update.inline_query.answer([], cache_time=0,
            switch_pm_text="🔍 Buscar repos en GitHub",
            switch_pm_parameter="s")
    repos = search_repos(q)
    results = []
    for r in repos[:15]:
        fn = r["full_name"]
        desc = (r.get("description") or "")[:120]
        results.append(InlineQueryResultArticle(
            id=fn,
            title=f"⭐ {r['stargazers_count']}  {fn}",
            description=desc or "(sin descripción)",
            input_message_content=InputTextMessageContent(
                f"📦 **{fn}**\n{(r.get('description') or '')[:300]}\n"
                f"⭐ {r['stargazers_count']}  🍴 {r['forks_count']}\n\n"
                f"Usá /clone {fn} para descargar",
                parse_mode=ParseMode.MARKDOWN)))
    await update.inline_query.answer(results, cache_time=30, is_personal=True)

async def gh_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cb = update.callback_query
    await cb.answer()
    action, fn = cb.data.split("|", 1)
    if action == "dl":
        await download_repo(update, ctx, fn)
    elif action == "br":
        try:
            branches = get_branches(fn)
        except requests.HTTPError as e:
            return await cb.message.reply_text(f"❌ `{fn}` — {e.response.status_code}")
        text = "🌿 **" + fn + "** branches:\n" + "\n".join(f"• `{b['name']}`" for b in branches[:30])
        await cb.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await cb.message.delete()


def main() -> None:
    init_db()

    # Migrate old subscribers.json → SQLite
    old_subs = Path("subscribers.json")
    if old_subs.exists() and old_subs.stat().st_size > 10:
        try:
            data = json.loads(old_subs.read_text())
            for sid_str in data:
                db_sub(int(sid_str))
            log.info("Migrated %d subscribers from JSON → SQLite", len(data))
        except Exception as e:
            log.warning("Subscriber migration failed: %s", e)

    _start_health_server()
    _start_autoping()

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # Rate commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tasas", tasas))
    app.add_handler(CommandHandler("moneda", moneda))
    app.add_handler(CommandHandler("convertir", convertir))
    app.add_handler(CommandHandler("asesor", cmd_asesor))

    # Subscribe
    app.add_handler(CommandHandler("sub", subscribe))
    app.add_handler(CommandHandler("unsub", unsubscribe))
    app.add_handler(CommandHandler("ayuda", help_cmd))

    # Mode / GitHub
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("github", cmd_github))
    app.add_handler(CommandHandler("settoken", cmd_settoken))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Old-style GitHub commands (keep for compat)
    app.add_handler(CommandHandler("explore", cmd_explore))
    app.add_handler(CommandHandler("clone", cmd_clone))
    app.add_handler(CommandHandler("branches", cmd_branches))

    # Document handler (file upload to GitHub)
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))

    # Text handler (conversation states + GitHub mode)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Callbacks
    app.add_handler(CallbackQueryHandler(cmd_mode_callback, pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(_gh_main_callback, pattern=r"^gh:"))
    app.add_handler(CallbackQueryHandler(gh_callback, pattern=r"^(dl|br)\|."))

    # Inline
    app.add_handler(InlineQueryHandler(inline_search))

    app.add_error_handler(error_handler)

    # Schedules
    jq = app.job_queue
    if jq is not None:
        jq.run_repeating(
            check_rates,
            interval=CHECK_INTERVAL_MIN * 60,
            first=30,
        )
        jq.run_daily(
            daily_summary,
            time=dtime(hour=DAILY_HOUR, minute=0),
        )

    log.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
