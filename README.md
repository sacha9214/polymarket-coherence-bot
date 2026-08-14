# Coherence Bot — Polymarket

Détecte les **incohérences logiques** entre marchés Polymarket liés, et les
signale sur Discord.

Le bot ne prédit rien. Il ne dit jamais « achète » ni « vends ». Il cherche des
prix qui **se contredisent entre eux**, et pour chaque contradiction il construit
un portefeuille dont le gain est positif quel que soit le résultat de l'événement.
Une alerte n'est donc pas une opinion : c'est un fait arithmétique.

---

## Les trois contradictions traquées

### 🟢🔵 Buckets — la somme doit valoir 1

Sur les marchés groupés (`negRisk`), les issues sont mutuellement exclusives et
couvrent tous les cas : *Bitcoin price on August 14?* → `<56k`, `56-58k`, `58-60k`…
Exactement une paiera 1 $. Leur somme doit donc valoir 1.

- **Σ ask < 1** → acheter YES sur toutes les issues coûte moins que le dollar garanti.
- **Σ bid > 1** → acheter NO sur toutes : une seule perdra, les autres paient.

Chaque issue a son propre carnet d'ordres, et rien ne force mécaniquement la somme :
c'est là que l'écart naît.

### 🟣 Échelles de strikes — la monotonie

*Bitcoin above $60,000* ne peut pas être **moins** probable que *Bitcoin above
$70,000* : le second implique le premier. Quand l'ordre s'inverse, la paire est
arbitrable.

Fonctionne aussi sur les échelles inversées (*dip to $60,000*), dont la monotonie
est l'opposée.

### 🟠 Échelles de dates — même principe

*Bitcoin hits $150k by December 31* ≥ *by June 30*. Toute inversion est exploitable.

### Le portefeuille, dans les trois cas

Si A implique B, alors P(B) ≥ P(A). On achète YES sur B (le large) et NO sur A
(l'étroit) :

| Résultat | YES B | NO A | Total |
|---|---|---|---|
| A vrai (donc B vrai) | 1 | 0 | **1** |
| B vrai seul | 1 | 1 | **2** |
| ni l'un ni l'autre | 0 | 1 | **1** |

Gain plancher 1 $ pour un coût de `ask(B) + 1 − bid(A)`. L'opération est donc
rentable exactement quand **`ask(B) < bid(A)`**, et la marge par part vaut
`bid(A) − ask(B)` — sans qu'aucune probabilité n'intervienne.

---

## Ce qui fait la différence avec un jouet

**Les prix affichés ne sont jamais utilisés.** `outcomePrices` est un mid ; une
inversion sur les mid disparaît dès qu'on regarde le spread. Tout est calculé sur
le carnet réel, en descendant les niveaux un par un, ce qui donne la taille
réellement exécutable et le gain net à cette taille.

**L'orientation des échelles est déduite des données, pas devinée.** Une liste de
mots-clés ne survit pas à la diversité des formulations : *« BTC above $60k »* et
*« ceasefire continues through Dec 31 »* sont toutes deux des échelles, de
monotonie opposée. Un signe inversé ne produit pas une erreur visible — il produit
une échelle entière de fausses opportunités très convaincantes (c'est arrivé
pendant le développement). Le moteur lit donc le sens dans les prix eux-mêmes
(tau de Kendall), et **se tait** quand le signal est ambigu.

**Le classement se fait à l'APY, pas au ROI.** 0,3 % qui se dénoue demain vaut
bien mieux que 5 % qui se dénoue dans un an, puisque le capital se recycle. Trier
au ROI brut classe à l'envers.

**Les sommes partielles sont interdites.** Écarter une issue dont un côté du
carnet est vide fait franchir 1 à la somme sans qu'aucun arb n'existe. La
contrainte ne porte que sur l'ensemble exhaustif.

---

## Installation

```bash
cd polymarket-coherence-bot
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

Mets le jeton de ton bot Discord dans `token.txt` (une ligne), ou dans
`DISCORD_BOT_TOKEN`. Optionnel : `DISCORD_GUILD_ID` pour un enregistrement
instantané des commandes au lieu d'environ une heure.

```bash
./start_mac_linux.sh
```

## Sans Discord

Le moteur est autonome — c'est ce qui permet de vérifier les chiffres sans
jamais lancer le bot :

```bash
./venv/bin/python coherence.py             # scan complet
./venv/bin/python coherence.py --diagnose  # marges les plus serrées, même celles qui tiennent
./venv/bin/python selftest.py              # 18 tests sur carnets synthétiques
```

`--diagnose` est là pour une raison précise : un scan qui ne trouve rien est
indiscernable d'un moteur cassé. Le diagnostic montre les contraintes qui frôlent
zéro, ce qui prouve que le pipeline travaille et permet de régler les seuils.

## Commandes Discord

| Commande | Effet |
|---|---|
| `/setup` | **Crée toute la structure de salons** et branche tout (admin) |
| `/scan [limit]` | Scan immédiat, top opportunités par rendement annualisé |
| `/preview` | Poste une alerte d'exemple (vérifie rendu et permissions) |
| `/board` | Installe le tableau vivant, réécrit en place à chaque cycle |
| `/watch [min_annualised] [min_profit]` | Abonne le salon aux alertes |
| `/unwatch` | Coupe les alertes du salon |
| `/status` | Réglages, seuils et dernier scan |
| `/guide` | Poste la note « comment lire ce salon » (à épingler) |

`/setup` crée la catégorie **POLYMARKET COHERENCE** avec quatre salons —
`coherence-guide` (guide épinglé), `coherence-board` (tableau vivant),
`arb-alerts` (le flux) et `arb-discussion` (ouvert) — les trois premiers en
lecture seule pour les membres. La commande est **idempotente** : la relancer
réutilise les salons existants et recâble tout, elle ne crée pas de doublons.

Les noms sont préfixés à dessein. Ce bot cohabite avec le bot overlap, dont le
`/setup` crée déjà `how-it-works` et `discussion` : avec des noms génériques,
chaque bot croirait reconnaître les salons de l'autre et irait écrire dedans. La
recherche de salons existants est en plus limitée à notre propre catégorie, pas
au serveur entier.

### Le tableau vivant

Comme « zéro opportunité » est l'état normal, un tableau qui n'afficherait que les
arbs serait vide en permanence et ne dirait pas si le scanner tourne encore. Il
montre donc la **santé de cohérence du marché** : les contraintes les plus
serrées, chacune avec son écart au sommet du carnet **et sa taille réellement
exécutable**.

Les deux sont indispensables. Exemple réel rencontré : un écart de **+7,10 ¢** sur
un marché de température, adossé à **0,03 part** disponible sur une des jambes.
Afficher l'écart seul en aurait fait une promesse mensongère permanente.

## Réglages

Tout est en haut de `coherence.py` :

| Réglage | Défaut | Rôle |
|---|---|---|
| `MAX_EVENTS` | 400 | Événements scannés (volume 24h décroissant) |
| `MIN_EDGE_CENTS` | 1.0 | Marge minimale par part, après frais |
| `MIN_APY_PCT` | 15.0 | Le vrai filtre de décision |
| `MIN_PROFIT_USD` | 1.0 | Sous ce seuil, le gas mange l'opération |
| `MAX_LEGS` | 15 | Au-delà, le capital immobilisé est absurde |
| `FEE_BPS` | 0.0 | **À vérifier** avant de trader en réel |

Un scan complet couvre ~1 900 marchés en **~2 secondes**.

---

## Limites — à lire avant de trader

- **Rien n'est exécuté.** Le bot détecte et signale, il ne passe aucun ordre.
- **Pas d'exécution atomique.** Polymarket n'offre pas le tout-ou-rien
  multi-jambes. Si une jambe passe et l'autre non, tu te retrouves avec un pari
  directionnel — exactement ce que l'outil sert à éviter. C'est le vrai risque.
- **Le carnet bouge en secondes.** Chaque chiffre vaut pour l'instant où il a été
  envoyé.
- **Les frais sont supposés nuls** (`FEE_BPS = 0`). Historiquement vrai sur
  Polymarket, mais des frais ont été introduits sur certains marchés : vérifie la
  grille en vigueur avant de dimensionner.
- **Le mécanisme `negRisk`** referme automatiquement une partie des écarts sur les
  marchés groupés. Les opportunités « buckets » y sont donc plus rares qu'ailleurs.
- **Le silence est l'état normal.** Un marché cohérent n'offre rien. Le bot ne sert
  que les minutes où il ne l'est pas — si tu veux des alertes en continu, tu as
  construit le mauvais outil.
