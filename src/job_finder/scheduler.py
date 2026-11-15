"""Agendador: rodadas nos horários configurados, com o digest ao fim de cada uma.

Uma rodada por vez (lock compartilhado com o /buscar do bot). O pipeline é síncrono
(SQLAlchemy, httpx, SDK do Claude) e roda numa thread para não travar o bot.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from job_finder.config import Settings
from job_finder.servico import NenhumPerfil, Relatorio, rodada_completa
from job_finder.telegram.digest import Enviador, enviar_digest

log = logging.getLogger(__name__)


def proxima_execucao(agora: datetime, horarios: Sequence[time], fuso: ZoneInfo) -> datetime:
    """Próximo horário depois de `agora`, em UTC. Respeita horário de verão do fuso."""
    if not horarios:
        raise ValueError("nenhum horário configurado")
    local = agora.astimezone(fuso)
    candidatos = [
        datetime.combine(local.date() + timedelta(days=d), h, tzinfo=fuso)
        for d in (0, 1)
        for h in horarios
    ]
    return min(c for c in candidatos if c > local).astimezone(UTC)


class Agendador:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        enviador: Enviador | None,
        *,
        pausa_entre_mensagens_s: float = 0.4,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.enviador = enviador
        self.pausa = pausa_entre_mensagens_s
        self.lock = asyncio.Lock()

    @property
    def ocupado(self) -> bool:
        return self.lock.locked()

    async def rodar_e_notificar(
        self, nomes: Sequence[str] | None = None, tipo: str = "agendada"
    ) -> Relatorio | None:
        async with self.lock:
            try:
                relatorio = await asyncio.to_thread(
                    rodada_completa, self.engine, self.settings, nomes=nomes, tipo=tipo
                )
            except NenhumPerfil:
                log.info("rodada %s sem perfis ativos", tipo)
                return None
            if self.enviador is not None:
                for rp in relatorio.perfis:
                    try:
                        await enviar_digest(
                            self.engine,
                            self.enviador,
                            rp.perfil_id,
                            score_minimo=self.settings.digest_score_minimo,
                            maximo=self.settings.digest_max_vagas,
                            avisos=relatorio.avisos_de(rp.perfil_id),
                            pausa_s=self.pausa,
                        )
                    except Exception:  # um chat com problema não impede o do outro perfil
                        log.exception("falha ao enviar o digest de %s", rp.nome)
            return relatorio

    async def loop(self) -> None:
        fuso = ZoneInfo(self.settings.timezone)
        while True:
            proxima = proxima_execucao(datetime.now(UTC), self.settings.horarios, fuso)
            log.info("próxima rodada agendada: %s", proxima.astimezone(fuso).isoformat())
            await asyncio.sleep(max(0.0, (proxima - datetime.now(UTC)).total_seconds()))
            try:
                await self.rodar_e_notificar(tipo="agendada")
            except Exception:  # nunca derrubar o loop; o erro fica no log
                log.exception("rodada agendada falhou")
