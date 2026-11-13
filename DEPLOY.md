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
| Version | `1.0.0` (jamais `latest`) |
| Port interne | `8080` |
| Healthcheck | `GET /health` → `200 ok` (503 si la base est inaccessible) |
| Mémoire | 384 Mo (mesuré : ~50 Mo au repos, ~60-70 Mo pendant la collecte) |
| PostgreSQL | oui (`DATABASE_URL` injectée par Hangar) |
| Volume `/data` | créé et attribué à l'utilisateur 10001, **non utilisé** (tout est en base) |
| Android | hors périmètre |

## Variables d'environnement

| Variable | Obligatoire | Rôle |
|---|---|---|
| `DATABASE_URL` | oui (Hangar) | `postgresql://user:mdp@hôte:5432/base` |
| `JWT_SECRET` | oui | ≥ 32 caractères : `openssl rand -hex 32`. Le changer déconnecte tout le monde. |
| `ADMIN_EMAIL` | oui | Adresse du compte administrateur (réservée : personne d'autre ne peut s'inscrire avec). |
| `ADMIN_PASSWORD` | 1er démarrage | Crée le compte admin s'il n'existe pas : `openssl rand -base64 24`. Ignorée ensuite (changer le mot de passe dans l'onglet Compte). |
| `SEC_USER_AGENT` | non | Défaut `Sillage/1.0 <ADMIN_EMAIL>`. La SEC exige un contact joignable. |
| `HOUSE_ENABLED` | non | `true` par défaut : transactions des représentants lues dans les déclarations officielles du Greffe de la Chambre (gratuit). |
| `QUIVER_API_KEY` | non | Ajoute Quiver Quantitative (payant), surtout utile pour le Sénat. Si `HOUSE_ENABLED` est actif, Quiver ne fournit que le Sénat (pas de doublon). |
| `FORM144_ENABLED` | non | `true` par défaut : ventes annoncées par les initiés (SEC Form 144). |
| `AMF_ENABLED` | non | `true` par défaut : déclarations des dirigeants français (AMF, base BDIF). |
| `FUNDS_13F` | non | Fonds suivis via leurs 13F : vide = 18 grands fonds par défaut (Berkshire, Pershing Square, Baupost…), liste de CIK séparés par des virgules pour choisir, `off` pour désactiver. CUSIP → ticker via OpenFIGI (gratuit). |
| `FINRA_ENABLED` | non | `true` par défaut : part quotidienne des ventes à découvert (FINRA), affichée dans le ticket. |
| `ALLOW_SIGNUP` | non | `true` par défaut ; `false` ferme les inscriptions. |
| `INGEST_ENABLED` | non | `true` par défaut. |
| `INGEST_INTERVAL_MIN` | non | 10 par défaut. |
| `BACKFILL_DAYS` | non | Jours ouvrés rattrapés via l'index quotidien SEC, 3 par défaut. |
| `PAPER_START_CASH` | non | Capital fictif de départ, 25 000 $ par défaut. |

Une variable obligatoire absente arrête le démarrage avec un message qui la nomme et le code de sortie 2,
par exemple : `démarrage impossible : variable(s) d'environnement obligatoire(s) manquante(s) : JWT_SECRET`.

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
- Premier démarrage : ~470 PTR de l'année en file, lus par 150 à chaque cycle (environ 1 min par cycle).
- Cours non officiels : Yahoo, puis Nasdaq en secours (Yahoo renvoie 429 depuis certaines adresses IP de serveur).
- Pas encore d'alertes Telegram, de 13F ni de données AMF.
- Une même déclaration Form 4 peut produire plusieurs lignes (une par transaction).
- Ce n'est pas un conseil en investissement.
