"""Parsers de texto livre: local, salário, HTML. Casos tirados de vagas reais."""

import pytest

from job_finder.domain import Salario
from job_finder.pipeline.locais import WORLDWIDE, elegivel, interpretar_local
from job_finder.pipeline.salario import anual, interpretar_salario
from job_finder.texto import chave, html_para_texto


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Worldwide", [WORLDWIDE]),
        ("USA", ["US"]),
        ("LATAM, Europe, USA, Canada, APAC", ["LATAM", "EUROPE", "US", "CA", "APAC"]),
        (
            "Europe, USA, Canada, Australia, Singapore, Mexico, Saudi Arabia",
            ["EUROPE", "US", "CA", "AU", "SG", "MX", "SA"],
        ),
        ("USA, Canada, USA timezones", ["US", "CA"]),
        ("Remote - Europe", ["EUROPE"]),
        ("Germany or Netherlands", ["DE", "NL"]),
        ("Brasil", ["BR"]),
        ("UK only", ["GB"]),
        ("Lisboa, Portugal", ["PT"]),  # cidade ignorada
        ("Remote", []),  # ambíguo: fica desconhecido
        ("", []),
        (None, []),
    ],
)
def test_interpretar_local(texto: str | None, esperado: list[str]) -> None:
    assert interpretar_local(texto) == esperado


@pytest.mark.parametrize(
    ("locais", "paises", "esperado"),
    [
        ([WORLDWIDE], ["PT"], True),
        (["US"], ["PT"], False),
        (["LATAM", "EUROPE"], ["PT"], True),
        (["EU"], ["GB"], False),  # Reino Unido não é UE
        (["EUROPE"], ["GB"], True),
        (["EMEA"], ["PT"], True),
        (["US", "CA"], ["BR", "PT"], False),
        (["LATAM"], ["BR", "PT"], True),
        ([], ["PT"], None),
    ],
)
def test_elegivel(locais: list[str], paises: list[str], esperado: bool | None) -> None:
    assert elegivel(locais, paises) is esperado


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("$35,3k- $52k", Salario(minimo=35_300, maximo=52_000, moeda="USD", periodo="ano")),
        ("$14 - $20/hour", Salario(minimo=14, maximo=20, moeda="USD", periodo="hora")),
        ("OTE $25k - $35k", Salario(minimo=25_000, maximo=35_000, moeda="USD", periodo="ano")),
        ("$50-$75 /hour", Salario(minimo=50, maximo=75, moeda="USD", periodo="hora")),
        ("$14/hour", Salario(minimo=14, maximo=14, moeda="USD", periodo="hora")),
        ("$3.5k–$5k/month", Salario(minimo=3_500, maximo=5_000, moeda="USD", periodo="mes")),  # noqa: RUF001
        ("70K-80K", Salario(minimo=70_000, maximo=80_000, moeda=None, periodo="ano")),
        ("$170k - $200k", Salario(minimo=170_000, maximo=200_000, moeda="USD", periodo="ano")),
        (
            "$100,000 - $120,000",
            Salario(minimo=100_000, maximo=120_000, moeda="USD", periodo="ano"),
        ),
        ("€4.000/mês", Salario(minimo=4_000, maximo=4_000, moeda="EUR", periodo="mes")),
        ("R$ 8.000 - R$ 12.000", Salario(minimo=8_000, maximo=12_000, moeda="BRL", periodo="mes")),
        ("60-70k EUR", Salario(minimo=60_000, maximo=70_000, moeda="EUR", periodo="ano")),
    ],
)
def test_interpretar_salario(texto: str, esperado: Salario) -> None:
    assert interpretar_salario(texto) == esperado


@pytest.mark.parametrize("texto", ["$1000 per task", "Competitive", "", None])
def test_salario_nao_interpretavel(texto: str | None) -> None:
    assert interpretar_salario(texto) is None


def test_anual() -> None:
    assert anual(20, "hora") == 41_600
    assert anual(4_000, "mes") == 48_000
    assert anual(50_000, "ano") == 50_000


def test_html_para_texto() -> None:
    html = "<p>Olá <b>mundo</b></p><script>x()</script><ul><li>AWS</li><li>Terraform</li></ul>"
    assert html_para_texto(html) == "Olá\nmundo\nAWS\nTerraform"
    assert html_para_texto("") is None
    assert html_para_texto("<p>   </p>") is None


def test_chave() -> None:
    assert chave("  Engenheiro(a) de Dados — São Paulo! ") == "engenheiro a de dados sao paulo"
