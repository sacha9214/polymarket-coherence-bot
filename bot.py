"""
Coherence Bot — alertes Discord sur les incohérences logiques de Polymarket.

Le bot ne prédit rien et ne conseille rien. Il signale des portefeuilles dont le
gain est positif quel que soit le résultat, parce que les prix violent une
contrainte mathématique entre marchés liés. Toute la logique de détection vit
dans `coherence.py`, testable sans Discord (`python3 coherence.py`).

Jeton : mets-le dans un fichier token.txt à côté de ce script (une ligne),
ou dans la variable d'environnement DISCORD_BOT_TOKEN.
"""

# Surtout PAS de `from __future__ import annotations` ici : py-cord lit les
# annotations des commandes pour typer les options du menu Discord. En mode PEP 563
# elles deviennent des chaînes, py-cord ne reconnaît plus `int`/`float` et affiche
# des champs texte à la place des champs numériques — sans la moindre erreur.

import asyncio
import fcntl
import os
import sqlite3
import sys
import time
from pathlib import Path

import discord
from discord.ext import tasks

import coherence as C

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
if not TOKEN:
    _f = Path(__file__).with_name("token.txt")
    if _f.exists():
        TOKEN = _f.read_text(encoding="utf-8").strip()

GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0") or 0)
GUILDS = [GUILD_ID] if GUILD_ID else None

POLL_MINUTES = int(os.environ.get("COHERENCE_POLL_MINUTES", "3") or 3)
# Une même incohérence peut persister plusieurs cycles : on ne la ré-annonce pas
# tant que ce délai n'est pas écoulé, sauf si elle grossit nettement.
REALERT_HOURS = 6
# Ré-alerte anticipée si l'opportunité a beaucoup grossi depuis la dernière fois.
REALERT_GROWTH = 2.0

_LOCK_PATH = Path(__file__).with_name("bot.lock")
_lock_file = None


def acquire_single_instance_lock():
    """Deux instances se battraient pour enregistrer les commandes (chacune efface
    celles de l'autre) et doubleraient les alertes. Pris au LANCEMENT seulement,
    pour que le module reste importable par les tests."""
    global _lock_file
    _lock_file = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(
            "Another instance is already running (bot.lock held). "
            "Stop it first:  pkill -f bot.py"
        )
    _lock_file.write(str(os.getpid()))
    _lock_file.flush()


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
db = sqlite3.connect(Path(__file__).with_name("coherence.db"))
db.execute(
    """CREATE TABLE IF NOT EXISTS subs(
  channel_id INTEGER PRIMARY KEY,
  guild_id   INTEGER,
  min_apy    REAL,
  min_profit REAL,
  created    INTEGER)"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS seen(
  channel_id INTEGER,
  key        TEXT,
  last_alert INTEGER,
  last_profit REAL,
  PRIMARY KEY(channel_id, key))"""
)
db.execute("""CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)""")
db.commit()


def meta_get(k, default=None):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(k, v):
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    db.commit()


# auto_sync_commands=False : sinon py-cord enregistre AUSSI les commandes en global,
# elles cohabitent avec les copies par serveur et Discord les affiche en double.
bot = discord.Bot(intents=discord.Intents.default(), auto_sync_commands=False)

_lock = asyncio.Lock()
_cache: dict = {"ts": 0.0, "result": None}
CACHE_TTL = 120


async def get_scan(force: bool = False) -> C.ScanResult:
    """Un seul scan à la fois, partagé par la boucle et les commandes.

    Sans ce verrou, cinq personnes tapant /scan en même temps lanceraient cinq
    scans complets en parallèle et se feraient limiter par l'API Polymarket.
    """
    async with _lock:
        now = time.time()
        if not force and _cache["result"] and now - _cache["ts"] < CACHE_TTL:
            return _cache["result"]
        result = await C.scan()
        _cache.update(ts=now, result=result)
        meta_set("last_scan", int(now))
        meta_set("last_count", len(result.opportunities))
        return result


# ---------------------------------------------------------------------------
# Présentation
# ---------------------------------------------------------------------------

KIND_STYLE = {
    "buckets_under": ("🟢", 0x2ECC71, "Sum of outcomes below $1"),
    "buckets_over": ("🔵", 0x3498DB, "Sum of outcomes above $1"),
    "ladder_strikes": ("🟣", 0x9B59B6, "Price ladder out of order"),
    "ladder_dates": ("🟠", 0xE67E22, "Date ladder out of order"),
}


def horizon_label(days: float) -> str:
    if days < 1:
        return f"{days * 24:.0f}h"
    if days < 60:
        return f"{days:.0f}d"
    return f"{days / 30:.0f}mo"


def opp_embed(o: C.Opportunity) -> discord.Embed:
    icon, color, kind_label = KIND_STYLE.get(o.kind, ("⚪", 0x95A5A6, o.kind))

    e = discord.Embed(
        title=f"{icon} {o.title[:230]}",
        url=o.url,
        description=f"**{kind_label}** — {o.detail}",
        color=color,
    )

    legs = "\n".join(f"`{leg}`" for leg in o.legs[:12])
    if len(o.legs) > 12:
        legs += f"\n`… +{len(o.legs) - 12} more legs`"
    e.add_field(name="Trade", value=legs, inline=False)

    e.add_field(name="Size", value=f"{o.units:,.0f} shares", inline=True)
    e.add_field(name="Capital", value=C.fmt_usd(o.capital), inline=True)
    e.add_field(name="Locked for", value=horizon_label(o.days), inline=True)

    e.add_field(name="Locked-in profit", value=C.fmt_usd(o.profit), inline=True)
    e.add_field(name="Return", value=f"{o.roi:.2f}%", inline=True)
    e.add_field(name="Annualised", value=f"{o.apy:,.0f}%", inline=True)

    e.set_footer(
        text="Sizes come from the live order book, not mid prices. "
        "The book moves — re-check before trading."
    )
    return e


GUIDE = (
    "**What this channel is**\n"
    "Every alert here is a set of Polymarket positions whose combined payout is "
    "positive **no matter what happens**. Nothing is predicted. The bot only finds "
    "prices that contradict each other.\n\n"
    "**The three contradictions it looks for**\n"
    "🟢🔵 **Buckets** — outcomes that are mutually exclusive and cover every case "
    "must add up to exactly $1. When they don't, buying all of them (or all their "
    "opposites) locks in the gap.\n"
    "🟣 **Price ladders** — *BTC above $60k* can never be less likely than "
    "*BTC above $70k*. When the order flips, the pair is mispriced.\n"
    "🟠 **Date ladders** — same idea across deadlines.\n\n"
    "**How to read the numbers**\n"
    "`Size` is what the real order book can absorb right now, walked level by "
    "level — not the top-of-book quote. `Annualised` matters more than `Return`: "
    "0.3% that settles tomorrow beats 5% that settles next year.\n\n"
    "**What the bot does not tell you**\n"
    "• You must fill **every leg**. Polymarket has no all-or-nothing execution — "
    "if one leg fills and another doesn't, you are left with a directional bet.\n"
    "• The book moves in seconds. Treat every number as of the moment it was sent.\n"
    "• Fees are assumed to be zero. Check the current fee schedule before sizing up.\n"
    "• Silence is the normal state. A coherent market offers nothing."
)


# ---------------------------------------------------------------------------
# Boucle d'alertes
# ---------------------------------------------------------------------------


def should_alert(channel_id: int, o: C.Opportunity) -> bool:
    """Une incohérence peut durer des heures : la ré-annoncer à chaque cycle
    noierait le salon. On ne la répète qu'après REALERT_HOURS, ou plus tôt si
    elle a nettement grossi (ce qui est une information nouvelle)."""
    row = db.execute(
        "SELECT last_alert, last_profit FROM seen WHERE channel_id=? AND key=?",
        (channel_id, o.key),
    ).fetchone()
    if not row:
        return True
    last_alert, last_profit = row
    if time.time() - last_alert > REALERT_HOURS * 3600:
        return True
    return o.profit >= (last_profit or 0) * REALERT_GROWTH


def mark_alerted(channel_id: int, o: C.Opportunity):
    db.execute(
        "INSERT OR REPLACE INTO seen VALUES(?,?,?,?)",
        (channel_id, o.key, int(time.time()), o.profit),
    )
    db.commit()


@tasks.loop(minutes=POLL_MINUTES)
async def poll():
    subs = db.execute(
        "SELECT channel_id, min_apy, min_profit FROM subs"
    ).fetchall()
    if not subs:
        return

    try:
        result = await get_scan(force=True)
    except Exception as e:  # noqa: BLE001
        print(f"[poll] scan failed: {type(e).__name__}: {e}", flush=True)
        return

    for channel_id, min_apy, min_profit in subs:
        ch = bot.get_channel(channel_id)
        if ch is None:
            continue
        sent = 0
        for o in result.opportunities:
            if o.apy < (min_apy or 0) or o.profit < (min_profit or 0):
                continue
            if not should_alert(channel_id, o):
                continue
            try:
                await ch.send(embed=opp_embed(o))
            except discord.DiscordException as e:
                print(f"[poll] send failed on {channel_id}: {e}", flush=True)
                break
            mark_alerted(channel_id, o)
            sent += 1
            # Un pic de marché peut produire des dizaines d'incohérences d'un coup :
            # on plafonne pour ne pas transformer le salon en mur de texte.
            if sent >= 5:
                break

    # Purge : sans ça, la table grossit indéfiniment avec des clés mortes.
    db.execute("DELETE FROM seen WHERE last_alert < ?", (int(time.time()) - 7 * 86400,))
    db.commit()


@poll.before_loop
async def before_poll():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------


@bot.slash_command(
    name="scan",
    description="Scan Polymarket right now for logical inconsistencies",
    guild_ids=GUILDS,
)
async def scan_cmd(ctx, limit: int = 5):
    await ctx.defer()
    result = await get_scan()

    if not result.opportunities:
        await ctx.respond(
            embed=discord.Embed(
                title="No executable inconsistency",
                description=(
                    f"Scanned **{result.events_scanned}** events and "
                    f"**{result.markets}** markets in {result.duration:.1f}s.\n\n"
                    "This is the normal result. A coherent market offers nothing — "
                    "the bot earns its keep on the minutes when it isn't."
                ),
                color=0x95A5A6,
            )
        )
        return

    limit = max(1, min(limit, 8))
    top = result.opportunities[:limit]
    await ctx.respond(
        f"**{len(result.opportunities)}** inconsistencies found across "
        f"{result.markets} markets ({result.duration:.1f}s) — showing top {len(top)} by annualised return:",
        embeds=[opp_embed(o) for o in top],
    )


@bot.slash_command(
    name="watch",
    description="Send inconsistency alerts to this channel",
    guild_ids=GUILDS,
)
async def watch_cmd(ctx, min_annualised: float = 25.0, min_profit: float = 2.0):
    # La fenêtre d'interaction Discord est de 3 s et la boucle de scan peut
    # occuper l'event loop : sans defer, l'interaction meurt en 404 (10062).
    await ctx.defer(ephemeral=True)
    db.execute(
        "INSERT OR REPLACE INTO subs VALUES(?,?,?,?,?)",
        (
            ctx.channel.id,
            ctx.guild.id if ctx.guild else 0,
            min_annualised,
            min_profit,
            int(time.time()),
        ),
    )
    db.commit()
    await ctx.respond(
        f"✅ Watching. Alerts land here when annualised return ≥ **{min_annualised:g}%** "
        f"and locked-in profit ≥ **${min_profit:g}**.\n"
        f"Checked every {POLL_MINUTES} min · run `/guide` to post the how-to-read note.",
        ephemeral=True,
    )


@bot.slash_command(
    name="unwatch", description="Stop alerts in this channel", guild_ids=GUILDS
)
async def unwatch_cmd(ctx):
    await ctx.defer(ephemeral=True)
    db.execute("DELETE FROM subs WHERE channel_id=?", (ctx.channel.id,))
    db.commit()
    await ctx.respond("🔕 Stopped watching this channel.", ephemeral=True)


@bot.slash_command(
    name="status", description="Scanner settings and last run", guild_ids=GUILDS
)
async def status_cmd(ctx):
    await ctx.defer(ephemeral=True)
    last = int(meta_get("last_scan", 0) or 0)
    row = db.execute(
        "SELECT min_apy, min_profit FROM subs WHERE channel_id=?", (ctx.channel.id,)
    ).fetchone()

    e = discord.Embed(title="Coherence scanner", color=0x34495E)
    e.add_field(
        name="This channel",
        value=(
            f"watching · APY ≥ {row[0]:g}% · profit ≥ ${row[1]:g}"
            if row
            else "not watching · run `/watch`"
        ),
        inline=False,
    )
    e.add_field(
        name="Last scan",
        value=(f"<t:{last}:R> · {meta_get('last_count', 0)} found" if last else "never"),
        inline=True,
    )
    e.add_field(name="Interval", value=f"{POLL_MINUTES} min", inline=True)
    e.add_field(
        name="Detection thresholds",
        value=(
            f"edge ≥ {C.MIN_EDGE_CENTS:g}¢/share · size ≥ {C.MIN_UNITS:g} shares\n"
            f"annualised ≥ {C.MIN_APY_PCT:g}% · profit ≥ ${C.MIN_PROFIT_USD:g}\n"
            f"fees assumed {C.FEE_BPS:g} bps · top {C.MAX_EVENTS} events by 24h volume"
        ),
        inline=False,
    )
    await ctx.respond(embed=e, ephemeral=True)


@bot.slash_command(
    name="guide", description="Post the how-to-read note in this channel (pin it)",
    guild_ids=GUILDS,
)
async def guide_cmd(ctx):
    await ctx.defer()
    await ctx.respond(
        embed=discord.Embed(
            title="📖 How to read this channel", description=GUIDE, color=0x34495E
        )
    )


# ---------------------------------------------------------------------------


@bot.event
async def on_ready():
    print(f"Connected as {bot.user}", flush=True)
    try:
        # Purger les globales AVANT de synchroniser par serveur : sinon les deux
        # jeux cohabitent et Discord affiche chaque commande EN DOUBLE dans le
        # menu. Sans effet sur une app neuve, indispensable après coup.
        await bot.http.bulk_upsert_global_commands(bot.application_id, [])
        # Sync par serveur : effet immédiat, au lieu d'environ une heure en global.
        await bot.sync_commands(
            guild_ids=[g.id for g in bot.guilds], force=True
        )
        print(f"Commands synced on {len(bot.guilds)} guild(s)", flush=True)
    except discord.DiscordException as e:
        print(f"Command sync failed: {e}", flush=True)

    if not poll.is_running():
        poll.start()


def main():
    if not TOKEN:
        sys.exit(
            "No Discord token. Put it in token.txt next to this script, "
            "or set DISCORD_BOT_TOKEN."
        )
    acquire_single_instance_lock()
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
