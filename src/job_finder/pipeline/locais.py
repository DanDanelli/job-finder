"""Interpretação de locais em texto livre e elegibilidade de vagas remotas.

Um local vira uma lista de códigos: países ISO alfa-2 ("PT") ou regiões ("EUROPE").
"""

import re
from functools import cache

import pycountry

from job_finder.texto import chave

WORLDWIDE = "WORLDWIDE"

# fmt: off
_UE = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
    "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
}
_EUROPA = _UE | {
    "AL", "AD", "BA", "BY", "CH", "FO", "GB", "GI", "IS", "LI", "MC", "MD", "ME", "MK",
    "NO", "RS", "SM", "UA", "VA", "XK",
}
_LATAM = {
    "AR", "BO", "BR", "CL", "CO", "CR", "CU", "DO", "EC", "GT", "HN", "MX", "NI", "PA",
    "PE", "PR", "PY", "SV", "UY", "VE",
}
_AMERICA_DO_NORTE = {"US", "CA", "MX"}
_APAC = {
    "AU", "BD", "CN", "FJ", "HK", "ID", "IN", "JP", "KH", "KR", "LK", "MM", "MN", "MY",
    "NP", "NZ", "PH", "PK", "SG", "TH", "TW", "VN",
}
_ORIENTE_MEDIO = {"AE", "BH", "IL", "IQ", "JO", "KW", "LB", "OM", "QA", "SA", "TR"}
_AFRICA = {
    "DZ", "AO", "CM", "CI", "EG", "ET", "GH", "KE", "MA", "MU", "MZ", "NG", "RW", "SN",
    "TN", "TZ", "UG", "ZA", "ZM", "ZW",
}
# fmt: on

REGIOES: dict[str, frozenset[str]] = {
    "EU": frozenset(_UE),
    "EUROPE": frozenset(_EUROPA),
    "LATAM": frozenset(_LATAM),
    "NORTH_AMERICA": frozenset(_AMERICA_DO_NORTE),
    "AMERICAS": frozenset(_LATAM | _AMERICA_DO_NORTE),
    "APAC": frozenset(_APAC),
    "EMEA": frozenset(_EUROPA | _ORIENTE_MEDIO | _AFRICA),
}

_ALIASES_REGIAO = {
    "worldwide": WORLDWIDE,
    "anywhere": WORLDWIDE,
    "global": WORLDWIDE,
    "qualquer lugar": WORLDWIDE,
    "europe": "EUROPE",
    "europa": "EUROPE",
    "eu": "EU",
    "european union": "EU",
    "uniao europeia": "EU",
    "latam": "LATAM",
    "latin america": "LATAM",
    "south america": "LATAM",
    "america latina": "LATAM",
    "north america": "NORTH_AMERICA",
    "america do norte": "NORTH_AMERICA",
    "americas": "AMERICAS",
    "apac": "APAC",
    "asia pacific": "APAC",
    "asia": "APAC",
    "emea": "EMEA",
}

_ALIASES_PAIS = {
    "usa": "US",
    "us": "US",
    "u s": "US",
    "u s a": "US",
    "united states": "US",
    "estados unidos": "US",
    "eua": "US",
    "uk": "GB",
    "england": "GB",
    "great britain": "GB",
    "reino unido": "GB",
    "brasil": "BR",
    "alemanha": "DE",
    "espanha": "ES",
    "franca": "FR",
    "italia": "IT",
    "holanda": "NL",
    "paises baixos": "NL",
    "the netherlands": "NL",
    "irlanda": "IE",
    "suica": "CH",
    "belgica": "BE",
    "canada": "CA",
    "mexico": "MX",
    "colombia": "CO",
    "russia": "RU",
    "south korea": "KR",
    "uae": "AE",
    # Nomes locais comuns em vagas europeias
    "deutschland": "DE",
    "osterreich": "AT",
    "schweiz": "CH",
    "suisse": "CH",
    "nederland": "NL",
    "espana": "ES",
    "belgie": "BE",
    "belgique": "BE",
    "polska": "PL",
    "sverige": "SE",
    "danmark": "DK",
    "norge": "NO",
}

# Palavras que acompanham o local mas não mudam o significado.
_RUIDO = re.compile(r"\b(only|timezones?|time zones?|based|remote|remoto|apenas|somente)\b")
_SEPARADORES = re.compile(r"[,;/|()]|\s+[-\u2013]\s+|\s+(?:or|and|ou|e)\s+|\s+&\s+")


@cache
def _pais(nome: str) -> str | None:
    if nome in _ALIASES_PAIS:
        return _ALIASES_PAIS[nome]
    try:
        return str(pycountry.countries.lookup(nome).alpha_2)
    except LookupError:
        return None


def interpretar_local(texto: str | None) -> list[str]:
    """Converte "LATAM, Europe, USA" em ["LATAM", "EUROPE", "US"].

    Trechos não reconhecidos (cidades, por exemplo) são ignorados. Lista vazia
    significa que não deu para interpretar nada.
    """
    if not texto:
        return []
    codigos: list[str] = []
    for pedaco in _SEPARADORES.split(texto):
        if not pedaco:
            continue
        nome = chave(_RUIDO.sub(" ", chave(pedaco)))
        if not nome:
            continue
        codigo = _ALIASES_REGIAO.get(nome) or _pais(nome)
        if codigo and codigo not in codigos:
            codigos.append(codigo)
    return codigos


def elegivel(locais: list[str], paises_da_pessoa: list[str]) -> bool | None:
    """A pessoa, morando em algum de `paises_da_pessoa`, pode aceitar a vaga?

    Devolve None quando os locais da vaga são desconhecidos.
    """
    if not locais:
        return None
    if WORLDWIDE in locais:
        return True
    for local in locais:
        cobertos = REGIOES.get(local, frozenset({local}))
        if any(p in cobertos for p in paises_da_pessoa):
            return True
    return False
