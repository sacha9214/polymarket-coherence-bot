"""
Tests du moteur — sans réseau, sur carnets synthétiques.

Raison d'être : un scan live qui ne trouve rien est indiscernable d'un moteur
cassé. Ces tests injectent des violations dont la réponse est calculable à la
main, ce qui prouve que le silence en production est un vrai silence.

    python3 selftest.py
"""

from __future__ import annotations

import sys

import coherence as C


def book(bids=(), asks=()) -> C.Book:
    return C.Book(
        bids=sorted([(p, s) for p, s in bids], key=lambda x: -x[0]),
        asks=sorted([(p, s) for p, s in asks], key=lambda x: x[0]),
    )


def out(label, key, mid, bids=(), asks=(), rank=None) -> C.Outcome:
    o = C.Outcome(
        market_id=label,
        question=label,
        label=label,
        yes_token="t" + label,
        end_date="",
        liquidity=1e6,
        key=key,
        mid=mid,
        rank=key if rank is None else rank,
    )
    o.book = book(bids, asks)
    return o


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---------------------------------------------------------------------------


@case
def test_walk_descend_les_niveaux():
    """Le coût unitaire se dégrade en profondeur : la marche doit s'arrêter pile
    quand le niveau suivant rend l'opération non rentable."""
    legs = [
        [(0.40, 10), (0.45, 100)],
        [(0.50, 20), (0.52, 50)],
    ]
    units, cost = C.walk(legs, payoff=1.0, min_edge=0.01, fee_rate=0.0)
    # 10 @ 0.90 = 9.00, puis 10 @ 0.95 = 9.50, puis 50 @ 0.97 = 48.50.
    assert abs(units - 70.0) < 1e-9, units
    assert abs(cost - 67.0) < 1e-9, cost


@case
def test_walk_refuse_marge_insuffisante():
    legs = [[(0.60, 100)], [(0.395, 100)]]  # unité = 0.995, marge 0.5 cent
    units, _ = C.walk(legs, payoff=1.0, min_edge=0.01, fee_rate=0.0)
    assert units == 0.0, units


@case
def test_walk_carnet_vide():
    units, cost = C.walk([[(0.4, 10)], []], payoff=1.0, min_edge=0.01, fee_rate=0.0)
    assert (units, cost) == (0.0, 0.0)


@case
def test_walk_applique_les_frais():
    legs = [[(0.50, 100)], [(0.47, 100)]]  # unité 0.97, marge 3 cents
    units, _ = C.walk(legs, payoff=1.0, min_edge=0.01, fee_rate=0.0)
    assert units == 100.0
    # 400 bps de frais portent l'unité à 1.0088 : l'arb disparaît.
    units, _ = C.walk(legs, payoff=1.0, min_edge=0.01, fee_rate=0.04)
    assert units == 0.0, units


# ---------------------------------------------------------------------------


@case
def test_echelle_detecte_inversion():
    """« étroit » coté plus cher que « large » : arb à 20 cents la part."""
    fam = C.Family(
        "e", "Test ladder", "slug", "strikes",
        [
            out("strike haut (étroit)", 1, 0.80, bids=[(0.80, 100)], asks=[(0.82, 100)]),
            out("strike moyen", 2, 0.70, bids=[(0.68, 100)], asks=[(0.70, 100)]),
            out("strike bas (large)", 3, 0.60, bids=[(0.58, 100)], asks=[(0.60, 100)]),
        ],
    )
    opps = C.scan_ladder(fam)
    assert opps, "l'inversion n'a pas été détectée"
    best = max(opps, key=lambda o: o.profit)
    assert abs(best.units - 100.0) < 1e-9, best.units
    assert abs(best.profit - 20.0) < 1e-9, best.profit
    assert abs(best.capital - 80.0) < 1e-9, best.capital


@case
def test_echelle_coherente_ne_dit_rien():
    fam = C.Family(
        "e", "Test ladder", "slug", "strikes",
        [
            out("étroit", 1, 0.20, bids=[(0.19, 100)], asks=[(0.21, 100)]),
            out("moyen", 2, 0.50, bids=[(0.49, 100)], asks=[(0.51, 100)]),
            out("large", 3, 0.80, bids=[(0.79, 100)], asks=[(0.81, 100)]),
        ],
    )
    assert C.scan_ladder(fam) == []


@case
def test_buckets_somme_sous_un():
    fam = C.Family(
        "e", "Test buckets", "slug", "buckets",
        [out(f"b{i}", 0, 0.30, bids=[(0.28, 100)], asks=[(0.30, 100)]) for i in range(3)],
    )
    opps = [o for o in C.scan_buckets(fam) if o.kind == "buckets_under"]
    assert opps, "Σ ask = 0.90 < 1 non détecté"
    o = opps[0]
    assert abs(o.units - 100.0) < 1e-9 and abs(o.profit - 10.0) < 1e-9, (o.units, o.profit)


@case
def test_buckets_somme_sur_un():
    """Σ bid = 1.50 > 1 : acheter NO partout, une seule issue perdra."""
    fam = C.Family(
        "e", "Test buckets", "slug", "buckets",
        [out(f"b{i}", 0, 0.50, bids=[(0.50, 100)], asks=[(0.52, 100)]) for i in range(3)],
    )
    opps = [o for o in C.scan_buckets(fam) if o.kind == "buckets_over"]
    assert opps, "Σ bid = 1.50 > 1 non détecté"
    o = opps[0]
    assert abs(o.units - 100.0) < 1e-9, o.units
    assert abs(o.capital - 150.0) < 1e-9, o.capital
    assert abs(o.profit - 50.0) < 1e-9, o.profit


@case
def test_buckets_coherents_ne_disent_rien():
    fam = C.Family(
        "e", "Test buckets", "slug", "buckets",
        [out(f"b{i}", 0, 0.33, bids=[(0.32, 100)], asks=[(0.35, 100)]) for i in range(3)],
    )
    assert C.scan_buckets(fam) == []


# ---------------------------------------------------------------------------


@case
def test_orientation_croissante():
    outs = [out("a", 1, 0.10), out("b", 2, 0.40), out("c", 3, 0.80)]
    assert C._orientation(outs) == 1


@case
def test_orientation_decroissante():
    outs = [out("a", 1, 0.80), out("b", 2, 0.40), out("c", 3, 0.10)]
    assert C._orientation(outs) == -1


@case
def test_orientation_ambigue_se_tait():
    outs = [out("a", 1, 0.50), out("b", 2, 0.10), out("c", 3, 0.40), out("d", 4, 0.20)]
    assert C._orientation(outs) == 0


@case
def test_regression_ceasefire_through():
    """Régression du faux positif trouvé en production.

    « Ceasefire continues through <date> » : plus la date est lointaine, MOINS
    c'est probable — l'inverse d'un marché « by <date> ». Une orientation devinée
    par mots-clés inversait le signe et fabriquait dix arbs inexistants sur une
    échelle en réalité parfaitement cohérente.
    """
    mids = [0.985, 0.910, 0.780, 0.720, 0.610]  # prix réels observés
    outs = [out(f"d{i}", float(i), m) for i, m in enumerate(mids)]
    assert C._orientation(outs) == -1

    for o in outs:
        o.rank = -o.key
        o.book = book(bids=[(o.mid - 0.005, 5000)], asks=[(o.mid + 0.005, 5000)])
    outs.sort(key=lambda x: x.rank)

    fam = C.Family("e", "Ceasefire", "slug", "dates", outs)
    assert C.scan_ladder(fam) == [], "le faux positif est revenu"


@case
def test_orientation_ignore_les_ex_aequo():
    """Une échelle où tout est collé à 0.9995 n'informe sur rien : se taire."""
    outs = [out(f"d{i}", float(i), 0.9995) for i in range(5)]
    assert C._orientation(outs) == 0


@case
def test_parse_strike():
    assert C._parse_strike("↑ 100,000") == (100000.0, "up")
    assert C._parse_strike("↓ 60,000") == (60000.0, "down")
    assert C._parse_strike("54,000") == (54000.0, "?")
    assert C._parse_strike("$2.5k") == (2500.0, "?")
    assert C._parse_strike("<56,000") == (56000.0, "?")
    assert C._parse_strike("Gavin Newsom") is None
    assert C._parse_strike("January") is None


@case
def test_parse_date():
    assert C._parse_date("by December 31, 2026") is not None
    assert C._parse_date("March 31, 2026") is not None
    assert C._parse_date("Gavin Newsom") is None
    assert C._parse_date("February 30, 2026") is None  # date impossible


@case
def test_no_asks_miroir_des_bids():
    b = book(bids=[(0.60, 10), (0.58, 20)])
    got = b.no_asks()
    want = [(0.40, 10), (0.42, 20)]
    assert len(got) == len(want)
    for (gp, gs), (wp, ws) in zip(got, want):
        assert abs(gp - wp) < 1e-9 and gs == ws, (got, want)


@case
def test_roi_et_cle_stable():
    o = C.Opportunity("k", "t", "s", "d", ["b", "a"], 100, 80.0, 20.0)
    assert abs(o.roi - 25.0) < 1e-9
    assert o.key == C.Opportunity("k", "t", "s", "d", ["a", "b"], 1, 1, 1).key


# ---------------------------------------------------------------------------


def main() -> int:
    failed = 0
    for fn in CASES:
        name = fn.__name__
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  ÉCHEC  {name}\n         {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERREUR {name}\n         {type(e).__name__}: {e}")
        else:
            print(f"  ok     {name}")

    print()
    if failed:
        print(f"{failed} test(s) en échec sur {len(CASES)}.")
        return 1
    print(f"{len(CASES)} tests passés.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
