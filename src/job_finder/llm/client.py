"""Cliente do Claude com registro de custo por chamada e teto mensal.

Cada resposta tem o `usage` gravado em `custo_llm` numa transação própria, para o gasto
contar mesmo se a operação que chamou o LLM falhar depois. Antes de cada chamada o gasto
do mês (UTC) é comparado com o teto configurado.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel
from sqlalchemy import Engine, func, select

from job_finder.db.models import CustoLLMDB
from job_finder.db.session import sessao

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class Preco:
    """USD por milhão de tokens."""

    entrada: float
    saida: float
    cache_leitura: float
    cache_escrita: float  # TTL de 5 minutos (1,25x a entrada)


# Preços de lista em 2026-11. O Haiku 5.5 cobra 5x acima de 100K tokens de prompt;
# os prompts daqui ficam muito abaixo disso.
PRECOS: dict[str, Preco] = {
    "claude-haiku-5-5": Preco(entrada=0.10, saida=0.50, cache_leitura=0.01, cache_escrita=0.125),
    "claude-opus-5-5": Preco(entrada=4.00, saida=20.00, cache_leitura=0.20, cache_escrita=5.00),
}
DESCONTO_BATCH = 0.5


class LimiteDeCustoAtingido(Exception):
    def __init__(self, gasto: float, limite: float) -> None:
        super().__init__(f"teto mensal do Claude atingido (US$ {gasto:.2f} de US$ {limite:.2f})")
        self.gasto = gasto
        self.limite = limite


class RespostaInvalida(Exception):
    """O modelo respondeu, mas sem uma saída estruturada utilizável."""


class _Usage(Protocol):
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None


def erro_fatal(e: Exception) -> bool:
    """Erros da conta (chave, permissão, saldo) que valem para todas as chamadas.

    Não adianta seguir para a próxima vaga nem gastar as tentativas do match.
    """
    import anthropic

    if isinstance(e, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return True
    return isinstance(e, anthropic.BadRequestError) and "credit balance" in str(e).lower()


def custo(modelo: str, usage: _Usage, *, batch: bool = False) -> float:
    preco = PRECOS.get(modelo)
    if preco is None:
        log.warning("modelo sem preço cadastrado: %s; custo registrado como 0", modelo)
        return 0.0
    total = (
        usage.input_tokens * preco.entrada
        + usage.output_tokens * preco.saida
        + (usage.cache_read_input_tokens or 0) * preco.cache_leitura
        + (usage.cache_creation_input_tokens or 0) * preco.cache_escrita
    ) / 1_000_000
    return total * DESCONTO_BATCH if batch else total


def inicio_do_mes(agora: datetime) -> datetime:
    return agora.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class ClienteLLM:
    def __init__(
        self,
        engine: Engine,
        anthropic: Any,  # anthropic.Anthropic; Any para aceitar dublês nos testes
        *,
        limite_mensal_usd: float,
        relogio: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.engine = engine
        self.anthropic = anthropic
        self.limite_mensal_usd = limite_mensal_usd
        self.relogio = relogio

    def gasto_no_mes(self) -> float:
        with sessao(self.engine, commit=False) as s:
            total = s.scalar(
                select(func.coalesce(func.sum(CustoLLMDB.custo_usd), 0.0)).where(
                    CustoLLMDB.criado_em >= inicio_do_mes(self.relogio())
                )
            )
        return float(total or 0.0)

    def verificar_limite(self) -> None:
        gasto = self.gasto_no_mes()
        if gasto >= self.limite_mensal_usd:
            raise LimiteDeCustoAtingido(gasto, self.limite_mensal_usd)

    def registrar(
        self,
        operacao: str,
        modelo: str,
        usage: _Usage,
        *,
        perfil_id: int | None = None,
        execucao_id: int | None = None,
        batch: bool = False,
    ) -> float:
        valor = custo(modelo, usage, batch=batch)
        with sessao(self.engine) as s:
            s.add(
                CustoLLMDB(
                    execucao_id=execucao_id,
                    perfil_id=perfil_id,
                    operacao=operacao,
                    modelo=modelo,
                    batch=batch,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_input_tokens or 0,
                    cache_write_tokens=usage.cache_creation_input_tokens or 0,
                    custo_usd=valor,
                    criado_em=self.relogio(),
                )
            )
        return valor

    def estruturado(
        self,
        formato: type[T],
        *,
        operacao: str,
        modelo: str,
        perfil_id: int | None = None,
        execucao_id: int | None = None,
        **params: Any,
    ) -> T:
        """Uma chamada com saída estruturada (`messages.parse`), já contabilizada.

        Levanta `RespostaInvalida` em recusa, truncamento ou saída ausente.
        """
        self.verificar_limite()
        resposta = self.anthropic.messages.parse(model=modelo, output_format=formato, **params)
        self.registrar(
            operacao, modelo, resposta.usage, perfil_id=perfil_id, execucao_id=execucao_id
        )
        u = resposta.usage
        log.info(
            "%s %s: in=%d out=%d cache_read=%d cache_write=%d stop=%s",
            operacao,
            modelo,
            u.input_tokens,
            u.output_tokens,
            u.cache_read_input_tokens or 0,
            u.cache_creation_input_tokens or 0,
            resposta.stop_reason,
        )
        if resposta.stop_reason == "refusal":
            raise RespostaInvalida("o modelo recusou a solicitação")
        if resposta.stop_reason == "max_tokens":
            raise RespostaInvalida("resposta truncada (max_tokens)")
        saida = resposta.parsed_output
        if saida is None:
            raise RespostaInvalida(f"sem saída estruturada (stop_reason={resposta.stop_reason})")
        return saida


def criar_cliente(engine: Engine) -> ClienteLLM:
    """Cliente real a partir das configurações. Exige ANTHROPIC_API_KEY."""
    import anthropic

    from job_finder.config import get_settings

    settings = get_settings()
    if settings.anthropic_api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY não configurada (coloque no .env)")
    return ClienteLLM(
        engine,
        anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value()),
        limite_mensal_usd=settings.custo_mensal_max_usd,
    )
