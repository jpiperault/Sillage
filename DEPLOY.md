# Déployer Sillage sur Hangar

Sillage (« Suivez l'argent informé ») : portail web de suivi des transactions déclarées des initiés américains,
avec un mode Entraînement en argent fictif. Un seul conteneur, état entièrement dans PostgreSQL.

## Fiche Hangar

| Champ | Valeur |
|---|---|
| Nom | Silliage |
| Identifiant | `sillage` |
| Adresse | `https://sillage.piperault.fr/` |
| Image | `ghcr.io/jpiperault/sillage` |
| Version | la dernière publiée, au format `x.y.z` (ex. `1.2.0`) — **jamais `latest`**, ce tag n'est pas publié |
| Port interne | `8080` |
| Healthcheck | `GET /health` → `200 ok` (503 si la base est inaccessible) |
| Mémoire | 384 Mo (mesuré : ~50 Mo au repos, ~60-70 Mo pendant la collecte) |
| PostgreSQL | oui (`DATABASE_URL` injectée par Hangar) |
| Volume `/data` | créé et attribué à l'utilisateur 10001, **non utilisé** (tout est en base) |
| Android | hors périmètre |

## Variables d'environnement

Seules `DATABASE_URL`, `JWT_SECRET`, `ADMIN_EMAIL` et `ADMIN_PASSWORD` doivent être des variables. Tout le reste se
règle aussi dans l'interface (**Compte → Réglages**, administrateur) : la valeur saisie dans l'interface prime sur la
variable, « Rétablir » revient à la variable ou au défaut, et la prise en compte se fait au cycle de collecte suivant
sans redémarrage.

| Variable | Obligatoire | Rôle |
|---|---|---|
| `DATABASE_URL` | oui (Hangar) | `postgresql://user:mdp@hôte:5432/base` |
| `JWT_SECRET` | oui | Clé secrète qui signe les sessions de connexion (voir plus bas). ≥ 32 caractères : `openssl rand -hex 32`. |
| `ADMIN_EMAIL` | oui | Adresse du compte administrateur (réservée : personne d'autre ne peut s'inscrire avec). |
| `ADMIN_PASSWORD` | 1er démarrage | Crée le compte admin s'il n'existe pas : `openssl rand -base64 24`. Ignorée ensuite (changer le mot de passe dans l'onglet Compte). |
| `SEC_USER_AGENT` | non | En-tête envoyé à sec.gov et house.gov. Défaut `Sillage/1.0 <ADMIN_EMAIL>` : **l'adresse admin part donc à la SEC** si cette variable est vide (voir plus bas). |
| `HOUSE_ENABLED` | non | `true` par défaut : transactions des représentants lues dans les déclarations officielles du Greffe de la Chambre (gratuit). |
| `QUIVER_API_KEY` | non | Ajoute Quiver Quantitative (payant), surtout utile pour le Sénat. Si `HOUSE_ENABLED` est actif, Quiver ne fournit que le Sénat (pas de doublon). |
| `FORM144_ENABLED` | non | `true` par défaut : ventes annoncées par les initiés (SEC Form 144). |
| `AMF_ENABLED` | non | `true` par défaut : déclarations des dirigeants français (AMF, base BDIF). |
| `FUNDS_13F` | non | Fonds suivis via leurs 13F : vide = 18 grands fonds par défaut (Berkshire, Pershing Square, Baupost…), liste de CIK séparés par des virgules pour choisir, `off` pour désactiver. CUSIP → ticker via OpenFIGI (gratuit). |
| `BAFIN_ENABLED` | non | `true` par défaut : Allemagne, déclarations des dirigeants (BaFin, export CSV public). |
| `SENATE_ENABLED` | non | `false` par défaut : Sénat (eFD). **Non testé** (site bloqué depuis le serveur de dev) : utiliser « Tester l'accès au Sénat » dans Réglages avant de l'activer. |
| `SCORE_VERSION` | non | `auto` (défaut) : score v2 calibré s'il est validé, sinon v1 ; `v1` ou `v2` pour forcer. |
| `FINRA_ENABLED` | non | `true` par défaut : part quotidienne des ventes à découvert (FINRA), affichée dans le ticket. |
| `BACKTEST_ENABLED` | non | `true` par défaut : backtest du score (historique SEC + cours Nasdaq), voir plus bas. |
| `BACKTEST_QUARTERS` | non | Trimestres d'historique Form 4 analysés, 6 par défaut (1 à 12). |
| `TELEGRAM_BOT_TOKEN` | non | Jeton d'un bot créé avec @BotFather : active les alertes Telegram. |
| `PUBLIC_URL` | non | Adresse publique (`https://sillage.piperault.fr`) pour les liens des alertes. |
| `ALLOW_SIGNUP` | non | `true` par défaut ; `false` ferme les inscriptions. |
| `INGEST_ENABLED` | non | `true` par défaut. |
| `INGEST_INTERVAL_MIN` | non | 10 par défaut. |
| `BACKFILL_DAYS` | non | Jours ouvrés rattrapés via l'index quotidien SEC, 3 par défaut. |
| `PAPER_START_CASH` | non | Capital fictif de départ, 25 000 $ par défaut. |

Une variable obligatoire absente arrête le démarrage avec un message qui la nomme et le code de sortie 2,
par exemple : `démarrage impossible : variable(s) d'environnement obligatoire(s) manquante(s) : JWT_SECRET`.

### `JWT_SECRET`

Quand on se connecte, Sillage remet un jeton de session (JWT, dans un cookie, valable 30 jours) qui dit « compte n° X ».
`JWT_SECRET` sert à le signer : sans cette clé, impossible de fabriquer ou de modifier un jeton (par exemple se faire
passer pour l'admin). C'est un mot de passe que personne ne tape : on le génère une fois et on le garde.

- Le garder secret : qui le connaît peut se faire passer pour n'importe quel compte.
- Garder la même valeur d'une version à l'autre : le changer déconnecte tout le monde (utile seulement en cas de fuite).
- Ce n'est pas le mot de passe admin (`ADMIN_PASSWORD`).

### Données envoyées aux services extérieurs

| Service | Ce qui est envoyé |
|---|---|
| sec.gov (Form 4, Form 144, 13D/13G, 13F, tickers) | `User-Agent: <SEC_USER_AGENT>`, par défaut `Sillage/1.0 <ADMIN_EMAIL>` |
| disclosures-clerk.house.gov (élus) | le même en-tête |
| AMF, FINRA, Yahoo, Nasdaq, Euronext, BaFin, Sénat | en-tête de navigateur générique, aucune donnée personnelle |
| BCE, USAspending | rien de personnel (taux de change ; nom de la société cherchée) |
| api.telegram.org (si jeton réglé) | le texte des alertes et l'identifiant de conversation de chaque utilisateur lié |
| OpenFIGI | en-tête par défaut de la bibliothèque HTTP + les CUSIP à convertir |
| Quiver Quantitative (si clé) | la clé `QUIVER_API_KEY` |

La SEC exige un contact joignable dans le `User-Agent` de tout robot (politique « fair access » d'EDGAR) et peut
bloquer un robot qui n'en donne pas. L'adresse n'est pas publiée mais reste dans leurs journaux. Pour ne pas
exposer l'adresse admin, définir `SEC_USER_AGENT` avec une adresse dédiée que l'on lit, par exemple
`Sillage/1.0 sillage@piperault.fr`.

## Collecte : deux boucles

- **Rapide** (toutes les `INGEST_INTERVAL_MIN` minutes, ~15 s) : Form 4/4A, Form 144, 13D/13G, élus, AMF, cours,
  stops, règle automatique, alertes, courbe des portefeuilles.
- **Lente** (toutes les 5 min, chaque tâche à son rythme) : tickers et capitalisations (24 h), rachats d'actions
  (2 h), calendrier des résultats (1 h), taux BCE (6 h), cours Euronext (25 min), BaFin (1 h), Sénat (1 h, si activé),
  13F (6 h), CUSIP (10 min), FINRA (1 h), backtest (import 24 h, cours par lots, rapport et score v2 6-24 h).

Premier démarrage d'une base vide : la boucle lente remonte progressivement 18 mois de rachats et de résultats et
2 ans de cours (~3 000 titres à ~2,5 s) : compter une demi-journée avant le premier rapport complet.

## Comportement en production

- Migrations SQL appliquées au démarrage (verrou `pg_advisory_lock`, nouvel essai pendant 60 s si la base n'est pas prête).
- `/health` fait un `SELECT 1` limité à 2 s. Base coupée → 503 sans planter ; reconnexion automatique quand elle revient.
- Journaux : une ligne par événement sur stdout ; journal d'accès maison **sans adresse IP**.
- Les en-têtes `X-Real-IP` et `X-Forwarded-Proto: https` du proxy Hangar sont utilisés pour la limite de tentatives de
  connexion (10 / 10 min par IP) et le cookie `Secure`.
- Arrêt propre sur SIGTERM (collecte arrêtée, pool fermé) en moins de 3 s.
- Collecte en tâche de fond toutes les `INGEST_INTERVAL_MIN` minutes ; l'administrateur peut la relancer depuis
  l'onglet **Compte → Relancer la collecte**, où l'état de chaque étape est affiché.

## Publier une version

Le dépôt GitHub est `jpiperault/sillage`. Un tag `v*` déclenche l'Action `.github/workflows/docker.yml` :
tests, puis construction `linux/amd64` et publication sur `ghcr.io/jpiperault/sillage:<tag sans v>`.

```bash
git tag v1.0.1 && git push origin v1.0.1
```

Puis, dans Hangar, passer la version de l'application à `1.0.1`.

## Accès à l'image privée

Sur le serveur Hangar, une fois (token GitHub avec la seule permission `read:packages`) :

```bash
docker login ghcr.io -u jpiperault
```

## Sauvegardes

Tout l'état (comptes, sessions, portefeuilles fictifs, données collectées) est dans PostgreSQL ; `/data` est vide.

```bash
pg_dump -Fc "$DATABASE_URL" > sillage-$(date +%F).dump
```

Restauration (base vide ou existante) :

```bash
pg_restore --clean --if-exists --no-owner --no-acl -d "$DATABASE_URL" sillage-AAAA-MM-JJ.dump
```

Les sessions restent valides après restauration tant que `JWT_SECRET` ne change pas. Les données collectées
peuvent être reconstruites par la collecte ; les comptes et portefeuilles fictifs, non.

## Essai local

```bash
docker compose -f docker-compose.local.yml up --build
```

Interface sur `http://127.0.0.1:8080/` (ou `SILLAGE_PORT=8090` si 8080 est déjà pris), compte
`admin@example.test` / `dev-admin-password`. Données dans `./.data/`.

Tests :

```bash
docker run --rm -v "$PWD/server":/src -w /src python:3.12-slim sh -c "pip install -q -r requirements.txt -r requirements-dev.txt && python -m pytest -q tests"
```

## Backtest du score

Onglet **Signaux → Ce que vaut le score**. Sillage importe les jeux de données trimestriels de la SEC (tous les Form 4,
~11 Mo par trimestre, un trimestre par cycle) et les cours de clôture Nasdaq sur 2 ans (150 titres par cycle, SPY
compris). Pour chaque achat ou vente d'initié : achat fictif à la clôture qui suit la déclaration, puis écart de
performance avec le S&P 500 à 1, 3 et 6 mois, par tranche de score et par critère (rôle, montant, délai, cluster,
plan 10b5-1, taille par rapport à la position, prix). Rapport recalculé une fois par jour. Il faut environ 20 cycles
(3 à 4 h) après le premier démarrage pour que tous les cours soient chargés ; le rapport s'affiche dès le premier calcul.

## Score v2

Onglet **Signaux → Score v2**. Une régression logistique est entraînée sur les achats d'initiés du backtest
(probabilité de battre sa référence de marché à 3 mois : S&P 500 au-dessus de 2 Md$ de capitalisation,
Russell 2000 en dessous). Validation sur les 30 % de signaux les plus récents, jamais vus à l'entraînement : le
modèle n'est utilisé que s'il fait mieux que le hasard (AUC ≥ 0,53 et meilleur quintile au-dessus de la moyenne).
Le score affiché devient alors le rang centile de cette probabilité ; le ticket montre la probabilité, les critères
qui pèsent le plus et le score v1 pour comparaison. Élus et Europe gardent le score v1.

## Résumé, entraînement, exports

- **Résumé** (premier onglet) : meilleurs achats des dernières 24 h, activité sur la watchlist et les positions,
  grosses ventes annoncées, résultats à venir, nouveaux 13F, performance des portefeuilles. En option, le même
  résumé chaque matin par Telegram.
- **Entraînement** : deux portefeuilles fictifs, « Mon portefeuille » (ordres à la main) et « Règle automatique »
  (copie des signaux au-dessus d'un score, part fixe par ligne, sortie à horizon ou au stop −8 %), chacun avec sa
  courbe comparée au S&P 500. Actions européennes : cours Euronext converti au taux BCE.
- **Exports CSV** (séparateur « ; », compatibles Excel) : signaux, journal fictif, rapport de backtest, 13F.

## Nouveautés v2.1

- **Tester une règle sur l'historique** (Entraînement → Règle automatique) : rejeu jour par jour des 18 mois
  d'achats d'initiés avec le score, la part par ligne, l'horizon, le stop et les frais choisis ; courbe comparée
  au S&P 500, pire baisse, part de trades gagnants. En score v2, seule la période de test du modèle est rejouée.
- **Comptes** (Compte → Comptes, admin) : désactiver / réactiver (sessions coupées), mot de passe temporaire
  affiché une fois, droits admin, suppression (avec portefeuilles et watchlist).
- **Santé** (onglet réservé à l'admin) : version, mémoire, taille de la base et des tables, files d'attente,
  sources bloquées, durée et erreurs de chaque tâche sur 24 h (historique gardé 7 jours).
- **Marché** : part des initiés acheteurs semaine par semaine (hors plans 10b5-1) avec le S&P 500, et par secteur
  (code SIC de la SEC, regroupé en 12 secteurs).
- **Entraînement** : vente partielle, stop réglable, stop suiveur (monte avec le plus haut), objectif de cours,
  note par ligne.
- **Alertes de cours** (Compte, fiche action) : au-dessus / en dessous d'un prix ; le titre passe en watchlist pour
  que son cours soit suivi ; notification Telegram si le compte est lié, rappel dans le Résumé.
- **Score v2 pour les élus et l'Europe** : même méthode, référence S&P 500 (élus) ou ETF CAC 40 (Europe), activé
  seulement quand au moins 300 achats mesurés valident le modèle ; l'historique AMF est remonté progressivement.
- **13F dans la convergence** : fonds suivis ayant ouvert ou renforcé la ligne au dernier trimestre déposé
  (au moins un signal récent d'une autre source reste exigé).
- **Réseau d'un initié** (clic sur son nom dans un ticket) : ses sociétés, rôles, achats/ventes, track record et
  les autres initiés des mêmes sociétés.

## Alertes Telegram

1. Dans Telegram, écrire à **@BotFather**, commande `/newbot`, choisir un nom : il donne un jeton `123456:ABC…`.
2. Le coller dans **Compte → Réglages → Jeton du bot Telegram**, avec l'adresse publique du site.
3. Chaque utilisateur : **Compte → Alertes Telegram → Lier Telegram**, puis « Démarrer » dans Telegram. Il choisit son
   score minimum, la watchlist et les stops fictifs. Les alertes partent à la fin de chaque cycle de collecte.

## Fiche action

Recherche « Ticker » dans la barre du haut, ou lien « fiche de l'action » dans un ticket : cours sur 1 an avec les
achats et ventes déclarés, tous les signaux, Form 144, 13D/13G, grands fonds actionnaires (13F), ventes à découvert.

## Dépannage Hangar

| Symptôme | Cause | Solution |
|---|---|---|
| `ghcr.io/jpiperault/sillage:latest: not found` | le tag `latest` n'est jamais publié | indiquer une version `x.y.z` |
| `…sillage:1.10: not found` | version mal saisie | les versions ont trois nombres : `1.2.0` |
| conteneur qui redémarre, logs `démarrage impossible : … manquante(s) : JWT_SECRET, ADMIN_EMAIL` | variables absentes du conteneur | les ajouter dans Réglages, enregistrer, puis **redéployer** la version (elles sont lues à la création du conteneur) |
| aucun compte pour se connecter | `ADMIN_PASSWORD` absente au premier démarrage | l'ajouter et redémarrer : le compte admin est créé s'il n'existe pas |
| supprimer puis recréer l'application | les variables peuvent être perdues | les vérifier dans Réglages avant de déployer |

Démarrage réussi : les logs montrent `migrations : N nouvelle(s)` puis `Sillage prêt sur le port 8080`, et `/health`
répond `ok`.

## Limites connues

- Élus : la Chambre vient du Greffe (PTR déposés en ligne, actions cotées seulement ; ~99 % des lignes lues sur 45 PTR
  testés, les lignes coupées entre deux pages du PDF sont perdues ; PTR papier scannés ignorés ; pas de parti politique).
  Le Sénat n'est couvert qu'avec `QUIVER_API_KEY`, source jamais testée avec une vraie clé.
- Form 144 : affiché seulement, sans score. Montant ignoré quand il implique plus de 50 000 $ par action (erreur de
  saisie dans le dépôt). Les dépôts papier (rares) ne sont pas lus.
- AMF : seules les acquisitions et cessions d'actions à prix non nul sont gardées (attributions gratuites, actions de
  performance, donations ignorées). Pas de cours en direct ni de copie fictive pour les actions françaises (portefeuille
  en dollars) ; montants en euros, mêmes seuils de score que pour les initiés américains. API BDIF non documentée.
- 13F : 2 derniers trimestres par fonds, rafraîchis toutes les 6 h ; retard légal jusqu'à 45 jours après la fin du
  trimestre. Les tickers arrivent progressivement (600 CUSIP par cycle via OpenFIGI, ~2 min) ; quelques titres restent
  sans ticker. Pas encore intégré à la convergence.
- FINRA : volume des ventes à découvert du jour (pas la position vendeuse totale), 45 jours gardés, information
  seulement, sans effet sur le score.
- Flux live : une ligne par déclaration et par code (plusieurs transactions regroupées, prix moyen pondéré) ; la
  taille par rapport à la position (« ligne augmentée de X % ») est affichée sans effet sur le score.
- Backtest : cours non officiels (Nasdaq) ; les titres radiés ou renommés n'ont pas de cours et sortent de l'échantillon
  (biais du survivant) ; les montants sont ceux déclarés, sans frais ni glissement.
- Alertes Telegram : non testées avec un vrai bot avant livraison (logique testée hors ligne).
- Sénat : écrit d'après le fonctionnement connu du site eFD, jamais exécuté (403 depuis le serveur de dev).
- Allemagne : pas de cours (Xetra hors Euronext), donc ni variation depuis le trade ni copie fictive.
- Contrats publics : recherche par nom de société (filiales au nom différent non comptées).
- Score v2 : calibré sur 18 mois seulement (avril 2025 - septembre 2026), sans frais ni glissement ; titres radiés
  absents (biais du survivant) ; à revalider à chaque nouveau rapport.
- Premier démarrage : ~470 PTR de l'année en file, lus par 150 à chaque cycle (environ 1 min par cycle).
- Cours non officiels : Yahoo, puis Nasdaq en secours (Yahoo renvoie 429 depuis certaines adresses IP de serveur).
- Pas encore d'alertes Telegram, de 13F ni de données AMF.
- Une même déclaration Form 4 peut produire plusieurs lignes (une par transaction).
- Ce n'est pas un conseil en investissement.
