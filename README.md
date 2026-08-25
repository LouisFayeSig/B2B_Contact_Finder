# Enrichissement Excel d'entreprises via Microsoft Foundry

Ce projet enrichit un fichier Excel d'entreprises en recherchant sur internet, via un deploiement Azure OpenAI dans Microsoft Foundry avec l'outil `web_search`, un email, un telephone et un site web. Une seconde passe deterministe complete les champs oublies depuis le site ou le profil trouve. Chaque ligne conserve un statut de traitement. La collecte des sources web est facultative.

Le pipeline lit un classeur existant, traite les lignes par batch, ecrit les resultats directement dans le fichier source, puis sauvegarde apres chaque batch. Si une information n'est pas trouvee ou reste trop incertaine, la valeur ecrite est exactement `Non trouvé`.

## Prerequis

- Python 3.11+
- Un fichier Excel `.xlsx`
- Un projet Microsoft Foundry et un deploiement Azure OpenAI compatible avec la Responses API
- Une authentification Microsoft Entra ID ou, pour un POC, une cle API Azure

## Installation

```bash
poetry install
copy .env.example .env
```

Toutes les dependances runtime et de developpement sont declarees dans `pyproject.toml` et verrouillees dans `poetry.lock`.

Pour l'authentification Entra ID en local :

```bash
az login
```

## Configuration

Renseigner le fichier `.env` :

```dotenv
INPUT_EXCEL_PATH=data/20ksocietes.xlsx
SHEET_NAME=Etablissements actifs (tous)
START_ROW=2
MAX_ROWS=
BATCH_SIZE=100
SAVE_EVERY_BATCH=true
SKIP_IF_FILLED=true
OVERWRITE_EXISTING=false
AZURE_FOUNDRY_ENDPOINT=https://<resource>.openai.azure.com/openai/v1/
AZURE_FOUNDRY_API_KEY=
AZURE_FOUNDRY_MODEL_DEPLOYMENT=gpt-5.6-luna
AZURE_FOUNDRY_REASONING_EFFORT=none
AZURE_FOUNDRY_CA_BUNDLE=Zscaler Root CA.crt
WEB_SEARCH_CONTEXT_SIZE=default
SEARCH_AUDIT_ENABLED=false
SITE_EXTRACTION_ENABLED=true
SITE_EXTRACTION_MAX_PAGES=6
SITE_EXTRACTION_TIMEOUT=12
SITE_EXTRACTION_MAX_BYTES=2000000
MAX_WORKERS=4
PROCESSING_JOURNAL_PATH=logs/enrichment.pending.jsonl
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
- `Z` (26) = Statut (`success`, `not_found`, `technical_error`, `invalid_input`)
- `AA` (27) = Toutes les pages consultees, au format JSON
- `AB` (28) = Source de l'email
- `AC` (29) = Source du telephone
- `AD` (30) = Source du site web
- `AE` (31) = Source de la preuve d'identite
- `AF` (32) = Type de rapprochement (`siret`, `name_and_address`, `name_and_city`, `none`)
- `AG` (33) = Horodatage UTC de la recherche
- `AH` (34) = Nom du deploiement Foundry demande
- `AI` (35) = Snapshot du modele retourne par Azure
- `AJ` (36) = Identifiant de reponse Azure
- `AK` (37) = Tokens d'entree
- `AL` (38) = Tokens de sortie
- `AM` (39) = Total de tokens
- `AN` (40) = Nombre d'appels de recherche web
- `AO` (41) = Pages lues par l'extracteur deterministe
- `AP` (42) = Nombre de champs completes sans nouvel appel au modele
- `AQ` (43) = Methode d'extraction de l'email
- `AR` (44) = Methode d'extraction du telephone
- `AS` (45) = Methode de verification d'identite
- `AT` (46) = Presence d'un pop-up de mentions legales dynamique

Les colonnes `AA:AT` ne sont creees et renseignees que lorsque l'audit est active.

Les entetes sont en ligne 1 et les donnees commencent en ligne 2.

## Lancement

Commande standard :

```bash
python -m app.main
```

Exemples avec surcharge CLI :

```bash
python -m app.main --file data/20ksocietes.xlsx --sheet "Etablissements actifs (tous)"
python -m app.main --max-rows 10 --batch-size 5
python -m app.main --start-row 2 --max-rows 50 --batch-size 10
python -m app.main --start-row 12 --max-rows 100 --batch-size 20 --workers 4 --audit
python -m app.main --search-context-size low --audit
python -m app.main --site-extraction
python -m app.main --no-site-extraction
python -m app.main --no-audit --workers 1
python -m app.main --overwrite-existing
python -m app.main --no-skip-if-filled
```

## Fonctionnement

1. Ouverture du fichier Excel d'origine.
2. Creation d'un backup avant toute modification si `CREATE_BACKUP=true`.
3. Lecture des lignes a partir de `START_ROW`.
4. Limitation eventuelle a `MAX_ROWS` pour un POC ou un traitement partiel.
5. Application des regles de statut, `OVERWRITE_EXISTING` puis `SKIP_IF_FILLED`.
6. Recherches web en parallele via la Responses API du deploiement Microsoft Foundry, dans la limite de `MAX_WORKERS`.
7. Verification de l'identite, parsing JSON et validation des coordonnees.
8. Si un email ou telephone manque mais qu'un site ou profil a ete trouve, lecture directe de la page puis, si necessaire, des liens contact et mentions legales du meme domaine.
9. Lorsque l'audit est active, controle que chaque preuve cite une page reellement consultee.
10. Ecriture sequentielle et non destructive dans Excel, avec journalisation durable avant chaque modification.
11. Sauvegarde du meme fichier a la fin de chaque batch si `SAVE_EVERY_BATCH=true`, puis purge du journal confirme.

## Regles metier

- Aucun score de confiance.
- Un SIRET d'entree doit contenir exactement 14 chiffres ; sinon la ligne devient `invalid_input`.
- Avant de conserver un contact, le modele doit confirmer l'identite par SIRET exact ou par concordance du nom et de l'adresse/ville.
- Un nom seul, generique ou homonyme, n'est jamais une preuve suffisante.
- Si l'identite n'est pas verifiee, toutes les coordonnees sont forcees a `Non trouve`.
- En mode audit, une preuve qui ne figure pas parmi les pages effectivement consultees est rejetee.
- Si l'appel API ou le parsing echoue, son statut devient `technical_error`, les coordonnees existantes restent intactes et la ligne sera retentee au prochain lancement.
- Un resultat valide mais vide devient `not_found`.
- Les emails, telephones et URL invalides sont normalises en `Non trouvé` avant ecriture.
- Sans `OVERWRITE_EXISTING`, seules les cellules vides ou egales a `Non trouvé` sont completees ; les contacts existants sont preserves.
- La recherche LLM reste generaliste : un site officiel, Google Maps, un annuaire, une annonce ou un profil professionnel peuvent etre retenus.
- L'extracteur direct ne remplace jamais une coordonnee deja trouvee par le modele.
- Il reconnait les liens `mailto:` et `tel:`, le texte visible et les donnees JSON-LD. Les numeros francais sont normalises au format `0X XX XX XX XX`.
- Un email inverse utilise comme protection anti-robot est remis dans le bon sens lorsqu'il forme ensuite une adresse valide.
- Les pages contact ou mentions legales ne sont suivies que sur le meme domaine et dans la limite configuree.
- Une concordance SIRET ou SIREN dans les mentions legales renforce la preuve d'identite. Un identifiant contradictoire proche du nom de l'entreprise bloque les contacts de la page.
- Les pop-ups legaux charges uniquement par JavaScript sont signales dans l'audit ; aucun navigateur headless couteux n'est lance par defaut.
- Les URL locales, privees, avec identifiants ou ports inhabituels sont refusees avant telechargement.

## Skip et overwrite

Priorite appliquee :

1. Si `OVERWRITE_EXISTING=true`, la ligne est toujours retraitee et les valeurs sont remplacees.
2. Une ligne `technical_error` est toujours retentee.
3. Une ligne `success` ou `not_found` est skippee si `SKIP_IF_FILLED=true`.
4. Une ligne `invalid_input` est retentee des que son SIRET et sa raison sociale ont ete corriges.
5. Pour un ancien classeur sans statut, une ligne dont `P/Q/R` sont toutes remplies reste skippee.
6. Une ligne partielle est traitee, mais ses cellules deja renseignees sont preservees.

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
- reprise eventuelle du journal
- resume final

## Reprise apres interruption

Avant toute modification d'une ligne, le resultat est ajoute a
`PROCESSING_JOURNAL_PATH`. Le fichier est force sur disque, puis Excel est mis a
jour. Apres une sauvegarde Excel reussie, le journal est supprime.

Au lancement suivant, un journal encore present est rejoue automatiquement avant
de reprendre les recherches. Chaque entree est comparee au SIRET et a la raison
sociale de la ligne actuelle ; en cas de decalage, le programme s'arrete sans
appliquer le journal afin d'eviter une ecriture sur la mauvaise entreprise.

## Backup automatique

Le script modifie le fichier source directement. Avant la premiere sauvegarde, il cree un backup horodate de type :

```text
data/20ksocietes.backup.YYYYMMDD_HHMMSS.xlsx
```

## Precautions avant execution

- Tester d'abord avec `MAX_ROWS=10` ou `MAX_ROWS=50`.
- Commencer avec `MAX_WORKERS=4`; reduire a `1` ou `2` en cas de limitation de debit Azure.
- Verifier le nom de feuille Excel.
- Verifier que les colonnes `P/Q/R` peuvent etre ecrasees selon votre mode choisi.
- Garder le fichier Excel ferme pendant l'execution pour eviter les verrous d'ecriture.

## Microsoft Foundry et choix du modele

Le service utilise le SDK OpenAI avec l'endpoint compatible `/openai/v1/` de Microsoft Foundry, `responses.create`, l'outil `web_search` et un format de sortie `json_schema`.

Le projet utilise un seul deploiement, configure par `AZURE_FOUNDRY_MODEL_DEPLOYMENT`. Le choix par defaut est `gpt-5.6-luna` avec `reasoning.effort=none` : il correspond au niveau economique/nano de la gamme actuelle et convient a une extraction structuree volumique. La valeur configuree est le **nom du deploiement Azure**, qui peut etre different de l'ID du modele.

L'endpoint peut etre :

- un endpoint Azure OpenAI, par exemple `https://<resource>.openai.azure.com/openai/v1/` ;
- un endpoint de projet Foundry se terminant par `/openai/v1/`.

Si `AZURE_FOUNDRY_API_KEY` est vide, le service utilise `DefaultAzureCredential`. En local, cette chaine peut reutiliser la session `az login`; en production, une identite managee est recommandee.

Le code reste prudent :

- extraction de `output_text` si disponible
- inclusion de `web_search_call.action.sources` seulement si `SEARCH_AUDIT_ENABLED=true`
- fallback vers l'analyse du payload brut
- tentative de `json.loads`
- extraction du premier bloc JSON si le modele renvoie du texte parasite
- statut `technical_error` si la reponse reste inexploitable, afin de permettre une reprise

Lorsque l'audit est desactive, les citations de navigation ne sont ni extraites ni ecrites dans Excel. Il peut etre active ponctuellement avec `--audit`, sans modifier `.env`. L'outil de recherche web reste necessaire au fonctionnement metier.

## Benchmark cout / qualite des modeles

Le benchmark relit des lignes deja terminees (`success` ou `not_found`) et utilise
leurs valeurs `P/Q/R` comme reference. Il appelle chaque deploiement candidat sur
exactement les memes entreprises, sans modifier le classeur source.

Exemple sur 20 entreprises, avec deux deploiements Azure :

```bash
poetry run python -m app.benchmark --deployment gpt-5.6-luna --deployment gpt-5.4-nano --start-row 12 --max-rows 20 --workers 2 --search-context-size low --audit
```

Les valeurs de `--deployment` sont les noms exacts des deploiements crees dans
Microsoft Foundry. Elles peuvent etre differentes des identifiants des modeles.

Deux rapports horodates sont produits dans `benchmarks/` :

- un CSV detaille par entreprise et par modele ;
- un JSON de synthese avec taux de succes, concordance exacte des champs deja
  trouves, nouveaux contacts, erreurs, latence, tokens et appels web.

`WEB_SEARCH_CONTEXT_SIZE=low` limite le contexte des resultats de recherche remis
au modele. C'est un levier de cout a tester en priorite pour cette extraction
courte. La valeur `default` conserve le comportement actuel ; `medium` et `high`
augmentent le contexte disponible.

Le benchmark mesure l'usage renvoye par Azure, mais ne pretend pas recalculer la
facture : les tarifs et les noms de compteurs Azure peuvent differer de ceux de
l'API OpenAI directe. Comparer le rapport au cout observe dans Azure Cost
Management sur la meme fenetre d'execution.

## Benchmark gratuit de l'extraction directe

Ce benchmark relit uniquement les lignes existantes qui possedent deja un site
mais auxquelles il manque un email ou un telephone. Il n'appelle pas Azure et ne
modifie pas le classeur :

```bash
poetry run python -m app.site_benchmark --max-rows 30 --workers 4
```

Il produit dans `benchmarks/` un CSV detaille et un JSON indiquant notamment les
emails et telephones recuperes, les pages consultees, les preuves SIRET/SIREN,
les pop-ups legaux detectes et la latence. Les nouvelles valeurs doivent etre
controlees sur l'URL source avant de servir de reference qualite.

## Certificat entreprise / Zscaler

Si votre proxy TLS d'entreprise intercepte la connexion OpenAI, renseignez :

```dotenv
AZURE_FOUNDRY_CA_BUNDLE=Zscaler Root CA.crt
```

Le projet charge alors explicitement ce certificat dans le client HTTP vers Foundry via un contexte SSL dedie. Si `AZURE_FOUNDRY_CA_BUNDLE` n'est pas renseigne, le code essaie automatiquement de detecter `Zscaler Root CA.crt` ou `zscaler_root_ra.crt` a la racine du projet.

## Tests et qualite

```bash
poetry run pytest
poetry run ruff check app tests
poetry run mypy app
```
