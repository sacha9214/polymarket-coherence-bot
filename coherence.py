"""
Moteur Coherence — détection d'incohérences logiques entre marchés Polymarket.

Principe : certains marchés sont liés par des contraintes mathématiques strictes.
Quand les prix violent une contrainte, il existe un portefeuille dont le gain est
positif QUEL QUE SOIT le résultat de l'événement. Aucune prédiction n'est requise.

Trois familles de contraintes, toutes observées sur des marchés réels :

  1. BUCKETS (negRisk)   « Bitcoin price on August 14? » → <56k / 56-58k / 58-60k…
                         Issues mutuellement exclusives et exhaustives : Σ P = 1.

  2. ÉCHELLE DE STRIKES  « Bitcoin above ___ on August 14? » → 54,000 / 56,000…
                         P(> 54k) ≥ P(> 56k). Inclut les échelles « dip to X »,
                         dont la monotonie est inversée.

  3. ÉCHELLE DE DATES    « When will Bitcoin hit $150k? » → by Sept 30 / by Dec 31
                         P(avant décembre) ≥ P(avant septembre).

POINT CRITIQUE : le moteur ne regarde JAMAIS les prix affichés (`outcomePrices`,
qui sont des mid). Une inversion sur les mid n'est pas une opportunité — elle
disparaît dès qu'on regarde le spread. Tout est calculé sur le carnet d'ordres
réel, en marchant les niveaux, ce qui donne le gain net à la taille réellement
exécutable. C'est la différence entre un jouet et un outil.

Testable seul, sans Discord :  python3 coherence.py
"""

from __future__ import annotations

import asyncio
import datetime
import json
import math
import re
from dataclasses import dataclass, field

import aiohttp

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "polymarket-coherence-bot/1.0"}

# ---------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------

# Nombre d'événements scannés (triés par volume 24h décroissant).
MAX_EVENTS = 400
# Un événement sans liquidité produit des « opportunités » infranchissables.
MIN_EVENT_LIQUIDITY = 5_000.0
# Marge minimale par part, en cents, APRÈS frais. En dessous, ce n'est que du bruit
# de tick : le carnet bouge plus vite que ta capacité à envoyer les deux jambes.
MIN_EDGE_CENTS = 1.0
# Polymarket impose une taille minimale d'ordre (orderMinSize, typiquement 5).
MIN_UNITS = 5.0
# Plafond de parts par opportunité : au-delà, on sur-estime toujours l'exécutable.
MAX_UNITS = 100_000.0
# Frais taker en points de base. Historiquement 0 sur Polymarket, mais des frais
# ont été introduits sur certains marchés : À VÉRIFIER avant de trader en réel.
FEE_BPS = 0.0
# Au-delà, immobiliser le capital sur N jambes n'a aucun sens (128 candidats à une
# présidentielle : 127 $ de capital pour 1 $ de gain).
MAX_LEGS = 15
# Rendement minimal sur capital immobilisé, en %. Plancher de dignité uniquement :
# le vrai filtre est l'APY, parce qu'un arb n'est pas jugeable sans son horizon.
MIN_ROI_PCT = 0.05
# Rendement annualisé minimal, en %. C'est LE critère de décision : 0.2 % sur un
# marché qui résout demain vaut bien mieux que 5 % sur un marché qui résout dans
# un an, puisque le capital se recycle. Trier sur le ROI brut classe à l'envers.
MIN_APY_PCT = 15.0
# Gain absolu minimal, en dollars : en dessous, les frais de gas Polygon et le
# temps passé mangent l'opération quel que soit l'APY affiché.
MIN_PROFIT_USD = 1.0
# Requêtes de carnets par lot (endpoint POST /books).
BOOK_CHUNK = 100


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------


def fmt_usd(v: float) -> str:
    if abs(v) >= 1e6:
        return f"${v/1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:.2f}"


def _jloads(value, default):
    """Gamma renvoie certains champs en JSON encodé dans une chaîne."""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def days_until(iso: str | None, default=30.0) -> float:
    """Jours avant résolution. Sert à annualiser : sans horizon, un rendement ne
    veut rien dire. En cas de date absente ou illisible, on prend une valeur
    prudente (pénalise l'APY) plutôt qu'optimiste."""
    if not iso:
        return default
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return default
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    delta = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 86400
    return max(delta, 0.5)


# ---------------------------------------------------------------------------
# Carnet d'ordres
# ---------------------------------------------------------------------------


@dataclass
class Book:
    """Carnet d'un token, niveaux triés du meilleur au pire.

    L'API renvoie les bids en ordre croissant et les asks en ordre décroissant
    (le meilleur est en DERNIER dans les deux cas). On retrie explicitement
    plutôt que de dépendre de cet ordre non documenté.
    """

    bids: list[tuple[float, float]] = field(default_factory=list)  # prix décroissant
    asks: list[tuple[float, float]] = field(default_factory=list)  # prix croissant

    @classmethod
    def from_api(cls, raw: dict) -> "Book":
        def levels(key, reverse):
            out = []
            for lvl in raw.get(key) or []:
                p, s = _num(lvl.get("price"), -1), _num(lvl.get("size"))
                if 0 < p < 1 and s > 0:
                    out.append((p, s))
            out.sort(key=lambda x: x[0], reverse=reverse)
            return out

        return cls(bids=levels("bids", True), asks=levels("asks", False))

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 1.0

    def no_asks(self) -> list[tuple[float, float]]:
        """Les asks du token NO sont le miroir exact des bids du token YES.

        Sur le CLOB Polymarket, un ordre d'achat YES à 0.60 EST un ordre de vente
        NO à 0.40 — c'est le même carnet vu des deux côtés. On dérive donc au lieu
        de requêter, ce qui divise par deux le nombre d'appels réseau.
        """
        return [(1.0 - p, s) for p, s in self.bids]


@dataclass
class Fill:
    """Ce qu'une jambe coûte réellement à la taille exécutable.

    `worst_price` est le prix limite à poser : c'est le pire niveau consommé,
    donc celui qui reproduit exactement l'opération calculée. Sans lui, une
    alerte n'est pas actionnable — on sait qu'il y a un arb, mais pas à quel
    prix on cesse de le gagner.
    """

    cost: float = 0.0
    worst_price: float = 0.0

    def avg(self, units: float) -> float:
        return self.cost / units if units else 0.0


@dataclass
class WalkResult:
    units: float
    cost: float
    fills: list[Fill] = field(default_factory=list)

    def __iter__(self):
        """Reste dépaquetable en (units, cost) pour tous les appels existants."""
        return iter((self.units, self.cost))


def walk(
    legs: list[list[tuple[float, float]]],
    payoff: float,
    min_edge: float,
    fee_rate: float,
) -> WalkResult:
    """Marche les carnets en achetant 1 part de chaque jambe par unité.

    Le coût unitaire ne peut que se dégrader en descendant les niveaux : prendre
    glouton tant que c'est rentable donne donc l'optimum exact, pas une heuristique.

    Retourne les unités exécutables, le coût total frais inclus, et le détail
    par jambe (coût et pire prix touché) nécessaire pour dicter les ordres.
    """
    n = len(legs)
    if not legs or any(not lv for lv in legs):
        return WalkResult(0.0, 0.0, [Fill() for _ in range(n)])

    idx = [0] * n
    rem = [lv[0][1] for lv in legs]
    fills = [Fill() for _ in range(n)]
    units = 0.0
    cost = 0.0

    while True:
        if any(idx[j] >= len(legs[j]) for j in range(n)):
            break
        unit = sum(legs[j][idx[j]][0] for j in range(n))
        unit *= 1.0 + fee_rate
        if unit >= payoff - min_edge:
            break

        qty = min(min(rem), MAX_UNITS - units)
        if qty <= 0:
            break

        units += qty
        cost += qty * unit

        for j in range(n):
            price = legs[j][idx[j]][0]
            fills[j].cost += qty * price * (1.0 + fee_rate)
            fills[j].worst_price = price
            rem[j] -= qty
            if rem[j] <= 1e-9:
                idx[j] += 1
                rem[j] = legs[j][idx[j]][1] if idx[j] < len(legs[j]) else 0.0

    return WalkResult(units, cost, fills)


# ---------------------------------------------------------------------------
# Modèle
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    market_id: str
    question: str
    label: str
    yes_token: str
    end_date: str
    liquidity: float
    key: float  # strike ou timestamp, tel que parsé — sans interprétation
    mid: float  # prix affiché, utilisé UNIQUEMENT pour déduire l'orientation
    rank: float = 0.0  # plus haut = événement plus large, donc plus probable
    book: Book | None = None


@dataclass
class Family:
    event_id: str
    title: str
    slug: str
    kind: str  # "buckets" | "strikes" | "dates"
    outcomes: list[Outcome]


@dataclass
class Order:
    """Un ordre à passer, tel qu'on le taperait sur Polymarket."""

    side: str  # "YES" ou "NO"
    label: str
    shares: float
    limit: float  # prix limite : au-dessus, l'arb n'existe plus
    avg: float
    cost: float

    def __str__(self) -> str:
        return f"{self.side} {self.label} @ {self.limit:.3f}"


@dataclass
class Opportunity:
    kind: str
    title: str
    slug: str
    detail: str
    legs: list[str]
    units: float
    capital: float
    profit: float
    days: float = 30.0  # capital immobilisé jusqu'à résolution de la DERNIÈRE jambe
    orders: list[Order] = field(default_factory=list)

    @property
    def roi(self) -> float:
        return 100.0 * self.profit / self.capital if self.capital else 0.0

    @property
    def apy(self) -> float:
        """Le seul chiffre comparable entre deux arbs d'horizons différents."""
        return self.roi * 365.0 / max(self.days, 0.5)

    @property
    def key(self) -> str:
        """Identité stable d'une opportunité, pour ne pas la ré-alerter en boucle."""
        return f"{self.kind}:{self.slug}:{'|'.join(sorted(self.legs))}"

    @property
    def url(self) -> str:
        return f"https://polymarket.com/event/{self.slug}"


# ---------------------------------------------------------------------------
# Classification des familles
# ---------------------------------------------------------------------------

_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}

_STRIKE_RE = re.compile(r"^\s*([↑↓])?\s*[<>]?\s*\$?\s*([\d][\d,]*(?:\.\d+)?)\s*([kKmM])?\s*$")
_DATE_RE = re.compile(
    r"(?:by\s+)?(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(\d{4}))?",
    re.I,
)
def _parse_strike(label: str) -> tuple[float, str] | None:
    """« ↑ 100,000 » → (100000, "up") ; « 62,000 » → (62000, "?").

    La flèche sert à SÉPARER deux échelles cohabitant dans un même événement
    (« reach » et « dip » n'ont rien à voir), jamais à décider de leur monotonie :
    celle-ci est déduite des prix par `_orientation`.
    """
    m = _STRIKE_RE.match(label or "")
    if not m:
        return None
    arrow, digits, suffix = m.groups()
    value = _num(digits.replace(",", ""), -1)
    if value <= 0:
        return None
    if suffix and suffix.lower() == "k":
        value *= 1_000
    elif suffix and suffix.lower() == "m":
        value *= 1_000_000
    direction = {"↑": "up", "↓": "down"}.get(arrow or "", "?")
    return value, direction


def _parse_date(label: str) -> float | None:
    m = _DATE_RE.search(label or "")
    if not m:
        return None
    month, day, year = m.group(1).lower(), int(m.group(2)), m.group(3)
    try:
        dt = datetime.datetime(
            int(year) if year else datetime.date.today().year,
            _MONTHS[month],
            day,
            tzinfo=datetime.timezone.utc,
        )
    except ValueError:
        return None
    return dt.timestamp()


ORIENTATION_TAU = 0.6
ORIENTATION_MIN_PAIRS = 3


def _orientation(outcomes: list[Outcome]) -> int:
    """Déduit des prix si la probabilité croît (+1) ou décroît (-1) avec la clé.

    Deviner par mots-clés est une impasse : « BTC above $60k » et « ceasefire
    continues through Dec 31 » sont toutes deux des échelles de dates/strikes, mais
    de monotonie opposée, et aucune liste de mots-clés ne couvrira les formulations
    à venir. Or un signe inversé ne produit pas une erreur visible — il produit une
    échelle entière de fausses opportunités très convaincantes.

    On lit donc l'orientation dans les prix eux-mêmes (tau de Kendall). C'est fiable
    parce qu'on cherche précisément des anomalies LOCALES : une échelle entièrement
    inversée n'existe pas en pratique, quelques barreaux de travers si.

    Retourne 0 quand le signal est ambigu — auquel cas la famille est ignorée.
    Se taire vaut infiniment mieux qu'un faux positif affirmatif.
    """
    conc = disc = 0
    for i, a in enumerate(outcomes):
        for b in outcomes[i + 1 :]:
            dk, dm = a.key - b.key, a.mid - b.mid
            if dk == 0 or abs(dm) < 1e-6:
                continue  # ex aequo : n'apporte aucune information de sens
            if dk * dm > 0:
                conc += 1
            else:
                disc += 1

    total = conc + disc
    if total < ORIENTATION_MIN_PAIRS:
        return 0
    tau = (conc - disc) / total
    if tau >= ORIENTATION_TAU:
        return 1
    if tau <= -ORIENTATION_TAU:
        return -1
    return 0


def _outcome_base(market: dict) -> tuple[str, str, str, float, float] | None:
    tokens = _jloads(market.get("clobTokenIds"), [])
    if len(tokens) != 2 or not market.get("acceptingOrders"):
        return None
    if market.get("closed") or not market.get("active"):
        return None
    prices = _jloads(market.get("outcomePrices"), [])
    if len(prices) != 2:
        return None
    mid = _num(prices[0], -1)
    if not 0.0 <= mid <= 1.0:
        return None
    label = (market.get("groupItemTitle") or "").strip()
    question = market.get("question") or ""
    return label, question, str(tokens[0]), _num(market.get("liquidityNum")), mid


def classify(event: dict) -> list[Family]:
    """Transforme un événement Gamma en zéro, une ou plusieurs familles contraintes.

    Un même événement peut porter deux échelles indépendantes : « What price will
    Bitcoin hit in August? » mélange les paliers ↑ (reach) et ↓ (dip), qui ont des
    monotonies opposées et ne doivent surtout pas être comparés entre eux.
    """
    markets = event.get("markets") or []
    if len(markets) < 2:
        return []

    ev_id = str(event.get("id"))
    title = event.get("title") or ""
    slug = event.get("slug") or ""

    # --- Famille 1 : buckets mutuellement exclusifs -------------------------
    # On se fie au drapeau negRisk de Polymarket plutôt qu'à une heuristique sur
    # les titres : c'est LA garantie que les issues sont exclusives ET exhaustives.
    # Sans elle, on appliquerait Σ P = 1 à des marchés compatibles entre eux
    # (« quelles équipes iront en playoffs »), ce qui produirait de faux arbs.
    if event.get("negRisk") and len(markets) >= 3:
        outs = []
        for m in markets:
            base = _outcome_base(m)
            if not base:
                continue
            label, question, yes_token, liq, mid = base
            outs.append(
                Outcome(
                    market_id=str(m.get("id")),
                    question=question,
                    label=label or question[:40],
                    yes_token=yes_token,
                    end_date=m.get("endDateIso") or "",
                    liquidity=liq,
                    key=0.0,
                    mid=mid,
                )
            )
        if len(outs) >= 3:
            return [Family(ev_id, title, slug, "buckets", outs)]
        return []

    # --- Familles 2 et 3 : échelles ----------------------------------------
    ladders: dict[str, list[Outcome]] = {}

    for m in markets:
        base = _outcome_base(m)
        if not base:
            continue
        label, question, yes_token, liq, mid = base

        strike = _parse_strike(label)
        if strike:
            key, arrow = strike
            # La flèche ne fixe pas la monotonie, elle sépare deux échelles
            # distinctes (« reach 70k » et « dip to 60k ») logées dans le même
            # événement : les mélanger comparerait des choses sans rapport.
            group = f"strikes:{arrow}"
        else:
            key = _parse_date(label) or _parse_date(question)
            if key is None:
                continue
            group = "dates"

        ladders.setdefault(group, []).append(
            Outcome(
                market_id=str(m.get("id")),
                question=question,
                label=label or question[:40],
                yes_token=yes_token,
                end_date=m.get("endDateIso") or "",
                liquidity=liq,
                key=key,
                mid=mid,
            )
        )

    families = []
    for group, outs in ladders.items():
        # Deux marchés de même clé ne sont pas comparables (doublons Gamma).
        seen, uniq = set(), []
        for o in sorted(outs, key=lambda x: x.key):
            if o.key in seen:
                continue
            seen.add(o.key)
            uniq.append(o)
        if len(uniq) < 3:
            # Sous trois barreaux, l'orientation n'est pas inférable de façon
            # fiable. On préfère perdre l'échelle que risquer un signe inversé.
            continue

        sign = _orientation(uniq)
        if sign == 0:
            continue
        for o in uniq:
            o.rank = sign * o.key

        uniq.sort(key=lambda x: x.rank)
        kind = "dates" if group == "dates" else "strikes"
        families.append(Family(ev_id, title, slug, kind, uniq))
    return families


# ---------------------------------------------------------------------------
# Recherche d'opportunités
# ---------------------------------------------------------------------------


def _fee_rate() -> float:
    return FEE_BPS / 10_000.0


def scan_buckets(fam: Family) -> list[Opportunity]:
    """Σ P(issue) doit valoir exactement 1.

    Sous 1 → on achète YES sur toutes les issues : exactement une paiera 1 $.
    Sur 1  → on achète NO sur toutes : exactement une perdra, les N-1 autres paient.
    """
    outs = [o for o in fam.outcomes if o.book]
    n = len(outs)
    if n < 3 or n > MAX_LEGS:
        return []

    edge = MIN_EDGE_CENTS / 100.0
    found = []
    # Le capital reste bloqué jusqu'à ce que la dernière jambe se dénoue.
    horizon = max(days_until(o.end_date) for o in outs)

    # Côté « somme sous 1 » : coût = Σ ask(YES), gain = 1 $.
    w = walk([o.book.asks for o in outs], 1.0, edge, _fee_rate())
    units, cost = w.units, w.cost
    if units >= MIN_UNITS:
        found.append(
            Opportunity(
                kind="buckets_under",
                title=fam.title,
                slug=fam.slug,
                detail=(
                    f"Σ des meilleurs asks = {sum(o.book.best_ask for o in outs):.4f} < 1.0000 — "
                    f"acheter YES sur les {n} issues coûte moins que le dollar garanti."
                ),
                legs=[f"YES {o.label} @ {o.book.best_ask:.3f}" for o in outs],
                orders=[
                    Order("YES", o.label, units, f.worst_price, f.avg(units), f.cost)
                    for o, f in zip(outs, w.fills)
                ],
                units=units,
                capital=cost,
                profit=units * 1.0 - cost,
                days=horizon,
            )
        )

    # Côté « somme sur 1 » : coût = Σ ask(NO), gain = (N-1) $.
    w = walk([o.book.no_asks() for o in outs], float(n - 1), edge, _fee_rate())
    units, cost = w.units, w.cost
    if units >= MIN_UNITS:
        found.append(
            Opportunity(
                kind="buckets_over",
                title=fam.title,
                slug=fam.slug,
                detail=(
                    f"Σ des meilleurs bids = {sum(o.book.best_bid for o in outs):.4f} > 1.0000 — "
                    f"acheter NO sur les {n} issues : une seule perdra."
                ),
                legs=[f"NO {o.label} @ {1 - o.book.best_bid:.3f}" for o in outs],
                orders=[
                    Order("NO", o.label, units, f.worst_price, f.avg(units), f.cost)
                    for o, f in zip(outs, w.fills)
                ],
                units=units,
                capital=cost,
                profit=units * (n - 1) - cost,
                days=horizon,
            )
        )
    return found


def scan_ladder(fam: Family) -> list[Opportunity]:
    """Si A implique B, alors P(B) ≥ P(A).

    Portefeuille : acheter YES sur B (le large) et NO sur A (l'étroit).
      A vrai (donc B vrai) → 1 + 0 = 1
      B vrai seul          → 1 + 1 = 2
      ni l'un ni l'autre   → 0 + 1 = 1
    Le gain plancher est de 1 $ pour un coût de ask(B) + 1 - bid(A).
    L'arb est donc exécutable exactement quand ask(B) < bid(A) — et la marge par
    part vaut bid(A) - ask(B), sans dépendre d'aucune probabilité.
    """
    outs = [o for o in fam.outcomes if o.book]
    edge = MIN_EDGE_CENTS / 100.0
    fee = _fee_rate()
    found = []

    # `outs` est trié par rang croissant : pour i < j, outs[j] est l'événement le
    # plus large. On teste toutes les paires et pas seulement les barreaux voisins :
    # la transitivité vaut sur les probabilités, pas sur les couples (ask, bid).
    for i, narrow_o in enumerate(outs):
        for wide_o in outs[i + 1 :]:
            if wide_o.rank <= narrow_o.rank:
                continue

            # Une jambe YES sur le large, une jambe NO sur l'étroit.
            w = walk([wide_o.book.asks, narrow_o.book.no_asks()], 1.0, edge, fee)
            units, cost = w.units, w.cost
            if units < MIN_UNITS:
                continue

            found.append(
                Opportunity(
                    kind="ladder_" + fam.kind,
                    title=fam.title,
                    slug=fam.slug,
                    detail=(
                        f"« {narrow_o.label} » implique « {wide_o.label} », or le premier "
                        f"s'achète moins cher : bid {narrow_o.book.best_bid:.3f} "
                        f"> ask {wide_o.book.best_ask:.3f}. Monotonie violée."
                    ),
                    legs=[
                        f"YES {wide_o.label} @ {wide_o.book.best_ask:.3f}",
                        f"NO {narrow_o.label} @ {1 - narrow_o.book.best_bid:.3f}",
                    ],
                    orders=[
                        Order("YES", wide_o.label, units, w.fills[0].worst_price,
                              w.fills[0].avg(units), w.fills[0].cost),
                        Order("NO", narrow_o.label, units, w.fills[1].worst_price,
                              w.fills[1].avg(units), w.fills[1].cost),
                    ],
                    units=units,
                    capital=cost,
                    profit=units * 1.0 - cost,
                    days=max(
                        days_until(wide_o.end_date), days_until(narrow_o.end_date)
                    ),
                )
            )
    return found


# ---------------------------------------------------------------------------
# Réseau
# ---------------------------------------------------------------------------


async def _get(session, url, params=None, retries=2):
    for attempt in range(retries + 1):
        try:
            async with session.get(url, params=params, headers=UA, timeout=30) as r:
                if r.status == 429:
                    await asyncio.sleep(2 + attempt * 3)
                    continue
                r.raise_for_status()
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt == retries:
                return None
            await asyncio.sleep(1 + attempt)
    return None


async def fetch_events(session, max_events=MAX_EVENTS) -> list[dict]:
    events, offset = [], 0
    while len(events) < max_events:
        page = await _get(
            session,
            f"{GAMMA}/events",
            {
                "active": "true",
                "closed": "false",
                "archived": "false",
                "limit": 100,
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
        if not page:
            break
        events.extend(page)
        if len(page) < 100:
            break
        offset += 100
    return events[:max_events]


async def fetch_books(session, token_ids: list[str]) -> dict[str, Book]:
    """Le POST /books ne renvoie PAS les carnets dans l'ordre demandé — on indexe
    impérativement par asset_id, sinon on associe des prix aux mauvais marchés."""
    books: dict[str, Book] = {}
    chunks = [
        token_ids[i : i + BOOK_CHUNK] for i in range(0, len(token_ids), BOOK_CHUNK)
    ]

    async def one(chunk):
        payload = [{"token_id": t} for t in chunk]
        for attempt in range(3):
            try:
                async with session.post(
                    f"{CLOB}/books", json=payload, headers=UA, timeout=40
                ) as r:
                    if r.status == 429:
                        await asyncio.sleep(2 + attempt * 3)
                        continue
                    r.raise_for_status()
                    return await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(1 + attempt)
        return []

    sem = asyncio.Semaphore(4)

    async def guarded(chunk):
        async with sem:
            return await one(chunk)

    for result in await asyncio.gather(*(guarded(c) for c in chunks)):
        for raw in result or []:
            asset = str(raw.get("asset_id") or "")
            if asset:
                books[asset] = Book.from_api(raw)
    return books


# ---------------------------------------------------------------------------
# Scan complet
# ---------------------------------------------------------------------------


@dataclass
class Margin:
    """Écart à une contrainte, en cents par part.

    Négatif = contrainte respectée (l'immense majorité du temps), positif = arb
    au moins au sommet du carnet. Sert au tableau de bord : un salon qui
    n'afficherait que les opportunités serait vide en permanence, alors que la
    marge la plus serrée du marché, elle, bouge en continu.
    """

    cents: float  # marge au SOMMET du carnet
    title: str
    slug: str
    detail: str
    units: float = 0.0  # parts réellement exécutables à marge positive

    @property
    def url(self) -> str:
        return f"https://polymarket.com/event/{self.slug}"


def compute_margins(families: list[Family]) -> list[Margin]:
    """Marge de chaque contrainte vérifiable, la plus serrée en premier.

    `cents` est la marge au sommet du carnet, `units` la taille réellement
    disponible à marge positive. Les deux sont nécessaires : un écart de 7 cents
    adossé à 0,03 part est un mirage, et n'afficher que la marge en ferait une
    fausse promesse permanente.
    """
    out: list[Margin] = []
    for fam in families:
        # Ne JAMAIS écarter une issue au carnet partiellement vide : la contrainte
        # Σ P = 1 ne porte que sur l'ensemble exhaustif des issues. Filtrer donne
        # une somme partielle qui franchit 1 sans qu'aucun arb n'existe.
        outs = [o for o in fam.outcomes if o.book]

        if fam.kind == "buckets":
            if len(outs) < 3:
                continue
            n = len(outs)
            sa = sum(o.book.best_ask for o in outs)
            sb = sum(o.book.best_bid for o in outs)
            u_under, _ = walk([o.book.asks for o in outs], 1.0, 0.0, _fee_rate())
            u_over, _ = walk([o.book.no_asks() for o in outs], float(n - 1), 0.0, _fee_rate())
            out.append(Margin(100 * (1.0 - sa), fam.title, fam.slug,
                              f"Σ ask {sa:.4f} (needs < 1)", u_under))
            out.append(Margin(100 * (sb - 1.0), fam.title, fam.slug,
                              f"Σ bid {sb:.4f} (needs > 1)", u_over))
        else:
            for i, narrow in enumerate(outs):
                for wide in outs[i + 1 :]:
                    if wide.rank <= narrow.rank:
                        continue
                    u, _ = walk(
                        [wide.book.asks, narrow.book.no_asks()], 1.0, 0.0, _fee_rate()
                    )
                    out.append(
                        Margin(
                            100 * (narrow.book.best_bid - wide.book.best_ask),
                            fam.title,
                            fam.slug,
                            f"bid «{narrow.label}» {narrow.book.best_bid:.3f} "
                            f"vs ask «{wide.label}» {wide.book.best_ask:.3f}",
                            u,
                        )
                    )

    out.sort(key=lambda m: m.cents, reverse=True)
    return out


@dataclass
class ScanResult:
    opportunities: list[Opportunity]
    events_scanned: int
    families: int
    markets: int
    duration: float
    tightest: list[Margin] = field(default_factory=list)
    constraints: int = 0


async def _collect(max_events: int) -> tuple[list[dict], list[Family], list[str]]:
    """Récupère les événements, en extrait les familles contraintes et attache les
    carnets. Partagé par `scan` et `diagnose` pour qu'ils voient exactement la
    même chose — un diagnostic sur un autre chemin de code ne diagnostiquerait rien.
    """
    async with aiohttp.ClientSession() as session:
        events = await fetch_events(session, max_events)

        families: list[Family] = []
        for ev in events:
            if _num(ev.get("liquidity")) < MIN_EVENT_LIQUIDITY:
                continue
            families.extend(classify(ev))

        # Une famille de plus de MAX_LEGS issues reste utile pour les échelles
        # (comparaisons deux à deux) mais pas pour les buckets (capital absurde).
        families = [f for f in families if f.kind != "buckets" or len(f.outcomes) <= MAX_LEGS]

        tokens = sorted({o.yes_token for f in families for o in f.outcomes})
        books = await fetch_books(session, tokens)

    for fam in families:
        for o in fam.outcomes:
            o.book = books.get(o.yes_token)

    return events, families, tokens


async def scan(max_events=MAX_EVENTS) -> ScanResult:
    started = asyncio.get_event_loop().time()
    events, families, tokens = await _collect(max_events)

    opportunities = []
    for fam in families:
        found = scan_buckets(fam) if fam.kind == "buckets" else scan_ladder(fam)
        opportunities.extend(
            o
            for o in found
            if o.roi >= MIN_ROI_PCT
            and o.apy >= MIN_APY_PCT
            and o.profit >= MIN_PROFIT_USD
        )

    # Tri par APY : c'est l'ordre dans lequel on veut réellement les traiter.
    opportunities.sort(key=lambda o: o.apy, reverse=True)

    margins = compute_margins(families)

    return ScanResult(
        opportunities=opportunities,
        events_scanned=len(events),
        families=len(families),
        markets=len(tokens),
        duration=asyncio.get_event_loop().time() - started,
        tightest=margins[:8],
        constraints=len(margins),
    )


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------


async def diagnose(max_events=MAX_EVENTS, top=15):
    """Montre les contraintes les PLUS SERRÉES, y compris celles qui tiennent.

    Un scan qui ne renvoie rien ne dit pas si le marché est cohérent ou si le
    moteur est muet. En affichant les marges frôlant zéro par le mauvais côté, on
    voit le pipeline travailler sur données réelles et on peut régler les seuils
    en connaissance de cause.

    Marge en cents par part : négative = contrainte respectée (cas normal),
    positive = arb exécutable.
    """
    events, families, tokens = await _collect(max_events)

    per_kind: dict[str, int] = {}
    for fam in families:
        per_kind[fam.kind] = per_kind.get(fam.kind, 0) + 1

    margins = compute_margins(families)

    print(f"Événements scannés   : {len(events)}")
    print(f"Marchés interrogés   : {len(tokens)}")
    print(f"Familles par type    : {per_kind}")
    print(f"Contraintes vérifiées: {len(margins)}\n")
    print(f"Les {top} marges les plus serrées (cents par part) :\n")
    for m in margins[:top]:
        flag = "ARB" if m.cents > 0 else "   "
        print(f"  {flag} {m.cents:+7.2f}¢ x{m.units:>9,.0f}  {m.title[:38]:38s}  {m.detail}")
    print(
        f"\nSeuil d'alerte actuel : +{MIN_EDGE_CENTS:.2f}¢ par part, "
        f"ROI ≥ {MIN_ROI_PCT}%, taille ≥ {MIN_UNITS:.0f} parts."
    )


# ---------------------------------------------------------------------------
# CLI — permet de vérifier les chiffres sans lancer le bot
# ---------------------------------------------------------------------------


async def _main():
    import sys

    if "--diagnose" in sys.argv:
        await diagnose()
        return

    print("Scan Polymarket en cours…\n")
    res = await scan()

    print(f"Événements scannés  : {res.events_scanned}")
    print(f"Familles contraintes: {res.families}")
    print(f"Marchés interrogés  : {res.markets}")
    print(f"Durée               : {res.duration:.1f}s")
    print(f"Opportunités        : {len(res.opportunities)}\n")

    if not res.opportunities:
        print("Aucune incohérence exécutable. C'est le résultat normal la plupart")
        print("du temps — un marché cohérent n'offre rien. Le bot ne sert que")
        print("les moments où il ne l'est pas.")
        return

    for i, o in enumerate(res.opportunities[:20], 1):
        print(f"{'─' * 70}")
        print(f"{i}. [{o.kind}] {o.title}")
        print(f"   {o.detail}")
        for leg in o.legs[:8]:
            print(f"     · {leg}")
        if len(o.legs) > 8:
            print(f"     · … et {len(o.legs) - 8} autres jambes")
        print(
            f"   → {o.units:,.0f} parts | capital {fmt_usd(o.capital)} "
            f"| gain {fmt_usd(o.profit)} | ROI {o.roi:.2f}%"
        )
        print(f"   {o.url}")




# ---------------------------------------------------------------------------
# Jugement a posteriori des alertes
# ---------------------------------------------------------------------------


async def score_events(slugs: list[str]) -> dict[str, str]:
    """Verdict par événement : l'arbitrage annoncé aurait-il payé ?

    Un arbitrage de cohérence paie PAR CONSTRUCTION — sauf si un marché du
    groupe est **annulé** (`outcomePrices == ["0","0"]`), auquel cas il ne paie
    ni YES ni NO et la somme garantie s'effondre. C'est le seul mode d'échec, et
    donc la seule chose à vérifier.

    Contrôle volontairement strict : on exige **exactement un gagnant** dans le
    groupe. Se contenter de « l'événement est clos » laisserait passer des
    groupes à zéro ou deux gagnants, qui casseraient l'arithmétique sans que
    rien ne le signale.

    Retourne "win" | "void" | "partial" | "open" par slug.
    """
    out: dict[str, str] = {}
    sem = asyncio.Semaphore(6)

    async def one(session, slug):
        async with sem:
            data = await _get(session, f"{GAMMA}/events", {"slug": slug})
        if not data:
            return
        markets = (data[0].get("markets") or []) if isinstance(data, list) else []
        if not markets:
            return
        closed = [m for m in markets if m.get("closed")]
        if not closed:
            out[slug] = "open"
            return
        if len(closed) < len(markets):
            out[slug] = "partial"
            return

        winners = voided = 0
        for m in closed:
            prices = _jloads(m.get("outcomePrices"), [])
            if len(prices) != 2:
                continue
            if prices == ["0", "0"]:
                voided += 1
            elif prices[0] == "1":
                winners += 1
        out[slug] = "win" if (winners == 1 and voided == 0) else "void"

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(one(session, s) for s in slugs))
    return out


if __name__ == "__main__":
    asyncio.run(_main())
