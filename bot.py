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
import re
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

# Permissions demandées à l'invitation : voir/écrire/embeds, plus créer les salons
# (`/setup`) et épingler (`manage_messages`). Rien de plus — le bot n'a aucune
# raison de toucher aux membres ni aux rôles.
INVITE_PERMS = 1024 | 2048 | 16384 | 16 | 8192

DEFAULT_MIN_APY = 25.0
DEFAULT_MIN_PROFIT = 2.0

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
# Un seul message réécrit en place par salon : le tableau montre l'état courant
# au lieu d'un fil qui défile. Idem pour le guide, sinon `/guide` deux fois de
# suite laisse deux copies dans le salon.
db.execute(
    """CREATE TABLE IF NOT EXISTS board(
  channel_id INTEGER PRIMARY KEY, message_id INTEGER, updated INTEGER)"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS guides(
  channel_id INTEGER PRIMARY KEY, message_id INTEGER, updated INTEGER)"""
)
# Journal des alertes : table qu'on n'écrase JAMAIS, contrairement à `seen` qui
# n'est qu'un antidoublon. Sans ce journal, impossible de savoir après coup ce
# que le bot a réellement annoncé — le bot overlap en a fait la démonstration :
# sa table `seen` est une photo réécrite à chaque cycle, donc son historique
# d'alertes est définitivement perdu.
#
# verdict : NULL tant que non jugé · "win"  = arithmétique tenue, l'arb payait
#           "void" = un marché annulé a cassé la garantie
#           "partial" = tous les marchés du groupe ne sont pas encore résolus
db.execute(
    """CREATE TABLE IF NOT EXISTS journal(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  key      TEXT,
  kind     TEXT,
  slug     TEXT,
  title    TEXT,
  profit   REAL,
  capital  REAL,
  units    REAL,
  apy      REAL,
  days     REAL,
  ts       INTEGER,
  verdict  TEXT,
  scored   INTEGER)"""
)
db.execute("CREATE INDEX IF NOT EXISTS idx_journal_verdict ON journal(verdict, ts)")
db.execute(
    """CREATE TABLE IF NOT EXISTS trackboard(
  channel_id INTEGER PRIMARY KEY, message_id INTEGER, updated INTEGER)"""
)
# Salons retenus par IDENTIFIANT, pas par nom : un identifiant survit aux
# renommages, un nom non. Sans ça, ajouter un emoji au nom d'un salon fait que
# `/setup` ne le reconnaît plus et en recrée un doublon à côté.
db.execute(
    """CREATE TABLE IF NOT EXISTS channels(
  guild_id INTEGER, key TEXT, channel_id INTEGER,
  PRIMARY KEY(guild_id, key))"""
)
db.commit()


def _norm(name: str) -> str:
    """Nom comparable : emojis, majuscules et ponctuation retirés."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


async def ensure_channel(guild, cat, key, display, topic, overwrites):
    """Retrouve un salon par ID mémorisé, puis par nom normalisé, sinon le crée.

    Trois niveaux de repli, du plus robuste au plus fragile, pour que l'on
    puisse renommer les salons librement sans que `/setup` fasse des doublons.
    """
    row = db.execute(
        "SELECT channel_id FROM channels WHERE guild_id=? AND key=?", (guild.id, key)
    ).fetchone()
    if row:
        ch = guild.get_channel(row[0])
        if ch is not None:
            return ch, False

    target = _norm(key)
    for ch in cat.text_channels:
        if _norm(ch.name) == target:
            db.execute(
                "INSERT OR REPLACE INTO channels VALUES(?,?,?)", (guild.id, key, ch.id)
            )
            db.commit()
            return ch, False

    ch = await guild.create_text_channel(
        display, category=cat, topic=topic, overwrites=overwrites
    )
    db.execute("INSERT OR REPLACE INTO channels VALUES(?,?,?)", (guild.id, key, ch.id))
    db.commit()
    return ch, True


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

    # Le bloc d'ordres est l'information principale : sans prix limite ni taille,
    # une alerte dit qu'un arb existe sans dire comment le prendre — et payer un
    # tick de trop suffit à le faire disparaître.
    # Toutes les jambes vivent dans le MÊME événement Polymarket : un seul lien
    # suffit. On le construit D'ABORD et on lui réserve sa place : ajouté en
    # dernier puis tronqué à 1024, un slug un peu long le coupait en deux et
    # l'alerte perdait le seul moyen d'aller passer les ordres.
    link = f"\n🔗 **[Open the event on Polymarket]({o.url})** — all legs are here"
    budget = 1024 - len(link)

    lines, shown = [], 0
    for i, ord_ in enumerate(o.orders, 1):
        row = (
            f"`{i}.` **BUY {ord_.side}** «{ord_.label[:26]}»\n"
            f"　　 max price `{ord_.limit:.3f}` · `{ord_.shares:,.0f}` shares · "
            f"{C.fmt_usd(ord_.cost)}"
        )
        rest = len(o.orders) - i
        tail = f"\n`…` +{rest} more legs, all required" if rest else ""
        if sum(len(x) + 1 for x in lines) + len(row) + len(tail) > budget:
            break
        lines.append(row)
        shown = i

    if shown < len(o.orders):
        lines.append(f"`…` +{len(o.orders) - shown} more legs, all required")

    e.add_field(
        name="📋 Orders to place",
        value=("\n".join(lines) + link)[:1024],
        inline=False,
    )

    payout = o.capital + o.profit
    e.add_field(
        name="💰 What you get",
        value=(
            # Montants exacts, pas arrondis : « $1.2K → $1.2K » masquerait
            # précisément le gain, qui est toute l'information de la ligne.
            f"Pay **${o.capital:,.2f}** now → receive **${payout:,.2f}** "
            f"at resolution, whatever happens.\n"
            f"Locked-in profit **${o.profit:,.2f}** · {o.roi:.2f}% over "
            f"{horizon_label(o.days)} · **{o.apy:,.0f}%** annualised"
        ),
        inline=False,
    )

    e.add_field(
        name="⚠️ All legs or none",
        value=(
            "Never pay above the max price — one tick more and the edge is gone. "
            "If one leg fills and another does not, unwind immediately: a single "
            "leg is a directional bet, which is exactly what this avoids."
        ),
        inline=False,
    )

    e.set_footer(
        text="Prices walked from the live order book, never mid. "
        "The book moves in seconds — re-check before sending."
    )
    return e


# Tranches de gain. La médiane des alertes est de 5 $ : sans découpage, le
# tableau serait écrasé par quelques arbs géants qui font 78 % du total mais
# exigent un capital hors de portée.
TRACK_BUCKETS = [
    ("under $5", 0, 5),
    ("$5–20", 5, 20),
    ("$20–100", 20, 100),
    ("$100–1k", 100, 1000),
    ("over $1k", 1000, float("inf")),
]


async def score_journal(limit: int = 60) -> int:
    """Juge les alertes en attente dont l'événement s'est dénoué.

    On ne rejuge jamais une alerte déjà tranchée en "win" ou "void" : ces
    verdicts sont définitifs. Seuls "partial", "open" et les non jugées sont
    repris à chaque passage.
    """
    pending = db.execute(
        "SELECT DISTINCT slug FROM journal "
        "WHERE slug<>'' AND (verdict IS NULL OR verdict IN ('open','partial')) "
        "ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not pending:
        return 0

    verdicts = await C.score_events([r[0] for r in pending])
    now = int(time.time())
    n = 0
    for slug, verdict in verdicts.items():
        cur = db.execute(
            "UPDATE journal SET verdict=?, scored=? "
            "WHERE slug=? AND (verdict IS NULL OR verdict IN ('open','partial'))",
            (verdict, now, slug),
        )
        n += cur.rowcount
    db.commit()
    return n


def track_record() -> dict:
    """Bilan par tranche de gain : alertes, jugeables, réussies, taux, cumul."""
    rows = db.execute(
        "SELECT profit, verdict FROM journal WHERE profit IS NOT NULL"
    ).fetchall()
    out = {"buckets": [], "total": dict(alerts=0, judged=0, wins=0, pending=0, gain=0.0)}
    for label, lo, hi in TRACK_BUCKETS:
        b = [(p, v) for p, v in rows if lo <= (p or 0) < hi]
        if not b:
            continue
        wins = sum(1 for _, v in b if v == "win")
        voids = sum(1 for _, v in b if v == "void")
        judged = wins + voids
        gain = sum(p for p, _ in b)
        won_gain = sum(p for p, v in b if v == "win")
        out["buckets"].append(dict(
            label=label, alerts=len(b), judged=judged, wins=wins, voids=voids,
            pending=len(b) - judged, gain=gain, won_gain=won_gain,
        ))
        t = out["total"]
        t["alerts"] += len(b); t["judged"] += judged; t["wins"] += wins
        t["pending"] += len(b) - judged; t["gain"] += gain
    return out


def track_embed() -> discord.Embed:
    """Bilan public du bot : ce qu'il a annoncé, et ce que ça a donné.

    Un bot qui affiche ses alertes sans jamais dire si elles tenaient demande
    qu'on lui fasse confiance sur parole. Ce tableau répond à la seule question
    qui compte pour un visiteur : est-ce que ça marche vraiment ?
    """
    tr = track_record()
    t = tr["total"]
    rate = (100 * t["wins"] / t["judged"]) if t["judged"] else 0.0

    first = db.execute("SELECT MIN(ts) FROM journal").fetchone()[0]
    days = ((time.time() - first) / 86400) if first else 0.0

    e = discord.Embed(
        title="📒 Track record — every alert, scored",
        description=(
            f"**{t['alerts']:,}** alerts logged over **{days:.1f} days**. "
            f"**{t['judged']:,}** have fully resolved.\n"
            f"A coherence arb pays **by construction** — the only way it breaks "
            f"is a market being **voided**, paying neither side. That is what "
            f"this scores."
        ),
        color=0x2ECC71 if rate >= 99 and t["judged"] else 0x34495E,
    )

    if t["judged"]:
        e.add_field(
            name="Verdict",
            value=f"**{t['wins']:,} / {t['judged']:,}** held up — **{rate:.1f}%**",
            inline=False,
        )

    if not tr["buckets"]:
        e.add_field(
            name="No alerts logged yet",
            value="The journal starts filling on the next alert. Nothing is lost "
                  "from here on — every alert is written down and scored when its "
                  "markets settle.",
            inline=False,
        )
        e.set_footer(text=f"Rewritten every {POLL_MINUTES} min")
        return e

    lines = [f"`{'bucket':<10}{'alerts':>7}{'judged':>8}{'held':>6}{'rate':>7}`"]
    for b in tr["buckets"]:
        r = f"{100*b['wins']/b['judged']:.0f}%" if b["judged"] else "—"
        lines.append(
            f"`{b['label']:<10}{b['alerts']:>7}{b['judged']:>8}{b['wins']:>6}{r:>7}`"
        )
    e.add_field(name="By size", value="\n".join(lines)[:1024], inline=False)

    gl = [f"`{b['label']:<10}` {C.fmt_usd(b['won_gain']):>9} settled "
          f"· {C.fmt_usd(b['gain'])} incl. open" for b in tr["buckets"]]
    e.add_field(name="Locked-in gain (theoretical)", value="\n".join(gl)[:1024], inline=False)

    e.add_field(
        name="Read this before the numbers",
        value=(
            "Nothing here was traded — these are the gains the alerts described, "
            "not money made. A handful of huge multi-leg arbs dominate the totals "
            "and need capital most people don't have; the **median alert is worth "
            "a few dollars**. The rate above says the maths held, not that the bot "
            "predicted anything."
        ),
        inline=False,
    )
    e.set_footer(text=f"Rewritten every {POLL_MINUTES} min · pending alerts are re-checked as markets settle")
    e.timestamp = discord.utils.utcnow()
    return e


def board_embed(result: C.ScanResult) -> discord.Embed:
    """Tableau réécrit en place à chaque cycle.

    Il n'affiche pas QUE les opportunités : elles sont rares, et un tableau vide
    en permanence ne dit pas si le scanner tourne encore. On montre donc l'état de
    cohérence du marché — les contraintes les plus serrées — avec, à chaque fois,
    la taille réellement exécutable. Un écart de 7 cents adossé à 0 part est un
    mirage, et l'afficher sans sa taille en ferait une promesse mensongère.
    """
    live = bool(result.opportunities)
    e = discord.Embed(
        title="🧭 Polymarket coherence — live",
        description=(
            f"**{len(result.opportunities)} executable** "
            f"· {result.constraints:,} constraints checked "
            f"· {result.markets:,} markets in {result.duration:.1f}s"
        ),
        color=0x2ECC71 if live else 0x34495E,
    )

    if live:
        for o in result.opportunities[:3]:
            icon = KIND_STYLE.get(o.kind, ("⚪",))[0]
            e.add_field(
                name=f"{icon} {o.title[:80]}",
                value=(
                    f"{o.detail[:140]}\n"
                    f"**{C.fmt_usd(o.profit)}** locked in · {o.units:,.0f} shares · "
                    f"{C.fmt_usd(o.capital)} capital · **{o.apy:,.0f}%** annualised "
                    f"· {horizon_label(o.days)}\n[Open market]({o.url})"
                ),
                inline=False,
            )

    lines = []
    for m in result.tightest[:6]:
        mark = "🟢" if m.cents > 0 and m.units >= C.MIN_UNITS else "▫️"
        size = f"×{m.units:,.0f}" if m.units >= 1 else "×0"
        lines.append(f"{mark} `{m.cents:+5.2f}¢ {size:>8}` {m.title[:44]}")
    if lines:
        e.add_field(
            name="Tightest constraints" if live else "Closest to breaking",
            value="\n".join(lines)[:1024],
            inline=False,
        )

    if not live:
        e.add_field(
            name="Nothing to trade — and that is the normal state",
            value=(
                "`¢` is the gap at the top of the book, `×` how many shares can "
                "actually be filled at a positive margin. A wide gap with no size "
                "behind it is a mirage, which is why both are always shown.\n"
                "🟢 = real depth behind the gap, but still **below the alert "
                "thresholds** (edge per share, profit, or annualised return).\n"
                "▫️ = no usable size — ignore it."
            ),
            inline=False,
        )

    e.set_footer(text=f"Rewritten every {POLL_MINUTES} min · never uses mid prices")
    e.timestamp = discord.utils.utcnow()
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
    "**How to act on an alert**\n"
    "Every alert lists the exact orders to place: which side, how many shares, "
    "and a **max price**. Place them as *limit* orders at that price — never "
    "higher. The edge is a few cents per share, so paying one tick more can "
    "erase it entirely.\n"
    "Fill **every leg or none**. If one fills and another does not, unwind at "
    "once: a single leg is a directional bet, which is the exact thing this "
    "avoids.\n"
    "Sizes come from walking the real order book, not the top-of-book quote. "
    "`Annualised` matters more than `Return`: 0.3% that settles tomorrow beats "
    "5% that settles next year.\n\n"
    "**What the bot does not tell you**\n"
    "• You must fill **every leg**. Polymarket has no all-or-nothing execution — "
    "if one leg fills and another doesn't, you are left with a directional bet.\n"
    "• The book moves in seconds. Treat every number as of the moment it was sent.\n"
    "• Fees are assumed to be zero. Check the current fee schedule before sizing up.\n"
    "• Silence is the normal state. A coherent market offers nothing."
)


def build_guide_embed() -> discord.Embed:
    return discord.Embed(
        title="📖 How to read this channel", description=GUIDE, color=0x34495E
    )


async def upsert_pinned(channel, table: str, embed: discord.Embed) -> discord.Message:
    """Réécrit le message épinglé du salon, ou le crée s'il n'existe pas (encore).

    Sans réécriture, relancer `/board` ou `/guide` empile les copies et le salon
    finit avec trois tableaux dont deux sont périmés.
    """
    row = db.execute(
        f"SELECT message_id FROM {table} WHERE channel_id=?", (channel.id,)
    ).fetchone()
    if row:
        try:
            msg = await channel.fetch_message(row[0])
            await msg.edit(embed=embed)
            db.execute(
                f"UPDATE {table} SET updated=? WHERE channel_id=?",
                (int(time.time()), channel.id),
            )
            db.commit()
            return msg
        except discord.NotFound:
            pass  # message supprimé à la main → on le recrée

    msg = await channel.send(embed=embed)
    try:
        await msg.pin()
    except discord.DiscordException:
        pass  # pas la permission d'épingler : le message reste utile, on continue
    db.execute(
        f"INSERT OR REPLACE INTO {table} VALUES(?,?,?)",
        (channel.id, msg.id, int(time.time())),
    )
    db.commit()
    return msg


async def install_pinned(ctx, table: str, embed: discord.Embed, ok: str) -> bool:
    """Pose un message épinglé et répond TOUJOURS à l'interaction.

    Sans ce garde-fou, un salon où le bot n'a pas le droit d'écrire fait
    remonter une `Forbidden` : l'interaction déjà différée n'est jamais
    répondue et Discord affiche « réfléchit… » indéfiniment, sans que
    l'utilisateur puisse deviner ce qui manque.
    """
    try:
        await upsert_pinned(ctx.channel, table, embed)
    except discord.Forbidden:
        await ctx.respond(
            "❌ I can't post in this channel.\n"
            "Give my role **Send Messages** and **Embed Links** here — plus "
            "**Manage Messages** if you want the message pinned — then run the "
            "command again.",
            ephemeral=True,
        )
        return False
    except discord.DiscordException as e:
        await ctx.respond(f"❌ Discord refused: {e}", ephemeral=True)
        return False
    await ctx.respond(ok, ephemeral=True)
    return True


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
    # Journal permanent, en plus de l'antidoublon. On enregistre au moment de
    # l'alerte : les prix et la taille exécutable ne sont plus reconstituables
    # après coup, et c'est précisément ce qu'on voudra juger plus tard.
    db.execute(
        "INSERT INTO journal(key,kind,slug,title,profit,capital,units,apy,days,ts) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (o.key, o.kind, o.slug, o.title, o.profit, o.capital, o.units,
         o.apy, o.days, int(time.time())),
    )
    db.commit()


@tasks.loop(minutes=POLL_MINUTES)
async def poll():
    subs = db.execute("SELECT channel_id, min_apy, min_profit FROM subs").fetchall()
    boards = db.execute("SELECT channel_id FROM board").fetchall()
    if not subs and not boards:
        return

    try:
        result = await get_scan(force=True)
    except Exception as e:  # noqa: BLE001
        print(f"[poll] scan failed: {type(e).__name__}: {e}", flush=True)
        return

    # Juger les alertes en attente dont les marchés se sont dénoués. Isolé dans
    # son propre try : une panne du scoreur ne doit pas empêcher les alertes.
    try:
        n = await score_journal()
        if n:
            print(f"[journal] {n} alerte(s) jugée(s)", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[journal] scoring failed: {type(e).__name__}: {e}", flush=True)

    for (channel_id,) in db.execute("SELECT channel_id FROM trackboard").fetchall():
        ch = bot.get_channel(channel_id)
        if ch is None:
            continue
        try:
            await upsert_pinned(ch, "trackboard", track_embed())
        except discord.DiscordException as e:
            print(f"[poll] trackboard failed on {channel_id}: {e}", flush=True)

    # Tableaux d'abord : ils doivent rester à jour même si aucun salon n'est abonné.
    for (channel_id,) in boards:
        ch = bot.get_channel(channel_id)
        if ch is None:
            continue
        try:
            await upsert_pinned(ch, "board", board_embed(result))
        except discord.DiscordException as e:
            print(f"[poll] board update failed on {channel_id}: {e}", flush=True)

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
@discord.option("limit", int, description="How many to show (1-8)",
                min_value=1, max_value=8, default=5, required=False)
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
@discord.option("min_annualised", float, description="Minimum annualised return (%)",
                min_value=0, default=25.0, required=False)
@discord.option("min_profit", float, description="Minimum locked-in profit ($)",
                min_value=0, default=2.0, required=False)
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
    await ctx.defer(ephemeral=True)
    await install_pinned(
        ctx, "guides", build_guide_embed(),
        "📖 Guide posted and pinned. Running `/guide` again updates that same "
        "message instead of adding another one.",
    )


@bot.slash_command(
    name="board",
    description="Install the live coherence board in this channel",
    guild_ids=GUILDS,
)
async def board_cmd(ctx):
    await ctx.defer(ephemeral=True)
    result = await get_scan()
    await install_pinned(
        ctx, "board", board_embed(result),
        f"🧭 Board installed and pinned. It is **rewritten in place every "
        f"{POLL_MINUTES} min**, so this channel always shows the current state — "
        "no feed to scroll through.\nRun `/guide` to pin the how-to-read note too.",
    )


@bot.slash_command(
    name="preview",
    description="Post a sample alert here to check formatting and permissions",
    guild_ids=GUILDS,
)
async def preview_cmd(ctx):
    await ctx.defer(ephemeral=True)

    # Chiffres inventés, mais réalistes : c'est la seule façon de vérifier le rendu
    # et les permissions d'écriture tant que le marché reste cohérent.
    sample = C.Opportunity(
        kind="ladder_strikes",
        title="Ethereum above ___ on August 20?  (SAMPLE)",
        slug="",
        detail=(
            "« 1,900 » implique « 1,800 », or le premier s'achète moins cher : "
            "bid 0.845 > ask 0.830. Monotonie violée."
        ),
        legs=["YES 1,800 @ 0.830", "NO 1,900 @ 0.155"],
        units=1200,
        capital=1182.0,
        profit=18.0,
        days=6.5,
    )

    e = opp_embed(sample)
    # Marquage très visible : une alerte fabriquée qui traîne dans un salon
    # d'alertes doit être impossible à confondre avec une vraie.
    e.title = "🧪 EXAMPLE ALERT — not a real opportunity"
    e.url = None
    e.color = 0x95A5A6
    e.set_footer(
        text="Sample posted by /preview to test formatting and permissions. "
        "These numbers are made up — do not trade this."
    )

    try:
        await ctx.channel.send(embed=e)
    except discord.Forbidden:
        return await ctx.respond(
            "I can't post in this channel. Give my role **Send Messages** and "
            "**Embed Links** here, then run `/preview` again.",
            ephemeral=True,
        )

    await ctx.respond(
        "🧪 Sample alert posted — this is exactly how a real one will look.\n"
        "Delete it whenever you like; it is not stored and never repeats.",
        ephemeral=True,
    )


@bot.slash_command(
    name="track-board",
    description="Install the live track record board in this channel",
    guild_ids=GUILDS,
)
async def track_board_cmd(ctx):
    await ctx.defer(ephemeral=True)
    await install_pinned(
        ctx, "trackboard", track_embed(),
        f"📒 Track record installed and pinned, rewritten every {POLL_MINUTES} min.\n"
        "Every alert is logged and scored once its markets settle — including the "
        "ones that fail.",
    )


@bot.slash_command(
    name="setup",
    description="Create the full channel structure and wire everything up",
    guild_ids=GUILDS,
)
@discord.default_permissions(manage_guild=True)
async def setup_cmd(ctx):
    await ctx.defer(ephemeral=True)
    g = ctx.guild
    if g is None:
        return await ctx.respond("Run this in a server, not in a DM.", ephemeral=True)

    if not g.me.guild_permissions.manage_channels:
        return await ctx.respond(
            "I need the **Manage Channels** permission to build the structure.\n"
            "Grant it to my role in Server Settings → Roles, or re-invite me with:\n"
            f"https://discord.com/oauth2/authorize?client_id={bot.user.id}"
            f"&permissions={INVITE_PERMS}&scope=bot%20applications.commands",
            ephemeral=True,
        )

    # Salons en lecture seule : tout le monde lit, seul le bot écrit. Un flux
    # d'alertes où n'importe qui peut poster devient illisible en deux jours.
    read_only = {
        g.default_role: discord.PermissionOverwrite(
            send_messages=False, add_reactions=True
        ),
        g.me: discord.PermissionOverwrite(send_messages=True, manage_messages=True),
    }

    # Noms préfixés : ce bot cohabite avec le bot overlap, dont le `/setup` crée
    # déjà « how-it-works » et « discussion ». Des noms génériques feraient que
    # chaque bot croit reconnaître les salons de l'autre.
    # (clé logique, nom affiché à la création, sujet, lecture seule)
    # La clé ne change jamais : c'est elle qui identifie le salon en base. Le nom
    # affiché est libre — tu peux le renommer sans rien casser.
    plan = [
        ("coherence-guide", "📖coherence-guide",
         "Read this first — what an arbitrage alert here means", True),
        ("coherence-board", "🧭coherence-board",
         "Live state of the market, rewritten automatically", True),
        ("arb-alerts", "🚨arb-alerts",
         "Executable inconsistencies, the moment they appear", True),
        ("arb-discussion", "💬arb-discussion",
         "Talk about the calls here — open to everyone", False),
    ]

    cat = discord.utils.get(g.categories, name="POLYMARKET COHERENCE")
    if cat is None:
        cat = await g.create_category("POLYMARKET COHERENCE")

    made, reused, chans = [], [], {}
    for key, display, topic, locked in plan:
        # Recherche confinée à NOTRE catégorie, jamais tout le serveur : une
        # recherche globale retrouverait les salons d'un autre bot Polymarket.
        try:
            ch, created = await ensure_channel(
                g, cat, key, display, topic,
                # py-cord exige un dict : `None` lève InvalidArgument et fait
                # échouer toute la commande sur le premier salon ouvert.
                read_only if locked else {},
            )
        except discord.HTTPException as e:
            return await ctx.respond(
                f"Could not create **{display}**: {e}\n"
                "Channels created before this point were kept — fix the issue "
                "and run `/setup` again, it reuses what already exists.",
                ephemeral=True,
            )
        (made if created else reused).append(ch)
        chans[key] = ch

    await upsert_pinned(chans["coherence-guide"], "guides", build_guide_embed())

    result = await get_scan()
    await upsert_pinned(chans["coherence-board"], "board", board_embed(result))

    db.execute(
        "INSERT OR REPLACE INTO subs VALUES(?,?,?,?,?)",
        (
            chans["arb-alerts"].id, g.id,
            DEFAULT_MIN_APY, DEFAULT_MIN_PROFIT, int(time.time()),
        ),
    )
    db.commit()

    lines = [
        "**Setup complete.**",
        f"📖 {chans['coherence-guide'].mention} — guide posted and pinned",
        f"🧭 {chans['coherence-board'].mention} — live board, rewritten every {POLL_MINUTES} min",
        f"🚨 {chans['arb-alerts'].mention} — alerts above {DEFAULT_MIN_APY:g}% annualised "
        f"and ${DEFAULT_MIN_PROFIT:g} profit",
        f"💬 {chans['arb-discussion'].mention} — open to everyone",
    ]
    if made:
        lines.append(f"\nCreated: {', '.join(c.mention for c in made)}")
    if reused:
        lines.append(f"Reused existing: {', '.join(c.mention for c in reused)}")
    lines.append(
        "\nAlert channels are read-only for members (reactions still allowed). "
        "Change the thresholds anytime with `/watch` in that channel.\n"
        "Expect long silences: a coherent market produces nothing, and that is "
        "the scanner working, not failing."
    )
    await ctx.respond("\n".join(lines), ephemeral=True)


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
