from __future__ import annotations

from app.models import CompanyRow

SYSTEM_PROMPT = """Tu es un agent de recherche B2B.
Tu recherches sur internet les coordonnees d'une entreprise francaise.
Tu dois d'abord confirmer que les pages consultees concernent exactement l'entreprise demandee.
Tu dois retourner uniquement un JSON strict et valide.
Ne retourne aucun commentaire, aucune explication, aucun markdown.
Si une information n'est pas trouvee avec une certitude raisonnable, retourne exactement "Non trouvé".
Chaque coordonnee trouvee doit etre accompagnee de l'URL exacte de la page qui la prouve.
Ne retourne jamais null, jamais de chaine vide et jamais de liste."""


def build_company_search_prompt(company: CompanyRow) -> str:
    address = company.address or "Non renseigne"
    postal_code = company.postal_code or "Non renseigne"
    city = company.city or "Non renseignee"

    return f"""Tu dois rechercher sur internet les coordonnées de l’entreprise suivante.
Retourne uniquement le JSON strict demande ci-dessous.

Entreprise :
- SIRET : {company.siret}
- Raison sociale : {company.company_name}
- Adresse : {address}
- Code postal : {postal_code}
- Ville : {city}

Consignes :
- verifie d'abord l'identite de l'entreprise avant de retenir une coordonnee
- identity_verified peut etre true uniquement si une source contient le SIRET exact,
  ou si le nom et l'adresse sont concordants
- pour un nom generique ou homonyme, le nom seul ne suffit jamais
- identity_match_type doit valoir siret, name_and_address, name_and_city ou none
- identity_source doit etre l'URL exacte de la meilleure preuve d'identite
- cherche un email de contact, un telephone, et le site web
- privilegie le site officiel de l’entreprise si possible
- sinon varie les approches de recherche
- utilise par exemple le nom du dirigeant avec le SIRET, ou l'adresse avec le nom de l'entreprise
- si necessaire, utilise une page contact, mentions legales, ou annuaire professionnel fiable
- retourne une seule valeur par champ
- email_source, phone_source et website_source doivent contenir l'URL exacte qui prouve chaque valeur
- une URL de source doit provenir d'une page reellement consultee pendant cette recherche
- si aucune source precise ne prouve une valeur, retourne Non trouvé pour la valeur et sa source
- si l'identite n'est pas suffisamment verifiee, retourne identity_verified=false
  et Non trouvé pour toutes les coordonnees
- si l’information n’est pas trouvée avec une certitude raisonnable, retourne exactement "Non trouvé"
- ne retourne jamais null
- ne retourne jamais une chaine vide
- ne retourne jamais de liste
- ne retourne jamais de commentaire
- ne retourne aucun texte hors JSON

Format attendu exact :
{{
  "email": "valeur ou Non trouvé",
  "phone": "valeur ou Non trouvé",
  "website": "valeur ou Non trouvé",
  "email_source": "URL exacte ou Non trouvé",
  "phone_source": "URL exacte ou Non trouvé",
  "website_source": "URL exacte ou Non trouvé",
  "identity_verified": true,
  "identity_match_type": "siret ou name_and_address ou name_and_city ou none",
  "identity_source": "URL exacte ou Non trouvé"
}}"""
