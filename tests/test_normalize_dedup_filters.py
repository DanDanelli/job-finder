from datetime import UTC, datetime, timedelta

import pytest

from job_finder.domain import (
    Filtros,
    Modalidade,
    Salario,
    TipoContrato,
    Vaga,
    VagaBruta,
)
from job_finder.pipeline.dedup import chave_dedup, empresa_canonica, titulo_canonico
from job_finder.pipeline.filters import motivo_descarte
from job_finder.pipeline.locais import WORLDWIDE
from job_finder.pipeline.normalize import normalizar

AGORA = datetime(2026, 11, 15, 12, tzinfo=UTC)


def bruta(**kw: object) -> VagaBruta:
    base: dict[str, object] = {
        "fonte": "teste",
        "id_fonte": "1",
        "titulo": "Cloud Architect",
        "empresa": "Acme",
        "url": "https://example.com/1",
    }
    return VagaBruta.model_validate(base | kw)


def vaga(**kw: object) -> Vaga:
    base: dict[str, object] = {
        "titulo": "Cloud Architect",
        "empresa": "Acme",
        "url": "https://example.com/1",
        "publicada_em": AGORA - timedelta(days=1),
    }
    return Vaga.model_validate(base | kw)


# --- normalização -------------------------------------------------------------


def test_normaliza_vaga_remota_do_remotive() -> None:
    v = normalizar(
        bruta(
            titulo="  Senior   Data Engineer ",
            empresa="Lemon.io ",
            local="LATAM, Europe, USA, Canada, APAC",
            modalidade="remote",
            tipo_contrato="full_time",
            salario_texto="$170k - $200k",
            publicada_em=datetime(2026, 11, 11, 20, 17),  # sem tz, como vem da API
        )
    )
    assert v.titulo == "Senior Data Engineer"
    assert v.empresa == "Lemon.io"
    assert v.modalidade == Modalidade.REMOTO
    assert v.tipo_contrato == TipoContrato.TEMPO_INTEGRAL
    assert v.locais_elegiveis == ["LATAM", "EUROPE", "US", "CA", "APAC"]
    assert v.pais is None
    assert v.salario == Salario(minimo=170_000, maximo=200_000, moeda="USD", periodo="ano")
    assert v.publicada_em == datetime(2026, 11, 11, 20, 17, tzinfo=UTC)


def test_pais_so_quando_o_local_e_um_unico_pais() -> None:
    assert normalizar(bruta(local="USA")).pais == "US"
    assert normalizar(bruta(local="USA, Canada")).pais is None


def test_worldwide_absorve_os_outros_locais() -> None:
    assert normalizar(bruta(local="Worldwide, USA")).locais_elegiveis == [WORLDWIDE]


def test_salario_estruturado_da_fonte_tem_prioridade() -> None:
    estruturado = Salario(minimo=1, maximo=2, moeda="EUR", periodo="ano")
    assert normalizar(bruta(salario=estruturado, salario_texto="$9k")).salario == estruturado


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Hybrid", Modalidade.HIBRIDO),
        ("On-site", Modalidade.PRESENCIAL),
        ("??", Modalidade.DESCONHECIDA),
    ],
)
def test_modalidade(texto: str, esperado: Modalidade) -> None:
    assert normalizar(bruta(modalidade=texto)).modalidade == esperado


# --- dedup --------------------------------------------------------------------


def test_titulo_canonico() -> None:
    assert titulo_canonico("Sr. Cloud Engineer (m/f/d)") == "senior cloud engineer"
    assert titulo_canonico("Senior Cloud Engineer (M/W/D)") == "senior cloud engineer"
    # Senioridade diferente é outra vaga.
    assert titulo_canonico("Jr. Cloud Engineer") != titulo_canonico("Sr. Cloud Engineer")


@pytest.mark.parametrize(
    ("nome", "esperado"),
    [
        ("Acme Inc.", "acme"),
        ("Acme GmbH", "acme"),
        ("Natura S.A.", "natura"),
        ("Itaú Unibanco Ltda", "itau unibanco"),
        ("Limited", "limited"),  # não apaga o nome inteiro
        (None, ""),
    ],
)
def test_empresa_canonica(nome: str | None, esperado: str) -> None:
    assert empresa_canonica(nome) == esperado


def test_mesma_vaga_em_fontes_diferentes_gera_mesma_chave() -> None:
    a = normalizar(bruta(titulo="Sr Cloud Architect (m/f/d)", empresa="Acme GmbH", local="Germany"))
    b = normalizar(bruta(titulo="Senior Cloud Architect", empresa="ACME", local="Deutschland"))
    assert chave_dedup(a) == chave_dedup(b) == "acme|senior cloud architect|de"


def test_chave_de_vaga_remota_sem_pais() -> None:
    v = normalizar(bruta(local="Worldwide", modalidade="remote"))
    assert chave_dedup(v) == "acme|cloud architect|remoto"


# --- filtros ------------------------------------------------------------------


def test_vaga_sem_filtros_passa() -> None:
    assert motivo_descarte(vaga(), Filtros(), AGORA) is None


def test_vaga_antiga() -> None:
    antiga = vaga(publicada_em=AGORA - timedelta(days=30))
    assert motivo_descarte(antiga, Filtros(idade_maxima_dias=14), AGORA) == "antiga"
    assert motivo_descarte(vaga(publicada_em=None), Filtros(), AGORA) is None


def test_modalidade_desconhecida_passa() -> None:
    filtros = Filtros(modalidades=[Modalidade.REMOTO])
    assert motivo_descarte(vaga(modalidade="presencial"), filtros, AGORA) == "modalidade"
    assert motivo_descarte(vaga(modalidade="desconhecida"), filtros, AGORA) is None


def test_remoto_precisa_aceitar_quem_mora_no_pais() -> None:
    filtros = Filtros(remoto_a_partir_de=["PT"])
    so_eua = vaga(modalidade="remoto", locais_elegiveis=["US"])
    europa = vaga(modalidade="remoto", locais_elegiveis=["LATAM", "EUROPE"])
    desconhecido = vaga(modalidade="remoto", locais_elegiveis=[])
    assert motivo_descarte(so_eua, filtros, AGORA) == "local"
    assert motivo_descarte(europa, filtros, AGORA) is None
    assert motivo_descarte(desconhecido, filtros, AGORA) is None


def test_pais_vale_para_presencial_e_nao_para_remoto() -> None:
    filtros = Filtros(paises=["PT", "DE"])
    assert motivo_descarte(vaga(modalidade="presencial", pais="US"), filtros, AGORA) == "pais"
    assert motivo_descarte(vaga(modalidade="hibrido", pais="DE"), filtros, AGORA) is None
    assert motivo_descarte(vaga(modalidade="remoto", pais="US"), filtros, AGORA) is None


def test_tipo_de_contrato() -> None:
    filtros = Filtros(tipos_contrato=[TipoContrato.TEMPO_INTEGRAL])
    assert motivo_descarte(vaga(tipo_contrato="freelance"), filtros, AGORA) == "tipo_contrato"
    assert motivo_descarte(vaga(tipo_contrato=None), filtros, AGORA) is None


@pytest.mark.parametrize(
    ("salario", "motivo"),
    [
        (Salario(minimo=30_000, maximo=40_000, moeda="EUR", periodo="ano"), "salario"),
        (Salario(minimo=40_000, maximo=60_000, moeda="EUR", periodo="ano"), None),  # teto passa
        (Salario(minimo=3_000, maximo=4_000, moeda="EUR", periodo="mes"), "salario"),  # 48k < 54k
        (Salario(minimo=3_000, maximo=4_500, moeda="EUR", periodo="mes"), None),  # 54k: na margem
        (Salario(minimo=5_000, maximo=5_500, moeda="EUR", periodo="mes"), None),  # 66k
        (Salario(minimo=10, maximo=20, moeda="USD", periodo="hora"), None),  # moeda diferente
        (Salario(minimo=10, maximo=20, moeda="EUR", periodo="hora"), None),  # hora: o LLM avalia
        (Salario(minimo=10, maximo=20, moeda="EUR", periodo=None), None),  # período desconhecido
        (None, None),  # sem salário
    ],
)
def test_salario_minimo(salario: Salario | None, motivo: str | None) -> None:
    filtros = Filtros(salario_minimo=Salario(minimo=60_000, moeda="EUR", periodo="ano"))
    assert motivo_descarte(vaga(salario=salario), filtros, AGORA) == motivo


def test_empresa_bloqueada_ignora_sufixo_e_caixa() -> None:
    filtros = Filtros(empresas_bloqueadas=["acme"])
    assert motivo_descarte(vaga(empresa="ACME Inc."), filtros, AGORA) == "empresa_bloqueada"
    assert motivo_descarte(vaga(empresa="Acme Labs"), filtros, AGORA) is None


def test_palavra_bloqueada_so_no_titulo_e_palavra_inteira() -> None:
    filtros = Filtros(palavras_bloqueadas=["intern", "Estágio"])
    assert motivo_descarte(vaga(titulo="Cloud Intern"), filtros, AGORA) == "palavra_bloqueada"
    assert motivo_descarte(vaga(titulo="Estagio em Dados"), filtros, AGORA) == "palavra_bloqueada"
    assert motivo_descarte(vaga(titulo="International Sales"), filtros, AGORA) is None
    assert motivo_descarte(vaga(descricao="intern"), filtros, AGORA) is None


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Contagem - MG, BR", ["BR"]),  # MG seria Madagascar
        ("Vitória, ES, Brasil", ["BR"]),  # ES seria Espanha
        ("Madrid, ES", ["ES"]),  # sem o Brasil explícito, ES continua Espanha
    ],
)
def test_sigla_de_estado_brasileiro_nao_vira_pais(texto: str, esperado: list[str]) -> None:
    from job_finder.pipeline.locais import interpretar_local

    assert interpretar_local(texto) == esperado


@pytest.mark.parametrize(
    ("contrato", "mensal", "motivo"),
    [
        ("tempo_integral", 22_000, None),  # CLT acima de 20k
        ("tempo_integral", 19_000, None),  # dentro da margem de 10%
        ("tempo_integral", 17_000, "salario"),
        ("contrato", 25_000, "salario"),  # PJ abaixo de 30k
        ("contrato", 32_000, None),
        (None, 22_000, None),  # sem tipo: usa o mínimo geral (20k)
    ],
)
def test_salario_minimo_por_tipo_de_contrato(
    contrato: str | None, mensal: int, motivo: str | None
) -> None:
    filtros = Filtros(
        salario_minimo=Salario(minimo=20_000, moeda="BRL", periodo="mes"),
        salario_minimo_por_contrato={
            "tempo_integral": Salario(minimo=20_000, moeda="BRL", periodo="mes"),
            "contrato": Salario(minimo=30_000, moeda="BRL", periodo="mes"),
        },
    )
    salario = Salario(minimo=mensal, maximo=mensal, moeda="BRL", periodo="mes")
    assert motivo_descarte(vaga(salario=salario, tipo_contrato=contrato), filtros, AGORA) == motivo


def test_valor_por_hora_nunca_descarta_no_codigo() -> None:
    filtros = Filtros(
        salario_minimo_por_contrato={
            "contrato": Salario(minimo=30_000, moeda="BRL", periodo="mes"),
        }
    )
    hora = Salario(minimo=90, maximo=90, moeda="BRL", periodo="hora")
    assert motivo_descarte(vaga(salario=hora, tipo_contrato="contrato"), filtros, AGORA) is None


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("R$ 150", Salario(minimo=150, maximo=150, moeda="BRL", periodo="hora")),
        ("R$ 120 a R$ 180 por hora", Salario(minimo=120, maximo=180, moeda="BRL", periodo="hora")),
        ("R$ 25.000", Salario(minimo=25_000, maximo=25_000, moeda="BRL", periodo="mes")),
    ],
)
def test_salario_em_reais_sem_periodo(texto: str, esperado: Salario) -> None:
    from job_finder.pipeline.salario import interpretar_salario

    assert interpretar_salario(texto) == esperado
