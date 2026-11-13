# Sillage — suivez l'argent informé

Portail web pour suivre les transactions déclarées des initiés américains (SEC Form 4, 13D/13G, et les
parlementaires via Quiver Quantitative si `QUIVER_API_KEY` est fournie), avec un score de copiabilité,
un ticket de copie manuelle pour son broker et un mode **Entraînement** en argent fictif.

Ce n'est pas un conseil en investissement.

- `server/` : FastAPI + PostgreSQL (psycopg 3), collecte en tâche de fond dans le même processus.
- `web/` : React 18 + Vite 6, servi par le backend.
- Déploiement, variables, sauvegardes : voir [DEPLOY.md](DEPLOY.md).
