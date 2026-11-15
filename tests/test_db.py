import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from job_finder.db.models import Base, MatchDB, PerfilDB, VagaDB
from job_finder.db.session import sessao

TABELAS = {
    "perfis",
    "vagas",
    "vaga_fontes",
    "matches",
    "feedback",
    "execucoes",
    "uso_quota",
    "custo_llm",
}


def test_migracao_cria_todas_as_tabelas(engine: Engine) -> None:
    assert set(inspect(engine).get_table_names()) >= TABELAS


def test_migracoes_estao_em_dia_com_os_models(engine: Engine) -> None:
    """Falha se alguém mudou `models.py` sem gerar uma migração nova."""
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_sqlite_em_wal_e_com_foreign_keys(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def _vaga(chave: str = "acme|cloud architect|de") -> VagaDB:
    return VagaDB(
        chave_dedup=chave,
        titulo="Cloud Architect",
        empresa="Acme",
        pais="DE",
        modalidade="remoto",
        url="https://example.com/vaga/1",
    )


def test_match_e_unico_por_vaga_e_perfil(engine: Engine) -> None:
    with sessao(engine) as s:
        perfil = PerfilDB(nome="daniel")
        vaga = _vaga()
        s.add_all([perfil, vaga])
        s.flush()
        s.add(MatchDB(vaga_id=vaga.id, perfil_id=perfil.id, status="nova"))

    with pytest.raises(IntegrityError), sessao(engine) as s:
        s.add(MatchDB(vaga_id=vaga.id, perfil_id=perfil.id, status="nova"))


def test_chave_dedup_e_unica(engine: Engine) -> None:
    with sessao(engine) as s:
        s.add(_vaga())
    with pytest.raises(IntegrityError), sessao(engine) as s:
        s.add(_vaga())


def test_foreign_key_e_aplicada(engine: Engine) -> None:
    with pytest.raises(IntegrityError), sessao(engine) as s:
        s.add(MatchDB(vaga_id=999, perfil_id=999, status="nova"))
