"""Cliente, extração de perfil, BM25 e scoring, com um dublê do SDK do Claude (sem rede)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest
from sqlalchemy import Engine, select
from typer.testing import CliRunner

from job_finder.cli import app as cli_app
from job_finder.db import repo
from job_finder.db.models import CustoLLMDB, MatchDB
from job_finder.db.session import sessao
from job_finder.domain import (
    DadosPerfil,
    Filtros,
    Idioma,
    Perfil,
    Query,
    ResultadoFit,
    Skill,
    StatusMatch,
    Vaga,
    VagaBruta,
)
from job_finder.llm.client import ClienteLLM, LimiteDeCustoAtingido, RespostaInvalida, custo
from job_finder.llm.profile_extractor import ExtracaoPerfil, extrair_perfil
from job_finder.llm.scorer import INSTRUCOES, MAX_TENTATIVAS, pontuar_pendentes
from job_finder.pipeline.prerank import bm25, preranquear
from job_finder.pipeline.run import executar_rodada
from job_finder.quota import Quota

AGORA = datetime(2026, 11, 15, 12, tzinfo=UTC)
HAIKU = "claude-haiku-5-5"
OPUS = "claude-opus-5-5"


@dataclass
class Usage:
    input_tokens: int = 1_000
    output_tokens: int = 200
    cache_read_input_tokens: int | None = 0
    cache_creation_input_tokens: int | None = 0


@dataclass
class Resposta:
    parsed_output: Any
    stop_reason: str = "end_turn"
    usage: Usage | None = None

    def __post_init__(self) -> None:
        self.usage = self.usage or Usage()


class Mensagens:
    def __init__(self, responder: Callable[[dict[str, Any]], Resposta]) -> None:
        self.responder = responder
        self.chamadas: list[dict[str, Any]] = []

    def parse(self, **kw: Any) -> Resposta:
        self.chamadas.append(kw)
        return self.responder(kw)


class Anthropic:
    def __init__(self, responder: Callable[[dict[str, Any]], Resposta]) -> None:
        self.messages = Mensagens(responder)


def cliente(engine: Engine, responder: Callable[..., Resposta], limite: float = 5.0) -> ClienteLLM:
    return ClienteLLM(engine, Anthropic(responder), limite_mensal_usd=limite, relogio=lambda: AGORA)


def fit(score: int = 80, **kw: Any) -> ResultadoFit:
    return ResultadoFit(score=score, resumo="ok", **kw)


# --- custo e teto -------------------------------------------------------------


def test_custo_por_modelo_com_cache_e_batch() -> None:
    u = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
    )
    assert custo(HAIKU, u) == pytest.approx(0.10 + 0.50 + 0.01 + 0.125)
    assert custo(OPUS, Usage(input_tokens=1_000_000, output_tokens=0)) == pytest.approx(4.0)
    assert custo(HAIKU, u, batch=True) == pytest.approx((0.10 + 0.50 + 0.01 + 0.125) / 2)
    assert custo("modelo-desconhecido", u) == 0.0


def test_registra_custo_e_respeita_o_teto_mensal(engine: Engine) -> None:
    c = cliente(engine, lambda kw: Resposta(fit()), limite=0.0005)
    c.estruturado(ResultadoFit, operacao="scoring", modelo=HAIKU, max_tokens=10, messages=[])
    # 1000 in x 0.10 + 200 out x 0.50 = 0.0002 por chamada
    assert c.gasto_no_mes() == pytest.approx(0.0002)
    c.estruturado(ResultadoFit, operacao="scoring", modelo=HAIKU, max_tokens=10, messages=[])
    c.estruturado(ResultadoFit, operacao="scoring", modelo=HAIKU, max_tokens=10, messages=[])
    with pytest.raises(LimiteDeCustoAtingido):
        c.estruturado(ResultadoFit, operacao="scoring", modelo=HAIKU, max_tokens=10, messages=[])
    assert len(c.anthropic.messages.chamadas) == 3  # a quarta nem foi feita


def test_gasto_do_mes_anterior_nao_conta(engine: Engine) -> None:
    passado = ClienteLLM(
        engine,
        None,
        limite_mensal_usd=1,
        relogio=lambda: datetime(2026, 10, 31, 23, 59, tzinfo=UTC),
    )
    passado.registrar("scoring", OPUS, Usage(input_tokens=1_000_000, output_tokens=0))
    assert passado.gasto_no_mes() == pytest.approx(4.0)
    assert cliente(engine, lambda kw: Resposta(fit())).gasto_no_mes() == 0.0


@pytest.mark.parametrize(
    "resposta",
    [
        Resposta(None, stop_reason="refusal"),
        Resposta(None, stop_reason="max_tokens"),
        Resposta(None),
    ],
)
def test_resposta_invalida_levanta_mas_registra_o_custo(engine: Engine, resposta: Resposta) -> None:
    c = cliente(engine, lambda kw: resposta)
    with pytest.raises(RespostaInvalida):
        c.estruturado(ResultadoFit, operacao="scoring", modelo=HAIKU, max_tokens=10, messages=[])
    assert c.gasto_no_mes() > 0


# --- extração de perfil -------------------------------------------------------


def test_extrair_perfil_envia_o_pdf_e_devolve_os_dados(engine: Engine, tmp_path: Path) -> None:
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    esperado = ExtracaoPerfil(
        dados=DadosPerfil(resumo="Arquiteto cloud", cargos=["Cloud Architect"]),
        termos_busca=["Cloud Architect"],
    )
    c = cliente(engine, lambda kw: Resposta(esperado))

    assert extrair_perfil(c, pdf, modelo=OPUS) == esperado

    chamada = c.anthropic.messages.chamadas[0]
    assert chamada["model"] == OPUS
    assert chamada["output_format"] is ExtracaoPerfil
    assert chamada["output_config"] == {"effort": "high"}
    documento = chamada["messages"][0]["content"][0]
    assert documento["type"] == "document"
    assert documento["source"]["media_type"] == "application/pdf"
    with sessao(engine) as s:
        assert s.scalars(select(CustoLLMDB.operacao)).all() == ["extrair_perfil"]


# --- BM25 ---------------------------------------------------------------------


def test_bm25_prefere_documento_com_mais_termos_da_consulta() -> None:
    docs = [["aws", "terraform", "kubernetes"], ["excel", "vendas"], ["aws", "vendas"]]
    s = bm25(["aws", "terraform"], docs)
    assert s[0] > s[2] > s[1] == 0.0
    assert bm25(["aws"], []) == []


def test_preranquear_usa_titulo_skills_e_queries() -> None:
    perfil = Perfil(
        nome="d",
        dados=DadosPerfil(cargos=["Cloud Architect"], skills=[Skill(nome="AWS", peso=5)]),
        queries=[Query(termo="DevOps Lead")],
    )
    vagas = [
        Vaga(titulo="Brand Manager", url="https://x/1", descricao="marketing de moda"),
        Vaga(titulo="Cloud Architect", url="https://x/2", descricao="AWS, landing zones"),
        Vaga(titulo="DevOps Lead", url="https://x/3", descricao="CI/CD"),
    ]
    s = preranquear(perfil, vagas)
    assert s[1] > s[2] > s[0]


# --- scoring ------------------------------------------------------------------


def preparar(engine: Engine, vagas: list[VagaBruta]) -> int:
    with sessao(engine) as s:
        repo.salvar_perfil(
            s,
            Perfil(
                nome="daniel",
                dados=DadosPerfil(
                    resumo="Arquiteto cloud sênior",
                    cargos=["Cloud Architect"],
                    skills=[Skill(nome="AWS", peso=5), Skill(nome="Terraform", peso=4)],
                    idiomas=[Idioma(codigo="pt", nivel="nativo")],
                ),
                queries=[Query(termo="Cloud Architect")],
                filtros=Filtros(remoto_a_partir_de=["PT"], idiomas=["pt", "en"]),
                preferencias="Nada de consultoria body shop.",
            ),
        )

    class Fonte:
        nome = "f"
        limite_diario = None

        def buscar(self, queries: object, quota: Quota) -> list[VagaBruta]:
            return vagas

    with sessao(engine) as s:
        executar_rodada(s, engine, [Fonte()], repo.listar_perfis(s), AGORA)
        perfil = repo.buscar_perfil(s, "daniel")
        assert perfil is not None
        return perfil.id


def bruta(id_fonte: str, titulo: str, descricao: str = "", **kw: Any) -> VagaBruta:
    base: dict[str, Any] = {
        "fonte": "f",
        "id_fonte": id_fonte,
        "titulo": titulo,
        "empresa": f"Empresa {id_fonte}",
        "descricao": descricao,
        "local": "Europe",
        "modalidade": "remote",
        "url": f"https://f.example/{id_fonte}",
        "publicada_em": AGORA - timedelta(days=1),
    }
    return VagaBruta.model_validate(base | kw)


VAGAS = [
    bruta("1", "Cloud Architect", "AWS Terraform landing zones"),
    bruta("2", "Brand Manager", "Fashion marketing"),
    bruta("3", "Senior Cloud Engineer", "AWS"),
]


def pontuar(engine: Engine, c: ClienteLLM, perfil_id: int, top_n: int = 10) -> Any:
    return pontuar_pendentes(engine, c, perfil_id, modelo=HAIKU, top_n=top_n, agora=AGORA)


def matches(engine: Engine) -> dict[str, MatchDB]:
    with sessao(engine) as s:
        todos = s.scalars(select(MatchDB)).all()
        for m in todos:
            _ = m.vaga.titulo
        s.expunge_all()
        return {m.vaga.titulo: m for m in todos}


def test_pontua_as_top_n_do_bm25_e_grava_o_resultado(engine: Engine) -> None:
    perfil_id = preparar(engine, VAGAS)
    c = cliente(
        engine,
        lambda kw: Resposta(fit(91, pontos_fortes=["AWS"], gaps=["Azure"])),
    )

    r = pontuar(engine, c, perfil_id, top_n=2)

    assert (r.pendentes, r.avaliadas, r.falhas) == (3, 2, [])
    m = matches(engine)
    # O BM25 manda as duas vagas de cloud para o LLM e deixa a de marketing pendente.
    assert m["Cloud Architect"].status == StatusMatch.NOVA
    assert m["Cloud Architect"].score == 91
    assert m["Cloud Architect"].pontos_fortes == ["AWS"]
    assert m["Cloud Architect"].modelo == HAIKU
    assert m["Senior Cloud Engineer"].status == StatusMatch.NOVA
    assert m["Brand Manager"].status == StatusMatch.PENDENTE
    assert m["Brand Manager"].score_prerank is not None
    assert m["Cloud Architect"].score_prerank > m["Brand Manager"].score_prerank  # type: ignore[operator]

    with sessao(engine) as s:
        custos = s.scalars(select(CustoLLMDB)).all()
        assert [(x.operacao, x.perfil_id) for x in custos] == [("scoring", perfil_id)] * 2


def test_prefixo_do_prompt_e_estavel_e_marcado_para_cache(engine: Engine) -> None:
    perfil_id = preparar(engine, VAGAS)
    c = cliente(engine, lambda kw: Resposta(fit()))
    pontuar(engine, c, perfil_id)

    chamadas = c.anthropic.messages.chamadas
    assert len(chamadas) == 3
    assert all(ch["system"] == chamadas[0]["system"] for ch in chamadas)  # mesmo prefixo
    instrucoes, perfil = chamadas[0]["system"]
    assert instrucoes["text"] == INSTRUCOES
    assert perfil["cache_control"] == {"type": "ephemeral"}
    assert "Terraform" in perfil["text"] and "body shop" in perfil["text"]
    assert chamadas[0]["output_config"] == {"effort": "low"}
    # Só a vaga muda, na mensagem do usuário, delimitada como dado.
    assert len({ch["messages"][0]["content"] for ch in chamadas}) == 3
    assert chamadas[0]["messages"][0]["content"].startswith("<vaga>")


def test_falha_deixa_pendente_e_nao_para_as_outras(engine: Engine) -> None:
    perfil_id = preparar(engine, VAGAS)

    def responder(kw: dict[str, Any]) -> Resposta:
        conteudo = kw["messages"][0]["content"]
        if "Brand Manager" in conteudo:
            return Resposta(None, stop_reason="refusal")
        if "Senior Cloud Engineer" in conteudo:
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api"))
        return Resposta(fit())

    r = pontuar(engine, cliente(engine, responder), perfil_id)

    assert r.avaliadas == 1
    assert len(r.falhas) == 2
    m = matches(engine)
    assert m["Cloud Architect"].status == StatusMatch.NOVA
    assert (m["Brand Manager"].status, m["Brand Manager"].tentativas) == (StatusMatch.PENDENTE, 1)
    assert m["Senior Cloud Engineer"].tentativas == 1


def test_nao_reavalia_e_desiste_apos_max_tentativas(engine: Engine) -> None:
    perfil_id = preparar(engine, VAGAS[:1])
    recusa = cliente(engine, lambda kw: Resposta(None, stop_reason="refusal"))
    for _ in range(MAX_TENTATIVAS):
        pontuar(engine, recusa, perfil_id)
    assert len(recusa.anthropic.messages.chamadas) == MAX_TENTATIVAS

    depois = cliente(engine, lambda kw: Resposta(fit()))
    assert pontuar(engine, depois, perfil_id).pendentes == 0
    assert depois.anthropic.messages.chamadas == []


def test_vaga_ja_pontuada_nao_volta_para_o_llm(engine: Engine) -> None:
    perfil_id = preparar(engine, VAGAS)
    pontuar(engine, cliente(engine, lambda kw: Resposta(fit())), perfil_id)
    segunda = cliente(engine, lambda kw: Resposta(fit()))
    assert pontuar(engine, segunda, perfil_id).avaliadas == 0
    assert segunda.anthropic.messages.chamadas == []


def test_teto_de_custo_interrompe_o_scoring(engine: Engine) -> None:
    perfil_id = preparar(engine, VAGAS)
    c = cliente(engine, lambda kw: Resposta(fit()), limite=0.0003)  # cabem 2 chamadas
    r = pontuar(engine, c, perfil_id)
    assert r.avaliadas == 2
    assert r.interrompido and "teto mensal" in r.interrompido
    assert sum(m.status == StatusMatch.PENDENTE for m in matches(engine).values()) == 1


def _erro(classe: type[anthropic.APIStatusError], status: int, msg: str) -> Exception:
    resposta = httpx.Response(status, request=httpx.Request("POST", "https://api"))
    return classe(msg, response=resposta, body={"error": {"message": msg}})


@pytest.mark.parametrize(
    "erro",
    [
        _erro(anthropic.AuthenticationError, 401, "invalid x-api-key"),
        _erro(anthropic.BadRequestError, 400, "Your credit balance is too low to access"),
    ],
)
def test_erro_da_conta_interrompe_sem_gastar_tentativas(engine: Engine, erro: Exception) -> None:
    perfil_id = preparar(engine, VAGAS)

    def responder(kw: dict[str, Any]) -> Resposta:
        raise erro

    c = cliente(engine, responder)
    with pytest.raises(type(erro)):
        pontuar(engine, c, perfil_id)
    assert len(c.anthropic.messages.chamadas) == 1
    assert all(m.tentativas == 0 for m in matches(engine).values())


def test_cli_mostra_erro_da_api_sem_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from job_finder.config import get_settings
    from job_finder.db.session import upgrade
    from job_finder.llm import client as client_mod

    upgrade(get_settings().resolved_database_url)

    def responder(kw: dict[str, Any]) -> Resposta:
        raise _erro(anthropic.BadRequestError, 400, "Your credit balance is too low")

    monkeypatch.setattr(
        client_mod,
        "criar_cliente",
        lambda eng: ClienteLLM(eng, Anthropic(responder), limite_mensal_usd=5),
    )
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    r = CliRunner().invoke(cli_app, ["perfil", "importar", str(pdf), "-p", "x"])
    assert r.exit_code == 1
    assert "credit balance is too low" in r.output
    assert "Traceback" not in r.output


# --- CLI ----------------------------------------------------------------------


def test_cli_importar_preenche_o_perfil(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from job_finder.config import get_settings
    from job_finder.db.session import criar_engine, upgrade
    from job_finder.llm import client as client_mod

    url = get_settings().resolved_database_url
    upgrade(url)
    engine = criar_engine(url)
    extracao = ExtracaoPerfil(
        dados=DadosPerfil(resumo="Arquiteta de marca", cargos=["Brand Manager"]),
        termos_busca=["Brand Manager", "Marketing Manager"],
    )
    monkeypatch.setattr(
        client_mod,
        "criar_cliente",
        lambda eng: ClienteLLM(eng, Anthropic(lambda kw: Resposta(extracao)), limite_mensal_usd=5),
    )
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    runner = CliRunner()
    r = runner.invoke(cli_app, ["perfil", "importar", str(pdf), "--perfil", "esposa"])
    assert r.exit_code == 0, r.output

    with sessao(engine) as s:
        db = repo.buscar_perfil(s, "esposa")
        assert db is not None
        perfil = repo.perfil_de(db)
        assert db.curriculo_path and Path(db.curriculo_path).exists()
    assert perfil.dados.cargos == ["Brand Manager"]
    assert [(q.termo, q.ativa) for q in perfil.queries] == [
        ("Brand Manager", True),
        ("Marketing Manager", True),
    ]

    # Exportar e carregar de volta preserva tudo; YAML sem `dados` não apaga os extraídos.
    exportado = runner.invoke(cli_app, ["perfil", "exportar", "esposa"])
    assert exportado.exit_code == 0, exportado.output
    yaml_sem_dados = tmp_path / "esposa.yaml"
    yaml_sem_dados.write_text("nome: esposa\nqueries:\n  - termo: Brand Manager\n")
    assert runner.invoke(cli_app, ["perfil", "carregar", str(yaml_sem_dados)]).exit_code == 0
    with sessao(engine) as s:
        db = repo.buscar_perfil(s, "esposa")
        assert db is not None
        assert repo.perfil_de(db).dados.cargos == ["Brand Manager"]
    assert "Arquiteta de marca" in exportado.output


def test_cli_pontuar_sem_chave_falha_com_mensagem() -> None:
    r = CliRunner().invoke(cli_app, ["pontuar"])
    assert r.exit_code != 0
    assert "ANTHROPIC_API_KEY" in r.output


def test_bloco_do_perfil_traz_o_minimo_por_contrato() -> None:
    from job_finder.domain import Salario
    from job_finder.llm.scorer import bloco_perfil

    filtros = Filtros(
        salario_minimo_por_contrato={"contrato": Salario(minimo=30_000, moeda="BRL", periodo="mes")}
    )
    bloco = bloco_perfil(Perfil(nome="x", filtros=filtros))
    assert "PJ / contractor" in bloco and "30000" in bloco
