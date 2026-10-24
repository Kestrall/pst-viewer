# PST Viewer

Visionneuse locale pour archives Outlook `.pst`, sous Linux, sans Outlook.
Un écran d'accueil permet de choisir l'archive, puis l'application affiche l'arborescence des dossiers, les mails (HTML ou texte), les pièces jointes et les événements du calendrier, avec une recherche plein texte.

## Prérequis

- Python 3.9+ (bibliothèque standard uniquement, aucun `pip install`)
- `readpst` : `sudo apt install pst-utils`

## Utilisation

```bash
git clone https://github.com/Kestrall/pst-viewer.git
cd pst-viewer
python3 pst_viewer.py
```

Le navigateur s'ouvre sur l'écran d'accueil (http://127.0.0.1:8765/) :

1. choisissez le fichier `.pst` dans l'explorateur intégré (raccourcis Documents, Téléchargements, supports amovibles…), ou collez son chemin ;
2. la première ouverture extrait et indexe l'archive, avec une barre de progression ;
3. la visionneuse s'affiche. « Changer d'archive » ramène à l'accueil, où les archives déjà indexées apparaissent dans « Archives récentes » et s'ouvrent instantanément.

Ouverture directe sans passer par l'accueil :

```bash
python3 pst_viewer.py ~/Documents/archive.pst
```

Options : `--port 9000`, `--no-browser`, `--rebuild` (réextraire et réindexer).

## Fonctionnalités

- Dossiers Outlook en tête, dossiers d'archive par année (récentes d'abord), compteurs
- Recherche dans l'objet, l'expéditeur, les destinataires et le contenu, insensible aux accents, par préfixe, dans le dossier courant ou partout
- Mails HTML affichés dans une iframe isolée : aucun script exécuté, images distantes (traceurs) bloquées par défaut, images intégrées (`cid:`) affichées
- Pièces jointes téléchargeables, export `.eml`, en-têtes complets
- Événements du calendrier (`.ics`)
- Navigation clavier (↑/↓ ou j/k), thème clair/sombre automatique, affichage mobile

## Fonctionnement

```
.pst ──readpst──▶ ~/.cache/pst-viewer/<empreinte>/mail/**/*.eml
                                   │
                         indexation (multiprocessus)
                                   ▼
                     index.sqlite (métadonnées + FTS5)
                                   │
              serveur HTTP local (127.0.0.1) ──▶ interface web
```

- L'archive est extraite avec `readpst -e -j 0`. Le mode parallèle de readpst, actif par défaut, saute des messages de façon aléatoire (environ 4 % sur une archive de 8 Go) : l'extraction est donc volontairement séquentielle.
- Le cache est identifié par le chemin, la taille et la date de modification du `.pst` : une archive modifiée est réindexée automatiquement.
- Le cache occupe à peu près la taille du `.pst`. Supprimez `~/.cache/pst-viewer/` pour libérer la place.

## Sécurité

- Le serveur n'écoute que sur `127.0.0.1` et refuse les requêtes dont l'en-tête `Host` n'est pas local (protection contre le DNS rebinding).
- L'ouverture d'une archive passe par un `POST` JSON, qu'un formulaire d'un site tiers ne peut pas envoyer.
- L'explorateur de fichiers ne liste que des dossiers et des fichiers `.pst`, et n'accepte d'ouvrir que des `.pst`.
- Le HTML des mails est servi avec une Content-Security-Policy stricte (`default-src 'none'`) et affiché dans une iframe sandboxée sans scripts.

## Licence

MIT
