"""Controle de quota por conector (limite diário; o mensal e o rodízio entram no M5).

Cada consumo é gravado na hora, numa transação própria: a chamada HTTP aconteceu
mesmo que a rodada falhe depois ou seja um dry-run com rollback.
"""

from datetime import date

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.sqlite import insert

from job_finder.db.models import UsoQuotaDB
from job_finder.db.session import sessao


class QuotaEsgotada(Exception):
    def __init__(self, conector: str, limite: int) -> None:
        super().__init__(f"quota diária de {conector} esgotada ({limite} chamadas)")
        self.conector = conector
        self.limite = limite


class Quota:
    def __init__(self, engine: Engine, conector: str, limite_diario: int | None, hoje: date):
        self.engine = engine
        self.conector = conector
        self.limite_diario = limite_diario
        self.hoje = hoje

    def usadas_hoje(self) -> int:
        with sessao(self.engine) as s:
            consulta = select(func.coalesce(func.sum(UsoQuotaDB.chamadas), 0)).where(
                UsoQuotaDB.conector == self.conector, UsoQuotaDB.dia == self.hoje
            )
            return int(s.execute(consulta).scalar_one())

    def disponivel(self) -> int | None:
        """Chamadas restantes hoje; None se não há limite."""
        if self.limite_diario is None:
            return None
        return max(self.limite_diario - self.usadas_hoje(), 0)

    def consumir(self, n: int = 1) -> None:
        """Registra `n` chamadas; levanta QuotaEsgotada se não houver saldo."""
        restante = self.disponivel()
        if restante is not None and restante < n:
            raise QuotaEsgotada(self.conector, self.limite_diario or 0)
        with sessao(self.engine) as s:
            stmt = insert(UsoQuotaDB).values(conector=self.conector, dia=self.hoje, chamadas=n)
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["conector", "dia"],
                    set_={"chamadas": UsoQuotaDB.chamadas + n},
                )
            )
