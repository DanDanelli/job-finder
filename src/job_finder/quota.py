"""Controle de quota por conector: limite diário e mensal (mês do calendário).

Cada consumo é gravado na hora, numa transação própria: a chamada HTTP aconteceu
mesmo que a rodada falhe depois ou seja um dry-run com rollback.
"""

from datetime import date

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.sqlite import insert

from job_finder.db.models import UsoQuotaDB
from job_finder.db.session import sessao


class QuotaEsgotada(Exception):
    def __init__(
        self, conector: str, limite_diario: int | None, limite_mensal: int | None = None
    ) -> None:
        limites = []
        if limite_diario is not None:
            limites.append(f"{limite_diario}/dia")
        if limite_mensal is not None:
            limites.append(f"{limite_mensal}/mês")
        super().__init__(f"quota de {conector} esgotada ({', '.join(limites)})")
        self.conector = conector


class Quota:
    def __init__(
        self,
        engine: Engine,
        conector: str,
        limite_diario: int | None,
        hoje: date,
        limite_mensal: int | None = None,
    ):
        self.engine = engine
        self.conector = conector
        self.limite_diario = limite_diario
        self.limite_mensal = limite_mensal
        self.hoje = hoje

    def usadas_no_mes(self) -> int:
        with sessao(self.engine) as s:
            consulta = select(func.coalesce(func.sum(UsoQuotaDB.chamadas), 0)).where(
                UsoQuotaDB.conector == self.conector,
                UsoQuotaDB.dia >= self.hoje.replace(day=1),
                UsoQuotaDB.dia <= self.hoje,
            )
            return int(s.execute(consulta).scalar_one())

    def usadas_hoje(self) -> int:
        with sessao(self.engine) as s:
            consulta = select(func.coalesce(func.sum(UsoQuotaDB.chamadas), 0)).where(
                UsoQuotaDB.conector == self.conector, UsoQuotaDB.dia == self.hoje
            )
            return int(s.execute(consulta).scalar_one())

    def disponivel(self) -> int | None:
        """Chamadas restantes hoje, considerando o limite diário e o mensal; None se não há
        limite."""
        restantes = []
        if self.limite_diario is not None:
            restantes.append(self.limite_diario - self.usadas_hoje())
        if self.limite_mensal is not None:
            restantes.append(self.limite_mensal - self.usadas_no_mes())
        return max(min(restantes), 0) if restantes else None

    def consumir(self, n: int = 1) -> None:
        """Registra `n` chamadas; levanta QuotaEsgotada se não houver saldo."""
        restante = self.disponivel()
        if restante is not None and restante < n:
            raise QuotaEsgotada(self.conector, self.limite_diario, self.limite_mensal)
        with sessao(self.engine) as s:
            stmt = insert(UsoQuotaDB).values(conector=self.conector, dia=self.hoje, chamadas=n)
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["conector", "dia"],
                    set_={"chamadas": UsoQuotaDB.chamadas + n},
                )
            )
