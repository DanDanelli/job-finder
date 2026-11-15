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


def test_migracoes_preservam_dados_das_tabelas_filhas(db_url: str) -> None:
    """O modo batch recria tabelas; com FKs ligadas isso apagava os matches em cascata."""
    from sqlalchemy import create_engine, text

    from job_finder.db.session import upgrade

    upgrade(db_url, "0001")
    bruto = create_engine(db_url)
    with bruto.begin() as c:
        c.execute(
            text(
                "INSERT INTO perfis (id, nome, dados, queries, filtros, preferencias, "
                "criado_em, atualizado_em) VALUES (1, 'p', '{}', '[]', '{}', '', "
                "'2026-11-15', '2026-11-15')"
            )
        )
        c.execute(
            text(
                "INSERT INTO vagas (id, chave_dedup, titulo, modalidade, url, criado_em, "
                "atualizado_em) VALUES (1, 'k', 't', 'remoto', 'https://x', "
                "'2026-11-15', '2026-11-15')"
            )
        )
        c.execute(
            text(
                "INSERT INTO vaga_fontes (vaga_id, fonte, id_fonte, url, dados_brutos, "
                "coletada_em) VALUES (1, 'f', '1', 'https://x', '{}', '2026-11-15')"
            )
        )
        c.execute(
            text(
                "INSERT INTO matches (vaga_id, perfil_id, status, pontos_fortes, gaps, "
                "dealbreakers, tentativas, criado_em, atualizado_em) VALUES (1, 1, "
                "'nova', '[]', '[]', '[]', 0, '2026-11-15', '2026-11-15')"
            )
        )
    bruto.dispose()

    upgrade(db_url)  # 0002 recria `vagas`, 0003 recria `perfis`

    bruto = create_engine(db_url)
    with bruto.connect() as c:
        assert c.execute(text("SELECT count(*) FROM matches")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM vaga_fontes")).scalar() == 1
    bruto.dispose()
