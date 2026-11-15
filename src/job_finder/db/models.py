"""Tabelas do banco (SQLAlchemy 2.x)."""

from datetime import UTC, date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def agora() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


class Timestamps:
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=agora, onupdate=agora
    )


class PerfilDB(Timestamps, Base):
    __tablename__ = "perfis"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(64), unique=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    # Conteúdo de domain.DadosPerfil / Query / Filtros serializado em JSON.
    dados: Mapped[dict[str, Any]] = mapped_column(default=dict)
    queries: Mapped[list[Any]] = mapped_column(default=list)
    filtros: Mapped[dict[str, Any]] = mapped_column(default=dict)
    preferencias: Mapped[str] = mapped_column(Text, default="")
    curriculo_path: Mapped[str | None] = mapped_column(String(512))

    matches: Mapped[list["MatchDB"]] = relationship(back_populates="perfil")


class VagaDB(Timestamps, Base):
    __tablename__ = "vagas"

    id: Mapped[int] = mapped_column(primary_key=True)
    chave_dedup: Mapped[str] = mapped_column(String(512), unique=True)
    titulo: Mapped[str] = mapped_column(String(512))
    empresa: Mapped[str | None] = mapped_column(String(256), index=True)
    pais: Mapped[str | None] = mapped_column(String(2), index=True)
    cidade: Mapped[str | None] = mapped_column(String(128))
    modalidade: Mapped[str] = mapped_column(String(16))
    descricao: Mapped[str | None] = mapped_column(Text)
    salario_min: Mapped[float | None] = mapped_column(Float)
    salario_max: Mapped[float | None] = mapped_column(Float)
    salario_moeda: Mapped[str | None] = mapped_column(String(3))
    salario_periodo: Mapped[str | None] = mapped_column(String(8))
    publicada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    url_candidatura: Mapped[str | None] = mapped_column(String(2048))

    fontes: Mapped[list["VagaFonteDB"]] = relationship(back_populates="vaga")
    matches: Mapped[list["MatchDB"]] = relationship(back_populates="vaga")


class VagaFonteDB(Base):
    """Cada ocorrência de uma vaga numa fonte (a mesma vaga pode vir de várias)."""

    __tablename__ = "vaga_fontes"
    __table_args__ = (UniqueConstraint("fonte", "id_fonte"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vaga_id: Mapped[int] = mapped_column(ForeignKey("vagas.id", ondelete="CASCADE"), index=True)
    fonte: Mapped[str] = mapped_column(String(64))
    id_fonte: Mapped[str] = mapped_column(String(256))
    url: Mapped[str] = mapped_column(String(2048))
    dados_brutos: Mapped[dict[str, Any]] = mapped_column(default=dict)
    coletada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)

    vaga: Mapped[VagaDB] = relationship(back_populates="fontes")


class MatchDB(Timestamps, Base):
    """Resultado do fit de uma vaga para um perfil. Um par nunca é reanalisado."""

    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("vaga_id", "perfil_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vaga_id: Mapped[int] = mapped_column(ForeignKey("vagas.id", ondelete="CASCADE"))
    perfil_id: Mapped[int] = mapped_column(ForeignKey("perfis.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    score: Mapped[int | None] = mapped_column(Integer, index=True)
    score_prerank: Mapped[float | None] = mapped_column(Float)
    pontos_fortes: Mapped[list[Any]] = mapped_column(default=list)
    gaps: Mapped[list[Any]] = mapped_column(default=list)
    dealbreakers: Mapped[list[Any]] = mapped_column(default=list)
    resumo: Mapped[str | None] = mapped_column(Text)
    modelo: Mapped[str | None] = mapped_column(String(64))
    tentativas: Mapped[int] = mapped_column(Integer, default=0)
    notificado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    vaga: Mapped[VagaDB] = relationship(back_populates="matches")
    perfil: Mapped[PerfilDB] = relationship(back_populates="matches")
    feedbacks: Mapped[list["FeedbackDB"]] = relationship(back_populates="match")


class FeedbackDB(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    acao: Mapped[str] = mapped_column(String(16))  # salvar | descartar
    origem: Mapped[str] = mapped_column(String(16))  # telegram | web
    comentario: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)

    match: Mapped[MatchDB] = relationship(back_populates="feedbacks")


class ExecucaoDB(Base):
    """Log de cada rodada do pipeline."""

    __tablename__ = "execucoes"

    id: Mapped[int] = mapped_column(primary_key=True)
    perfil_id: Mapped[int | None] = mapped_column(ForeignKey("perfis.id", ondelete="SET NULL"))
    tipo: Mapped[str] = mapped_column(String(16))  # agendada | sob_demanda | dry_run
    status: Mapped[str] = mapped_column(String(16))  # rodando | ok | parcial | erro
    iniciada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    finalizada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    vagas_coletadas: Mapped[int] = mapped_column(Integer, default=0)
    vagas_novas: Mapped[int] = mapped_column(Integer, default=0)
    matches_novos: Mapped[int] = mapped_column(Integer, default=0)
    erros: Mapped[list[Any]] = mapped_column(default=list)


class UsoQuotaDB(Base):
    """Chamadas por conector por dia; o total mensal é a soma dos dias."""

    __tablename__ = "uso_quota"
    __table_args__ = (UniqueConstraint("conector", "dia"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conector: Mapped[str] = mapped_column(String(64))
    dia: Mapped[date] = mapped_column(Date)
    chamadas: Mapped[int] = mapped_column(Integer, default=0)


class CustoLLMDB(Base):
    """Tokens e custo de cada chamada ao Claude, para o teto mensal e o /status."""

    __tablename__ = "custo_llm"

    id: Mapped[int] = mapped_column(primary_key=True)
    execucao_id: Mapped[int | None] = mapped_column(
        ForeignKey("execucoes.id", ondelete="SET NULL"), index=True
    )
    perfil_id: Mapped[int | None] = mapped_column(ForeignKey("perfis.id", ondelete="SET NULL"))
    operacao: Mapped[str] = mapped_column(String(32))  # extrair_perfil | scoring | ...
    modelo: Mapped[str] = mapped_column(String(64))
    batch: Mapped[bool] = mapped_column(default=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    custo_usd: Mapped[float] = mapped_column(Float, default=0.0)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora, index=True)
