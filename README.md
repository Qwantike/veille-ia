# Veille IA

Un script Python lit des flux RSS, note chaque article (poids de la source, mots-clés, fraîcheur),
garde les 10 meilleurs et écrit une page statique `docs/veille-AAAA-MM-JJ.html`.
`docs/index.html` liste les jours précédents.

## Lancer en local

    pip install -r requirements.txt
    python veille.py            # ouvre ensuite docs/index.html

## Automatiser sans serveur (GitHub Actions)

1. Crée un dépôt GitHub et pousse ce dossier.
2. Onglet Actions > "veille" > Run workflow pour un premier test. Ensuite, un run par jour.
3. Pour lire la page en ligne : Settings > Pages > Deploy from a branch > `main` / dossier `/docs`.
   GitHub Pages sur un dépôt privé demande un compte payant ; sinon, clone le dépôt
   et ouvre `docs/index.html`, ou publie `docs/` sur Cloudflare Pages / Netlify (gratuits).

## Affiner

Tout se règle dans `config.yaml` : sources, poids, mots-clés, plafonds, score minimum.

    python veille.py --report 14

affiche, par source, combien d'articles ont été candidats et retenus sur 14 jours,
et signale celles à revoir (jamais retenues ou en erreur).
Chaque article montre aussi les mots-clés qui l'ont fait remonter.
