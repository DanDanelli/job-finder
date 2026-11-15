import pytest
from pydantic import ValidationError

from job_finder.domain import Filtros, Perfil, Query, ResultadoFit, Skill


def test_resultado_fit_limita_score() -> None:
    assert ResultadoFit(score=85, resumo="ok").score == 85
    for invalido in (-1, 101):
        with pytest.raises(ValidationError):
            ResultadoFit(score=invalido, resumo="x")


def test_skill_peso_entre_1_e_5() -> None:
    with pytest.raises(ValidationError):
        Skill(nome="AWS", peso=6)


def test_codigo_de_pais_iso2_maiusculo() -> None:
    assert Query(termo="Cloud Architect", paises=["DE", "PT"]).paises == ["DE", "PT"]
    with pytest.raises(ValidationError):
        Query(termo="x", paises=["deu"])


def test_perfil_tem_defaults_utilizaveis() -> None:
    perfil = Perfil(nome="daniel")
    assert perfil.filtros == Filtros()
    assert perfil.filtros.idade_maxima_dias == 14
    assert perfil.queries == []


def test_perfil_ida_e_volta_em_json() -> None:
    perfil = Perfil(
        nome="daniel",
        telegram_chat_id=8_000_000_000,
        queries=[Query(termo="DevOps Lead", paises=["DE"])],
    )
    assert Perfil.model_validate_json(perfil.model_dump_json()) == perfil
