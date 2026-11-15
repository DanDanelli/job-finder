"""Chave de deduplicação: a mesma vaga vinda de fontes diferentes gera a mesma chave.

O fuzzy matching entre títulos parecidos entra no Marco 5.
"""

import re

from job_finder.domain import Modalidade, Vaga
from job_finder.texto import chave

# Marcadores de gênero comuns em vagas europeias: (m/f/d), (m/w/d), (h/f), (f/m/x)...
_GENERO = re.compile(r"\(\s*[mfwdhx]\s*(?:/\s*[mfwdhx]\s*)+\)", re.IGNORECASE)
_ABREVIACOES = {"sr": "senior", "jr": "junior", "mgr": "manager", "eng": "engineer"}
_SUFIXOS_EMPRESA = {
    "inc", "llc", "ltd", "ltda", "lda", "gmbh", "ag", "sa", "bv", "plc", "corp",
    "co", "limited", "sl", "srl", "spa",
}  # fmt: skip


def titulo_canonico(titulo: str) -> str:
    sem_genero = _GENERO.sub(" ", titulo)
    palavras = [_ABREVIACOES.get(p, p) for p in chave(sem_genero).split()]
    return " ".join(palavras)


def empresa_canonica(empresa: str | None) -> str:
    if not empresa:
        return ""
    # "S.A." vira "s a" depois do chave(); junta para casar com o sufixo "sa".
    palavras = re.sub(r"\bs a$", "sa", chave(empresa)).split()
    while len(palavras) > 1 and palavras[-1] in _SUFIXOS_EMPRESA:
        palavras.pop()
    return " ".join(palavras)


def chave_dedup(vaga: Vaga) -> str:
    if vaga.pais:
        lugar = vaga.pais.lower()
    elif vaga.modalidade == Modalidade.REMOTO:
        lugar = "remoto"
    else:
        lugar = "?"
    return f"{empresa_canonica(vaga.empresa)}|{titulo_canonico(vaga.titulo)}|{lugar}"
