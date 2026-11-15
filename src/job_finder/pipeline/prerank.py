"""Pré-ranqueamento BM25: ordena as vagas pendentes por aderência textual ao perfil.

Serve só para escolher quais vagas vão para o LLM em cada rodada (as top N); o score que
o usuário vê é o do LLM. Implementação própria para não puxar numpy para o Pi.
"""

import math
from collections import Counter
from collections.abc import Sequence

from job_finder.domain import Perfil, Vaga
from job_finder.texto import chave

K1 = 1.5
B = 0.75
PESO_TITULO = 3  # o título repete para pesar mais que a descrição


def tokens(texto: str | None) -> list[str]:
    return [t for t in chave(texto or "").split() if len(t) > 1]


def consulta_do_perfil(perfil: Perfil) -> list[str]:
    d = perfil.dados
    termos: list[str] = []
    for cargo in d.cargos:
        termos += tokens(cargo) * 2
    for skill in d.skills:
        termos += tokens(skill.nome) * skill.peso
    for q in perfil.queries:
        if q.ativa:
            termos += tokens(q.termo) * 2
    for extra in (*d.certificacoes, *d.setores):
        termos += tokens(extra)
    return termos


def documento_da_vaga(vaga: Vaga) -> list[str]:
    return tokens(vaga.titulo) * PESO_TITULO + tokens(vaga.descricao)


def bm25(consulta: Sequence[str], documentos: Sequence[Sequence[str]]) -> list[float]:
    if not documentos:
        return []
    n = len(documentos)
    media = sum(len(d) for d in documentos) / n or 1.0
    df: Counter[str] = Counter()
    for d in documentos:
        df.update(set(d))
    pesos_consulta = Counter(consulta)
    scores = []
    for d in documentos:
        tf = Counter(d)
        total = 0.0
        for termo, repeticoes in pesos_consulta.items():
            f = tf.get(termo)
            if not f:
                continue
            idf = math.log((n - df[termo] + 0.5) / (df[termo] + 0.5) + 1)
            total += repeticoes * idf * f * (K1 + 1) / (f + K1 * (1 - B + B * len(d) / media))
        scores.append(total)
    return scores


def preranquear(perfil: Perfil, vagas: Sequence[Vaga]) -> list[float]:
    """Score BM25 de cada vaga, na mesma ordem de `vagas`."""
    return bm25(consulta_do_perfil(perfil), [documento_da_vaga(v) for v in vagas])
