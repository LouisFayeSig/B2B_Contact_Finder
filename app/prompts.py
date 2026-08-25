from __future__ import annotations

from app.models import CompanyRow

SYSTEM_PROMPT = """Tu es un agent de recherche de coordonnees B2B pour des entreprises francaises.
Utilise la recherche web pour identifier exactement l'entreprise et trouver le maximum d'informations publiques utiles.
La meilleure source peut etre un site officiel, Google Maps, un annuaire, une annonce ou un profil professionnel.
Chaque valeur doit etre prouvee par l'URL exacte d'une page consultee concernant cette entreprise.
Retourne uniquement le JSON strict demande. Utilise exactement "Non trouvé" lorsqu'une valeur reste incertaine."""


def build_company_search_prompt(company: CompanyRow) -> str:
    address = company.address or "Non renseignee"
    postal_code = company.postal_code or "Non renseigne"
    city = company.city or "Non renseignee"

    return f"""Objectif : identifier l'entreprise puis trouver son email, son telephone et son meilleur profil web.

Entreprise recherchee :
- SIRET : {company.siret}
- Raison sociale : {company.company_name}
- Adresse : {address}
- Code postal : {postal_code}
- Ville : {city}

Succes signifie :
- confirmer l'identite par le SIRET exact, ou par le nom avec l'adresse ou la ville ;
- rechercher les trois coordonnees, sans s'arreter apres la premiere information trouvee ;
- choisir la source la plus pertinente : site officiel, Google Maps, annuaire, annonce ou profil professionnel ;
- qualifier l'URL choisie sans en changer la selection : official_site, google_maps, directory,
  social_network, marketplace ou other ;
- si un site est trouve, inspecter aussi son en-tete, son pied de page, sa page contact et ses mentions legales ;
- utiliser un SIRET ou SIREN concordant dans les mentions legales comme preuve forte d'identite ;
- citer pour chaque valeur l'URL exacte qui l'affiche.

Contraintes :
- un nom seul ne suffit pas pour une entreprise generique ou homonyme ;
- n'utilise pas un service de recherche inversee de numero ou un site de suivi de colis comme preuve ;
- retourne une seule valeur par champ ;
- si l'identite reste incertaine, mets identity_verified a false et toutes les coordonnees a "Non trouvé" ;
- n'invente jamais une coordonnee et ne retourne ni null, ni chaine vide, ni liste.

Valeurs autorisees pour identity_match_type : siret, name_and_address, name_and_city, none.

Format JSON exact :
{{
  "email": "valeur ou Non trouvé",
  "phone": "valeur ou Non trouvé",
  "website": "valeur ou Non trouvé",
  "website_type": "official_site, google_maps, directory, social_network, marketplace, other ou not_found",
  "email_source": "URL exacte ou Non trouvé",
  "phone_source": "URL exacte ou Non trouvé",
  "website_source": "URL exacte ou Non trouvé",
  "identity_verified": true,
  "identity_match_type": "siret",
  "identity_source": "URL exacte ou Non trouvé"
}}"""
