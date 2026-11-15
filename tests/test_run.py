"""Pipeline de ponta a ponta com conectores falsos (sem rede)."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine, func, select
from typer.testing import CliRunner

from job_finder.cli import app as cli_app
from job_finder.connectors.remotive import URL
from job_finder.db import repo
from job_finder.db.models import ExecucaoDB, MatchDB, VagaDB, VagaFonteDB
from job_finder.db.session import sessao, upgrade
from job_finder.domain import Filtros, Perfil, Query, StatusMatch, VagaBruta
from job_finder.pipeline.run import ResumoRodada, executar_rodada
from job_finder.quota import Quota

AGORA = datetime(2026, 11, 15, 12, tzinfo=UTC)


class Falso:
    limite_diario: int | None = None

    def __init__(self, nome: str, vagas: list[VagaBruta]) -> None:
        self.nome = nome
        self.vagas = vagas
        self.queries_recebidas: list[Query] = []

    def buscar(self, queries: Sequence[Query], quota: Quota) -> list[VagaBruta]:
        quota.consumir()
        self.queries_recebidas = list(queries)
        return self.vagas


class Quebrado:
    nome = "quebrado"
    limite_diario: int | None = None

    def buscar(self, queries: Sequence[Query], quota: Quota) -> list[VagaBruta]:
        raise httpx.ConnectError("sem rede")


def bruta(fonte: str, id_fonte: str, **kw: object) -> VagaBruta:
    base: dict[str, object] = {
        "fonte": fonte,
        "id_fonte": id_fonte,
        "titulo": "Cloud Architect",
        "empresa": "Acme",
        "local": "Europe",
        "modalidade": "remote",
        "url": f"https://{fonte}.example/{id_fonte}",
        "publicada_em": AGORA - timedelta(days=1),
    }
    return VagaBruta.model_validate(base | kw)


def criar_perfis(engine: Engine) -> None:
    with sessao(engine) as s:
        repo.salvar_perfil(
            s,
            Perfil(
                nome="daniel",
                queries=[Query(termo="Cloud Architect"), Query(termo="DevOps", ativa=False)],
                filtros=Filtros(remoto_a_partir_de=["PT"]),
            ),
        )
        repo.salvar_perfil(
            s,
            Perfil(
                nome="esposa",
                queries=[Query(termo="Brand Marketing"), Query(termo="cloud architect")],
                filtros=Filtros(remoto_a_partir_de=["BR"], palavras_bloqueadas=["architect"]),
            ),
        )


def rodar(engine: Engine, conectores: Sequence[object]) -> ResumoRodada:
    with sessao(engine) as s:
        return executar_rodada(s, engine, conectores, repo.listar_perfis(s), AGORA)  # type: ignore[arg-type]


def contar(engine: Engine, modelo: type) -> int:
    with sessao(engine) as s:
        return int(s.scalar(select(func.count()).select_from(modelo)) or 0)


def test_rodada_completa(engine: Engine) -> None:
    criar_perfis(engine)
    fonte_a = Falso(
        "a",
        [
            bruta("a", "1"),
            bruta("a", "2", titulo="Data Engineer", local="USA"),
            bruta("a", "3", titulo="Old Job", publicada_em=AGORA - timedelta(days=60)),
        ],
    )
    # Mesma vaga "a/1" anunciada por outra fonte, com título e empresa escritos diferente.
    fonte_b = Falso("b", [bruta("b", "x", titulo="Cloud Architect (m/f/d)", empresa="ACME GmbH")])

    resumo = rodar(engine, [fonte_a, fonte_b])

    assert resumo.erros == []
    assert resumo.coletadas == 4
    assert resumo.novas == 3  # a/1 e b/x são a mesma vaga
    assert contar(engine, VagaDB) == 3
    assert contar(engine, VagaFonteDB) == 4

    # Queries ativas, sem repetição (caixa diferente conta como a mesma).
    assert sorted(q.termo for q in fonte_a.queries_recebidas) == [
        "Brand Marketing",
        "Cloud Architect",
    ]

    daniel, esposa = resumo.perfis
    assert [v.titulo for v in daniel.aprovadas] == ["Cloud Architect"]
    assert daniel.descartes == {"local": 1}  # "USA" não aceita quem mora em PT
    assert esposa.aprovadas == []
    assert esposa.descartes == {"local": 2}  # Europa e EUA não aceitam quem mora no BR
    # A vaga de 60 dias nem entra como candidata.
    assert daniel.avaliadas == 2

    with sessao(engine) as s:
        matches = s.scalars(select(MatchDB)).all()
        assert [(m.perfil.nome, m.status) for m in matches] == [("daniel", StatusMatch.PENDENTE)]
        execucao = s.scalars(select(ExecucaoDB)).one()
        assert (execucao.status, execucao.vagas_novas, execucao.matches_novos) == ("ok", 3, 1)
        assert execucao.finalizada_em is not None


def test_segunda_rodada_nao_duplica(engine: Engine) -> None:
    criar_perfis(engine)
    fonte = Falso("a", [bruta("a", "1"), bruta("a", "2", titulo="SRE")])
    rodar(engine, [fonte])
    resumo = rodar(engine, [fonte])

    assert resumo.novas == 0
    assert contar(engine, VagaDB) == 2
    assert contar(engine, VagaFonteDB) == 2
    assert contar(engine, MatchDB) == 2  # um match por vaga para o daniel, sem repetir
    assert resumo.perfis[0].avaliadas == 0


def test_fonte_quebrada_nao_derruba_a_rodada(engine: Engine) -> None:
    criar_perfis(engine)
    resumo = rodar(engine, [Quebrado(), Falso("a", [bruta("a", "1")])])

    assert resumo.coletadas == 1
    assert resumo.erros == ["quebrado: ConnectError: sem rede"]
    with sessao(engine) as s:
        assert s.scalars(select(ExecucaoDB)).one().status == "parcial"


def test_quota_esgotada_vira_aviso(engine: Engine) -> None:
    criar_perfis(engine)
    fonte = Falso("a", [bruta("a", "1")])
    fonte.limite_diario = 1
    Quota(engine, "a", 1, AGORA.date()).consumir()

    resumo = rodar(engine, [fonte])
    assert resumo.coletadas == 0
    assert resumo.erros == ["quota de a esgotada (1/dia)"]
    with sessao(engine) as s:
        assert s.scalars(select(ExecucaoDB)).one().status == "erro"


# --- CLI ----------------------------------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "remotive" / "remote_jobs.json"
EXEMPLO = Path(__file__).parents[1] / "perfis" / "exemplo.yaml"


@pytest.fixture
def cli_db(tmp_path: Path) -> Engine:
    from job_finder.config import get_settings
    from job_finder.db.session import criar_engine

    url = get_settings().resolved_database_url
    upgrade(url)
    return criar_engine(url)


@pytest.fixture
def remotive_api() -> respx.MockRouter:
    dados = json.loads(FIXTURE.read_text())
    # Datas recentes para não caírem no filtro de idade.
    for job in dados["jobs"]:
        job["publication_date"] = (datetime.now(UTC) - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
    with respx.mock() as router:
        router.get(URL).mock(return_value=httpx.Response(200, json=dados))
        yield router


def test_cli_dry_run_nao_grava_mas_conta_quota(
    cli_db: Engine, remotive_api: respx.MockRouter
) -> None:
    runner = CliRunner()
    assert runner.invoke(cli_app, ["perfil", "carregar", str(EXEMPLO)]).exit_code == 0

    resultado = runner.invoke(cli_app, ["run", "--dry-run"])
    assert resultado.exit_code == 0, resultado.output
    assert "Coletadas: 6" in resultado.output
    assert "dry-run" in resultado.output
    assert contar(cli_db, VagaDB) == 0
    assert contar(cli_db, ExecucaoDB) == 0
    # A chamada HTTP aconteceu, então conta na quota mesmo no dry-run.
    assert Quota(cli_db, "remotive", 4, datetime.now(UTC).date()).usadas_hoje() == 1


def test_cli_run_grava_e_mostra_aprovadas(cli_db: Engine, remotive_api: respx.MockRouter) -> None:
    runner = CliRunner()
    runner.invoke(cli_app, ["perfil", "carregar", str(EXEMPLO)])
    resultado = runner.invoke(cli_app, ["run", "--perfil", "exemplo"])

    assert resultado.exit_code == 0, resultado.output
    assert contar(cli_db, VagaDB) == 6
    # Perfil exemplo: remoto a partir de PT. As duas vagas restritas a EUA/Canadá caem
    # no filtro de local; as quatro Worldwide/Europa passam (salário em USD não é
    # comparado com o mínimo em EUR, porque não há câmbio na fase 1).
    assert "4 de 6 passaram nos filtros" in resultado.output
    assert "local: 2" in resultado.output
    with sessao(cli_db) as s:
        titulos = set(s.scalars(select(VagaDB.titulo).join(MatchDB)))
    assert "Senior Data Engineer" in titulos
    assert "Customer Service Representative" not in titulos
    assert "Tech Lead Full-Stack Rails Engineer" not in titulos


def test_cli_run_sem_perfis_falha_com_mensagem(cli_db: Engine) -> None:
    resultado = CliRunner().invoke(cli_app, ["run"])
    assert resultado.exit_code != 0
    assert "nenhum perfil" in resultado.output
