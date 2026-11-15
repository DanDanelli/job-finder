"""Remotive: vagas remotas via API pública.

Termos (aviso legal da própria API, verificado em 2026-11-15):
- no máximo ~4 requisições por dia;
- linkar a vaga no Remotive e citar o Remotive como fonte;
- as vagas chegam com 24h de atraso.

O catálogo público é pequeno (16 vagas em 2026-11-15) e o parâmetro `search` é
impreciso, então baixamos o catálogo inteiro uma vez por rodada e deixamos o
pipeline filtrar.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx

from job_finder.connectors.base import registrar
from job_finder.domain import Query, VagaBruta
from job_finder.quota import Quota
from job_finder.texto import html_para_texto

URL = "https://remotive.com/api/remote-jobs"


class Remotive:
    nome = "remotive"
    limite_diario: int | None = 4

    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def buscar(self, queries: Sequence[Query], quota: Quota) -> list[VagaBruta]:
        quota.consumir()
        resposta = self.http.get(URL)
        resposta.raise_for_status()
        return [self._converter(job) for job in resposta.json().get("jobs", [])]

    def _converter(self, job: dict[str, Any]) -> VagaBruta:
        publicada = job.get("publication_date")
        return VagaBruta(
            fonte=self.nome,
            id_fonte=str(job["id"]),
            titulo=job["title"],
            empresa=(job.get("company_name") or "").strip() or None,
            local=job.get("candidate_required_location") or None,
            descricao=html_para_texto(job.get("description")),
            url=job["url"],
            publicada_em=datetime.fromisoformat(publicada) if publicada else None,
            modalidade="remote",
            tipo_contrato=job.get("job_type") or None,
            salario_texto=job.get("salary") or None,
            tags=[str(t) for t in job.get("tags") or []],
            dados_brutos={"category": job.get("category")},
        )


@registrar("remotive")
def _fabrica(http: httpx.Client) -> Remotive:
    return Remotive(http)
