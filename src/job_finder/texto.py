"""Utilitários de texto usados na normalização e deduplicação."""

import re
import unicodedata

from selectolax.lexbor import LexborHTMLParser

_ESPACOS = re.compile(r"[ \t\r\f\v]+")
_LINHAS_VAZIAS = re.compile(r"\n\s*\n+")


def sem_acento(texto: str) -> str:
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def chave(texto: str) -> str:
    """Forma canônica para comparação: minúsculas, sem acento, só letras/números."""
    simples = sem_acento(texto).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", simples).split())


def html_para_texto(html: str | None) -> str | None:
    """Remove tags mantendo quebras de parágrafo; devolve None se vazio."""
    if not html:
        return None
    arvore = LexborHTMLParser(html)
    for no in arvore.css("script, style"):
        no.decompose()
    corpo = arvore.body or arvore.root
    if corpo is None:
        return None
    texto = corpo.text(separator="\n")
    texto = _ESPACOS.sub(" ", texto)
    texto = "\n".join(linha.strip() for linha in texto.splitlines())
    texto = _LINHAS_VAZIAS.sub("\n\n", texto).strip()
    return texto or None
