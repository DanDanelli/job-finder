"""JSearch (OpenWeb Ninja, via RapidAPI): vagas do Google for Jobs.

O Google for Jobs indexa as vagas que LinkedIn, Indeed, Glassdoor, Vagas.com e outros
publicam; o JSearch devolve esse índice por API. Cada vaga traz o site de origem
(`job_publisher`), que aparece no digest.

Quota: o plano grátis é pequeno (cerca de 200 requisições por mês). Cada requisição
traz uma página (~10 vagas); por isso cada rodada faz só algumas buscas, em rodízio
entre os pares (termo, país) de todos os perfis. O cursor do rodízio é o número de
chamadas já feitas no mês, então não precisa de estado extra.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx

from job_finder.config import Settings, get_settings
from job_finder.connectors.base import registrar
from job_finder.domain import Query, Salario, VagaBruta
from job_finder.quota import Quota

HOST = "jsearch.p.rapidapi.com"
URL = f"https://{HOST}/search"
PERIODOS = {"YEAR": "ano", "MONTH": "mes", "HOUR": "hora"}


class JSearch:
    nome = "jsearch"

    def __init__(self, http: httpx.Client, settings: Settings) -> None:
        self.http = http
        self.settings = settings
        self.limite_diario: int | None = settings.jsearch_limite_diario
        self.limite_mensal: int | None = settings.jsearch_limite_mensal

    def combinacoes(self, queries: Sequence[Query]) -> list[tuple[str, str]]:
        """Pares (termo, país), na ordem do rodízio. Query sem país usa os padrões."""
        pares: list[tuple[str, str]] = []
        for q in queries:
            for pais in q.paises or self.settings.jsearch_paises:
                par = (q.termo, pais.lower())
                if par not in pares:
                    pares.append(par)
        return pares

    def buscar(self, queries: Sequence[Query], quota: Quota) -> list[VagaBruta]:
        if self.settings.jsearch_api_key is None:
            raise RuntimeError("JOBFINDER_JSEARCH_API_KEY não configurada")
        pares = self.combinacoes(queries)
        if not pares:
            return []
        inicio = quota.usadas_no_mes()
        n = min(self.settings.jsearch_buscas_por_rodada, len(pares))
        vagas: dict[str, VagaBruta] = {}
        for i in range(n):
            termo, pais = pares[(inicio + i) % len(pares)]
            quota.consumir()
            for job in self._buscar(termo, pais):
                vaga = self._converter(job)
                if vaga is not None:
                    vagas.setdefault(vaga.id_fonte, vaga)
        return list(vagas.values())

    def _buscar(self, termo: str, pais: str) -> list[dict[str, Any]]:
        assert self.settings.jsearch_api_key is not None
        resposta = self.http.get(
            URL,
            params={
                "query": termo,
                "country": pais,
                "page": 1,
                "num_pages": 1,
                "date_posted": self.settings.jsearch_publicadas_em,
            },
            headers={
                "x-rapidapi-key": self.settings.jsearch_api_key.get_secret_value(),
                "x-rapidapi-host": HOST,
            },
        )
        resposta.raise_for_status()
        corpo = resposta.json()
        if corpo.get("status") not in (None, "OK"):
            raise RuntimeError(f"JSearch: {corpo.get('status')}: {corpo.get('error')}")
        return list(corpo.get("data") or [])

    def _converter(self, job: dict[str, Any]) -> VagaBruta | None:
        url = job.get("job_apply_link") or job.get("job_google_link")
        if not job.get("job_id") or not job.get("job_title") or not url:
            return None
        publicada = job.get("job_posted_at_datetime_utc")
        direto = next(
            (o.get("apply_link") for o in job.get("apply_options") or [] if o.get("is_direct")),
            None,
        )
        local = job.get("job_location") or ", ".join(
            p for p in (job.get("job_city"), job.get("job_state"), job.get("job_country")) if p
        )
        return VagaBruta(
            fonte=self.nome,
            id_fonte=str(job["job_id"]),
            titulo=job["job_title"],
            empresa=job.get("employer_name") or None,
            local=local or None,
            descricao=job.get("job_description") or None,
            url=url,
            url_candidatura=direto,
            publicada_em=datetime.fromisoformat(publicada) if publicada else None,
            modalidade="remote" if job.get("job_is_remote") else None,
            tipo_contrato=job.get("job_employment_type") or None,
            salario=_salario(job),
            dados_brutos={
                "publisher": job.get("job_publisher"),
                "google_link": job.get("job_google_link"),
            },
        )


def _salario(job: dict[str, Any]) -> Salario | None:
    minimo, maximo = job.get("job_min_salary"), job.get("job_max_salary")
    if minimo is None and maximo is None:
        return None
    return Salario(
        minimo=minimo,
        maximo=maximo,
        moeda=job.get("job_salary_currency"),
        periodo=PERIODOS.get(str(job.get("job_salary_period") or "").upper()),
    )


@registrar("jsearch")
def _fabrica(http: httpx.Client) -> JSearch:
    return JSearch(http, get_settings())
