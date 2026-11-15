"""Interface comum dos conectores e registro dos disponíveis."""

from collections.abc import Callable, Sequence
from typing import Protocol

import httpx

from job_finder import __version__
from job_finder.domain import Query, VagaBruta
from job_finder.quota import Quota

USER_AGENT = f"job-finder/{__version__} (uso pessoal)"


class Conector(Protocol):
    nome: str
    # Limite de chamadas HTTP por dia (termos da fonte ou free tier). None = sem limite.
    limite_diario: int | None

    def buscar(self, queries: Sequence[Query], quota: Quota) -> list[VagaBruta]:
        """Busca vagas para o conjunto de queries de todos os perfis.

        Cada conector decide a estratégia: uma chamada por query, ou um catálogo
        inteiro filtrado depois. Toda chamada HTTP passa por `quota.consumir()`.
        """
        ...


FabricaConector = Callable[[httpx.Client], Conector]
_REGISTRO: dict[str, FabricaConector] = {}


def registrar(nome: str) -> Callable[[FabricaConector], FabricaConector]:
    def decorador(fabrica: FabricaConector) -> FabricaConector:
        _REGISTRO[nome] = fabrica
        return fabrica

    return decorador


def disponiveis() -> list[str]:
    return sorted(_REGISTRO)


def criar(nome: str, http: httpx.Client) -> Conector:
    try:
        return _REGISTRO[nome](http)
    except KeyError:
        raise ValueError(f"conector desconhecido: {nome} (disponíveis: {disponiveis()})") from None


def cliente_http() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    )
