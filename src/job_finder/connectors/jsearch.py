"""JSearch (OpenWeb Ninja, acesso direto): vagas do Google for Jobs.

O Google for Jobs indexa as vagas que LinkedIn, Indeed, Glassdoor, Vagas.com e outros
publicam; o JSearch devolve esse índice por API. Cada vaga traz o site de origem
(`job_publisher`), que aparece no digest.

API: GET https://api.openwebninja.com/jsearch/search-v2, chave no cabeçalho `x-api-key`
(spec OpenAPI oficial, conferida em 2026-11-15). Cada página (até 10 vagas) consome uma
requisição da quota.

Quota: o plano grátis é pequeno, por isso cada rodada faz só algumas buscas, em
rodízio entre os pares (termo, país) de todos os perfis. O cursor do rodízio é o número de
chamadas já feitas no mês, então não precisa de estado extra.
"""

import base64
import binascii
import hashlib
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from job_finder.config import Settings, get_settings
from job_finder.connectors.base import registrar
from job_finder.domain import Query, Salario, VagaBruta
from job_finder.pipeline.locais import interpretar_local
from job_finder.quota import Quota

URL = "https://api.openwebninja.com/jsearch/search-v2"
PERIODOS = {"YEAR": "ano", "MONTH": "mes", "HOUR": "hora"}
# "há 22 horas", "há 1 dia", "há 2 semanas", "há 1 mês"; ou em inglês, "3 days ago".
_IDADE = re.compile(
    r"(\d+)\+?\s*(minuto|minute|hora|hour|dia|day|semana|week|m[eê]s|meses|month)", re.I
)
_UNIDADES = {
    "minut": timedelta(minutes=1),
    "hora": timedelta(hours=1),
    "hour": timedelta(hours=1),
    "dia": timedelta(days=1),
    "day": timedelta(days=1),
    "semana": timedelta(weeks=1),
    "week": timedelta(weeks=1),
    "m": timedelta(days=30),  # mês, meses, month
}


class JSearch:
    nome = "jsearch"

    def __init__(
        self,
        http: httpx.Client,
        settings: Settings,
        relogio: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.http = http
        self.settings = settings
        self.relogio = relogio
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
                "num_pages": 1,
                "date_posted": self.settings.jsearch_publicadas_em,
            },
            headers={"x-api-key": self.settings.jsearch_api_key.get_secret_value()},
        )
        resposta.raise_for_status()
        corpo = resposta.json()
        if corpo.get("status") not in (None, "OK"):
            raise RuntimeError(f"JSearch: {corpo.get('status')}: {corpo.get('error')}")
        return list((corpo.get("data") or {}).get("jobs") or [])

    def _converter(self, job: dict[str, Any]) -> VagaBruta | None:
        url = job.get("job_apply_link") or job.get("job_google_link")
        if not job.get("job_id") or not job.get("job_title") or not url:
            return None
        direto = next(
            (o.get("apply_link") for o in job.get("apply_options") or [] if o.get("is_direct")),
            None,
        )
        local = job.get("job_location") or ", ".join(
            p for p in (job.get("job_city"), job.get("job_state")) if p
        )
        # "Contagem - MG" não diz o país (e MG parece Madagascar); o job_country diz.
        pais = job.get("job_country")
        if pais and interpretar_local(local) != [pais]:
            local = f"{local}, {pais}" if local else pais
        return VagaBruta(
            fonte=self.nome,
            id_fonte=_id_estavel(str(job["job_id"])),
            titulo=job["job_title"],
            empresa=job.get("employer_name") or None,
            local=local or None,
            descricao=job.get("job_description") or None,
            url=url,
            url_candidatura=direto,
            publicada_em=self._publicada(job),
            modalidade="remote" if job.get("job_is_remote") else None,
            tipo_contrato=job.get("job_employment_type") or None,
            salario=_salario(job),
            salario_texto=job.get("job_salary_string") or None,
            dados_brutos={
                "publisher": job.get("job_publisher"),
                "google_link": job.get("job_google_link"),
            },
        )

    def _publicada(self, job: dict[str, Any]) -> datetime | None:
        if iso := job.get("job_posted_at_datetime_utc"):
            return datetime.fromisoformat(iso)
        if ts := job.get("job_posted_at_timestamp"):
            return datetime.fromtimestamp(ts, UTC)
        m = _IDADE.search(job.get("job_posted_at") or "")
        if not m:
            return None
        unidade = next(d for k, d in _UNIDADES.items() if m.group(2).lower().startswith(k))
        return self.relogio() - int(m.group(1)) * unidade


def _id_estavel(job_id: str) -> str:
    """O job_id é base64 de "<docid do Google>:<token da busca>"; o token muda a cada
    busca, então a mesma vaga voltaria como nova. Usa só o docid."""
    try:
        decodificado = base64.urlsafe_b64decode(job_id + "=" * (-len(job_id) % 4))
        docid = decodificado.split(b":", 1)[0].decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        docid = ""
    if docid and len(docid) <= 64:
        return docid
    return hashlib.sha256(job_id.encode()).hexdigest()[:32]


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
