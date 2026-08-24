# Enrichissement Excel d'entreprises via OpenAI Responses API

Ce projet enrichit un fichier Excel d'entreprises en recherchant sur internet, via un modele OpenAI avec outil de recherche web, un email, un telephone et un site web.

Le pipeline lit un classeur existant, traite les lignes par batch, ecrit les resultats directement dans le fichier source, puis sauvegarde apres chaque batch. Si une information n'est pas trouvee ou reste trop incertaine, la valeur ecrite est exactement `Non trouvé`.

## Prerequis

- Python 3.11+
- Un fichier Excel `.xlsx`
- Une cle API OpenAI valide

## Installation

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
copy .env.example .env
```

## Configuration

Renseigner le fichier `.env` :

```dotenv
INPUT_EXCEL_PATH=20ksocietes.xlsx
SHEET_NAME=Etablissements actifs (tous)
START_ROW=2
MAX_ROWS=
BATCH_SIZE=20
SAVE_EVERY_BATCH=true
SKIP_IF_FILLED=true
OVERWRITE_EXISTING=false
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1-mini
OPENAI_CA_BUNDLE=Zscaler Root CA.crt
REQUEST_TIMEOUT=90
MAX_RETRIES=3
RETRY_WAIT_SECONDS=5
SLEEP_BETWEEN_CALLS=1.0
SLEEP_BETWEEN_BATCHES=2.0
LOG_LEVEL=INFO
CREATE_BACKUP=true
```

## Colonnes Excel

Colonnes source lues :

- `C` (3) = SIRET
- `D` (4) = Raison sociale
- `G` (7) = Adresse
- `H` (8) = Code postal
- `I` (9) = Ville

Colonnes cibles ecrites :

- `P` (16) = Email
- `Q` (17) = Telephone
- `R` (18) = Site Web

Les entetes sont en ligne 1 et les donnees commencent en ligne 2.

## Lancement

Commande standard :

```bash
python -m app.main
```

Exemples avec surcharge CLI :

```bash
python -m app.main --file 20ksocietes.xlsx --sheet "Etablissements actifs (tous)"
python -m app.main --max-rows 10 --batch-size 5
python -m app.main --start-row 2 --max-rows 50 --batch-size 10
python -m app.main --overwrite-existing
python -m app.main --no-skip-if-filled
```

## Fonctionnement

1. Ouverture du fichier Excel d'origine.
2. Creation d'un backup avant toute modification si `CREATE_BACKUP=true`.
3. Lecture des lignes a partir de `START_ROW`.
4. Limitation eventuelle a `MAX_ROWS` pour un POC ou un traitement partiel.
5. Application des regles `OVERWRITE_EXISTING` puis `SKIP_IF_FILLED`.
6. Recherche web via OpenAI Responses API.
7. Parsing JSON robuste avec fallback complet sur `Non trouvé`.
8. Ecriture directe dans les colonnes `P/Q/R`.
9. Sauvegarde du meme fichier a la fin de chaque batch si `SAVE_EVERY_BATCH=true`.

## Regles metier

- Aucun recontrole de SIRET.
- Aucun score de confiance.
- Aucun matching complexe.
- Si une ligne n'a pas de SIRET ou pas de raison sociale, le script ecrit `Non trouvé`.
- Si l'appel API echoue, le script journalise l'erreur, ecrit `Non trouvé` et continue.
- Si le parsing JSON echoue, le script ecrit `Non trouvé`.

## Skip et overwrite

Priorite appliquee :

1. Si `OVERWRITE_EXISTING=true`, la ligne est toujours retraitee.
2. Sinon, si `SKIP_IF_FILLED=true` et que `P/Q/R` sont deja toutes remplies, la ligne est skippee.
3. Sinon, la ligne est traitee.

## Logging

Le projet produit des logs :

- en console
- dans `logs/enrichment.log`

Messages journalises :

- ouverture du fichier
- feuille selectionnee
- backup cree
- volume de lignes
- debut de batch
- ligne en cours
- skip
- enrichissement
- erreur
- sauvegarde
- resume final

## Backup automatique

Le script modifie le fichier source directement. Avant la premiere sauvegarde, il cree un backup horodate de type :

```text
20ksocietes.backup.YYYYMMDD_HHMMSS.xlsx
```

## Precautions avant execution

- Tester d'abord avec `MAX_ROWS=10` ou `MAX_ROWS=50`.
- Verifier le nom de feuille Excel.
- Verifier que les colonnes `P/Q/R` peuvent etre ecrasees selon votre mode choisi.
- Garder le fichier Excel ferme pendant l'execution pour eviter les verrous d'ecriture.

## Remarques OpenAI

Le service utilise `responses.create` avec l'outil `web_search` et un format de sortie `json_schema`. Le code reste prudent :

- extraction de `output_text` si disponible
- fallback vers l'analyse du payload brut
- tentative de `json.loads`
- extraction du premier bloc JSON si le modele renvoie du texte parasite
- fallback final vers `Non trouvé`

Pour un traitement volumique, `gpt-4.1-mini` est le choix par defaut recommande dans ce projet. Lors des tests reels ici, le certificat Zscaler a bien permis la connexion, et `gpt-4.1-mini` a retourne une reponse exploitable, alors que `gpt-5` restait bloque dans une boucle de recherche web avec reponses `incomplete`.

## Certificat entreprise / Zscaler

Si votre proxy TLS d'entreprise intercepte la connexion OpenAI, renseignez :

```dotenv
OPENAI_CA_BUNDLE=Zscaler Root CA.crt
```

Le projet charge alors explicitement ce certificat dans le client HTTP OpenAI via un contexte SSL dedie. Si `OPENAI_CA_BUNDLE` n'est pas renseigne, le code essaie automatiquement de detecter `Zscaler Root CA.crt` ou `zscaler_root_ra.crt` a la racine du projet.
