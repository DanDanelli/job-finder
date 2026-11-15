"""Filtros duros: decidem em código, sem LLM, se uma vaga segue no pipeline.

A regra geral é conservadora: quando falta informação na vaga (sem salário, local
desconhecido), ela passa, e o scoring do LLM avalia.
"""

from datetime import datetime, timedelta

from job_finder.domain import Filtros, Modalidade, Vaga
from job_finder.pipeline.dedup import empresa_canonica
from job_finder.pipeline.locais import elegivel
from job_finder.pipeline.salario import anual
from job_finder.texto import chave


def motivo_descarte(vaga: Vaga, filtros: Filtros, agora: datetime) -> str | None:
    """Devolve o motivo do descarte, ou None se a vaga passa em todos os filtros."""
    if vaga.publicada_em and agora - vaga.publicada_em > timedelta(days=filtros.idade_maxima_dias):
        return "antiga"

    if (
        filtros.modalidades
        and vaga.modalidade != Modalidade.DESCONHECIDA
        and vaga.modalidade not in filtros.modalidades
    ):
        return "modalidade"

    if vaga.modalidade == Modalidade.REMOTO:
        if filtros.remoto_a_partir_de and (
            elegivel(vaga.locais_elegiveis, filtros.remoto_a_partir_de) is False
        ):
            return "local"
    elif filtros.paises and vaga.pais and vaga.pais not in filtros.paises:
        return "pais"

    if (
        filtros.cidades
        and vaga.cidade
        and vaga.modalidade != Modalidade.REMOTO
        and chave(vaga.cidade) not in {chave(c) for c in filtros.cidades}
    ):
        return "cidade"

    if (
        filtros.tipos_contrato
        and vaga.tipo_contrato
        and (vaga.tipo_contrato not in filtros.tipos_contrato)
    ):
        return "tipo_contrato"

    if _abaixo_do_salario(vaga, filtros):
        return "salario"

    empresa = empresa_canonica(vaga.empresa)
    if empresa and empresa in {empresa_canonica(e) for e in filtros.empresas_bloqueadas}:
        return "empresa_bloqueada"

    # Palavras bloqueadas valem só para o título: na descrição dariam falso positivo demais.
    titulo = f" {chave(vaga.titulo)} "
    if any(f" {chave(p)} " in titulo for p in filtros.palavras_bloqueadas if chave(p)):
        return "palavra_bloqueada"

    return None


def _abaixo_do_salario(vaga: Vaga, filtros: Filtros) -> bool:
    minimo, salario = filtros.salario_minimo, vaga.salario
    if not minimo or minimo.minimo is None or not salario:
        return False
    teto_vaga = salario.maximo if salario.maximo is not None else salario.minimo
    if teto_vaga is None or not salario.periodo or not salario.moeda:
        return False
    if salario.moeda != (minimo.moeda or salario.moeda):
        return False  # sem conversão de câmbio na fase 1
    return anual(teto_vaga, salario.periodo) < anual(minimo.minimo, minimo.periodo or "ano")
