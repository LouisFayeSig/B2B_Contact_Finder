from __future__ import annotations

from app.models import CompanyRow

SYSTEM_PROMPT = """Tu es un agent de recherche B2B.
Tu recherches sur internet les coordonnees d'une entreprise francaise.
Tu dois retourner uniquement un JSON strict et valide.
Ne retourne aucun commentaire, aucune explication, aucun markdown.
Si une information n'est pas trouvee avec une certitude raisonnable, retourne exactement "Non trouvé".
Ne retourne jamais null, jamais de chaine vide et jamais de liste."""


def build_company_search_prompt(company: CompanyRow) -> str:
    address = company.address or "Non renseigne"
    postal_code = company.postal_code or "Non renseigne"
    city = company.city or "Non renseignee"

    return f"""Tu dois rechercher sur internet les coordonnées de l’entreprise suivante.
Retourne uniquement un JSON strict avec les champs email, phone, website.

Entreprise :
- SIRET : {company.siret}
- Raison sociale : {company.company_name}
- Adresse : {address}
- Code postal : {postal_code}
- Ville : {city}

Consignes :
- utilise les informations fournies pour identifier la bonne entreprise
- cherche un email de contact, un telephone, et le site web
- privilegie le site officiel de l’entreprise si possible
- sinon tente de trouver les informations en variants les approches (nom dirigeant + siret, adresse + nom d'entreprise, etc)
- si necessaire, utilise une page contact, mentions legales, ou annuaire professionnel fiable
- retourne une seule valeur par champ
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
  "website": "valeur ou Non trouvé"
}}"""
