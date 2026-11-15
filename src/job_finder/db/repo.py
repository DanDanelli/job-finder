"""Acesso a dados: conversão entre tabelas e modelos de domínio."""

from datetime import datetime

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from job_finder.db.models import MatchDB, PerfilDB, VagaDB, VagaFonteDB
from job_finder.domain import (
    DadosPerfil,
    Filtros,
    Modalidade,
    Perfil,
    Query,
    Salario,
    TipoContrato,
    Vaga,
    VagaBruta,
)

FONTE_MANUAL = "manual"  # avaliação avulsa pedida pelo bot

# --- perfis -------------------------------------------------------------------


def salvar_perfil(s: Session, perfil: Perfil) -> PerfilDB:
    """Cria ou atualiza o perfil pelo nome."""
    db = buscar_perfil(s, perfil.nome)
    if db is None:
        db = PerfilDB(nome=perfil.nome)
        s.add(db)
    db.telegram_chat_id = perfil.telegram_chat_id
    db.dados = perfil.dados.model_dump(mode="json")
    db.queries = [q.model_dump(mode="json") for q in perfil.queries]
    db.filtros = perfil.filtros.model_dump(mode="json")
    db.preferencias = perfil.preferencias
    s.flush()
    return db


def buscar_perfil(s: Session, nome: str) -> PerfilDB | None:
    return s.scalar(select(PerfilDB).where(PerfilDB.nome == nome))


def perfil_de(db: PerfilDB) -> Perfil:
    return Perfil(
        nome=db.nome,
        telegram_chat_id=db.telegram_chat_id,
        dados=DadosPerfil.model_validate(db.dados or {}),
        queries=[Query.model_validate(q) for q in db.queries or []],
        filtros=Filtros.model_validate(db.filtros or {}),
        preferencias=db.preferencias,
    )


def listar_perfis(
    s: Session, nomes: list[str] | None = None, *, incluir_pausados: bool = True
) -> list[PerfilDB]:
    consulta = select(PerfilDB).order_by(PerfilDB.nome)
    if nomes:
        consulta = consulta.where(PerfilDB.nome.in_(nomes))
    if not incluir_pausados:
        consulta = consulta.where(PerfilDB.pausado.is_(False))
    return list(s.scalars(consulta))


def perfil_do_chat(s: Session, chat_id: int) -> PerfilDB | None:
    return s.scalar(select(PerfilDB).where(PerfilDB.telegram_chat_id == chat_id))


# --- vagas --------------------------------------------------------------------


def registrar_vaga(s: Session, bruta: VagaBruta, vaga: Vaga, chave: str) -> tuple[VagaDB, bool]:
    """Grava a vaga (ou reaproveita a existente pela chave) e a ocorrência na fonte.

    Devolve (vaga, nova), onde `nova` indica que a vaga não existia no banco.
    """
    fonte = s.scalar(
        select(VagaFonteDB).where(
            VagaFonteDB.fonte == bruta.fonte, VagaFonteDB.id_fonte == bruta.id_fonte
        )
    )
    if fonte is not None:
        return fonte.vaga, False

    db = s.scalar(select(VagaDB).where(VagaDB.chave_dedup == chave))
    nova = db is None
    if db is None:
        db = _vaga_db(vaga, chave)
        s.add(db)
        s.flush()
    s.add(
        VagaFonteDB(
            vaga_id=db.id,
            fonte=bruta.fonte,
            id_fonte=bruta.id_fonte,
            url=str(bruta.url),
            dados_brutos=bruta.dados_brutos,
        )
    )
    s.flush()
    return db, nova


def _vaga_db(vaga: Vaga, chave: str) -> VagaDB:
    salario = vaga.salario or Salario()
    return VagaDB(
        chave_dedup=chave,
        titulo=vaga.titulo,
        empresa=vaga.empresa,
        pais=vaga.pais,
        cidade=vaga.cidade,
        local_texto=vaga.local_texto,
        locais_elegiveis=list(vaga.locais_elegiveis),
        modalidade=vaga.modalidade.value,
        tipo_contrato=vaga.tipo_contrato.value if vaga.tipo_contrato else None,
        descricao=vaga.descricao,
        salario_min=salario.minimo,
        salario_max=salario.maximo,
        salario_moeda=salario.moeda,
        salario_periodo=salario.periodo,
        publicada_em=vaga.publicada_em,
        url=str(vaga.url),
        url_candidatura=str(vaga.url_candidatura) if vaga.url_candidatura else None,
    )


def vaga_de(db: VagaDB) -> Vaga:
    tem_salario = db.salario_min is not None or db.salario_max is not None
    return Vaga(
        titulo=db.titulo,
        empresa=db.empresa,
        pais=db.pais,
        cidade=db.cidade,
        local_texto=db.local_texto,
        locais_elegiveis=db.locais_elegiveis or [],
        modalidade=Modalidade(db.modalidade),
        tipo_contrato=TipoContrato(db.tipo_contrato) if db.tipo_contrato else None,
        descricao=db.descricao,
        salario=Salario(
            minimo=db.salario_min,
            maximo=db.salario_max,
            moeda=db.salario_moeda,
            periodo=db.salario_periodo,
        )
        if tem_salario
        else None,
        publicada_em=db.publicada_em,
        url=db.url,
        url_candidatura=db.url_candidatura,
    )


def candidatas(s: Session, perfil_id: int, desde: datetime) -> list[VagaDB]:
    """Vagas recentes que ainda não têm match para o perfil.

    Vagas que só chegaram por avaliação avulsa (fonte "manual") ficam de fora: são de
    quem pediu, não entram nas rodadas dos outros perfis.
    """
    sem_match = ~exists().where(and_(MatchDB.vaga_id == VagaDB.id, MatchDB.perfil_id == perfil_id))
    de_alguma_fonte = exists().where(
        and_(VagaFonteDB.vaga_id == VagaDB.id, VagaFonteDB.fonte != FONTE_MANUAL)
    )
    recente = or_(
        VagaDB.publicada_em >= desde,
        and_(VagaDB.publicada_em.is_(None), VagaDB.criado_em >= desde),
    )
    return list(
        s.scalars(select(VagaDB).where(sem_match, recente, de_alguma_fonte).order_by(VagaDB.id))
    )
