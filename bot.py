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
from datetime import datetime, time as dtime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import httpx
import requests
from bs4 import BeautifulSoup
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, InlineQueryHandler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("BOT_TOKEN", "")
if not TOKEN:
    raise SystemExit("BOT_TOKEN env var is required")

CHANNEL_URL = "https://t.me/s/eltoquecom"
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
RATES_FILE = DATA_DIR / "rates.json"
SUBS_FILE = DATA_DIR / "subscribers.json"
CHECK_INTERVAL_MIN = 30  # how often to check for rate changes
DAILY_HOUR = 9           # daily summary hour (UTC)

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

def load_subs() -> dict[str, list[int]]:
    """Returns {chat_id: [chat_id]} — simple set of subscriber chat IDs."""
    return _load_json(SUBS_FILE)

def save_subs(subs: dict[str, list[int]]) -> None:
    _save_json(SUBS_FILE, subs)

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
    subs = load_subs()
    if str(chat_id) not in subs:
        subs[str(chat_id)] = [chat_id]
        save_subs(subs)
        log.info("New subscriber: %s", chat_id)

    rates = load_rates()
    msg = (
        "👋 *Bienvenido al Bot de Tasas de Cuba*\n\n"
        "Consultá el precio del dólar, euro y MLC en el mercado "
        "informal cubano, actualizado desde @eltoquecom.\n\n"
        "📌 *Comandos:*\n"
        "  /tasas — Cotizaciones USD, EUR, MLC\n"
        "  /moneda USD — Cotización específica\n"
        "  /convertir 100 USD — Conversión CUP ↔ moneda\n"
        "  /explore django — Buscar repos en GitHub\n"
        "  /clone user/repo — Descargar ZIP de GitHub\n"
        "  /sub — Notificaciones diarias\n"
        "  /unsub — Desactivar notificaciones\n"
        "  /ayuda — Ayuda completa\n\n"
        f"_{rates.get('date_raw', '—')}_\n"
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
    subs = load_subs()
    subs[str(chat_id)] = [chat_id]
    save_subs(subs)
    await update.message.reply_text(
        "✅ Notificaciones activadas. Recibirás la tasa cada día "
        f"~{DAILY_HOUR:02d}:00 UTC y cuando haya cambios."
    )


async def unsubscribe(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    subs = load_subs()
    subs.pop(str(chat_id), None)
    save_subs(subs)
    await update.message.reply_text("❌ Notificaciones desactivadas.")


async def help_cmd(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, _ctx)


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
    subs = load_subs()
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
    subs = load_subs()
    for sid in subs:
        try:
            await ctx.bot.send_message(int(sid), msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.warning("Failed daily %s: %s", sid, e)


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Exception while handling update: %s", ctx.error)


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
        BotCommand("explore", "Buscar repos en GitHub"),
        BotCommand("clone", "Descargar ZIP de GitHub"),
        BotCommand("branches", "Branches de un repo"),
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
    _start_health_server()

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tasas", tasas))
    app.add_handler(CommandHandler("moneda", moneda))
    app.add_handler(CommandHandler("convertir", convertir))
    app.add_handler(CommandHandler("explore", cmd_explore))
    app.add_handler(CommandHandler("clone", cmd_clone))
    app.add_handler(CommandHandler("branches", cmd_branches))
    app.add_handler(CommandHandler("sub", subscribe))
    app.add_handler(CommandHandler("unsub", unsubscribe))
    app.add_handler(CommandHandler("ayuda", help_cmd))
    app.add_handler(InlineQueryHandler(inline_search))
    app.add_handler(CallbackQueryHandler(gh_callback, pattern=r"^(dl|br)\|."))
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
