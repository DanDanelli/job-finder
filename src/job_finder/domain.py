"""Modelos de domínio compartilhados por conectores, pipeline, LLM e interfaces."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class Modalidade(StrEnum):
    REMOTO = "remoto"
    HIBRIDO = "hibrido"
    PRESENCIAL = "presencial"
    DESCONHECIDA = "desconhecida"


class StatusMatch(StrEnum):
    NOVA = "nova"
    SALVA = "salva"
    DESCARTADA = "descartada"
    PENDENTE = "pendente"


PeriodoSalario = Literal["ano", "mes", "hora"]
CodigoPais = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]  # ISO 3166-1 alfa-2


class Salario(BaseModel):
    minimo: float | None = None
    maximo: float | None = None
    moeda: str | None = None  # ISO 4217, ex: "EUR"
    periodo: PeriodoSalario | None = None


class VagaBruta(BaseModel):
    """Vaga como um conector a entrega, antes da normalização."""

    fonte: str
    id_fonte: str
    titulo: str
    empresa: str | None = None
    local: str | None = None
    descricao: str | None = None
    url: HttpUrl
    url_candidatura: HttpUrl | None = None
    publicada_em: datetime | None = None
    modalidade: str | None = None
    salario: Salario | None = None
    tags: list[str] = Field(default_factory=list)
    dados_brutos: dict[str, Any] = Field(default_factory=dict)


class Vaga(BaseModel):
    """Vaga normalizada e deduplicada."""

    titulo: str
    empresa: str | None = None
    pais: CodigoPais | None = None
    cidade: str | None = None
    modalidade: Modalidade = Modalidade.DESCONHECIDA
    descricao: str | None = None
    salario: Salario | None = None
    publicada_em: datetime | None = None
    url: HttpUrl
    url_candidatura: HttpUrl | None = None


class Skill(BaseModel):
    nome: str
    peso: int = Field(default=3, ge=1, le=5)


class Idioma(BaseModel):
    codigo: str  # ISO 639-1, ex: "pt"
    nivel: Literal["basico", "intermediario", "avancado", "fluente", "nativo"]


class Query(BaseModel):
    termo: str
    paises: list[CodigoPais] = Field(default_factory=list)  # vazio = global
    ativa: bool = True


class Filtros(BaseModel):
    """Filtros duros, aplicados em código antes de qualquer chamada ao LLM."""

    paises: list[CodigoPais] = Field(default_factory=list)
    cidades: list[str] = Field(default_factory=list)
    modalidades: list[Modalidade] = Field(default_factory=list)  # vazio = todas
    idiomas: list[str] = Field(default_factory=list)
    tipos_contrato: list[str] = Field(default_factory=list)
    salario_minimo: Salario | None = None
    empresas_bloqueadas: list[str] = Field(default_factory=list)
    palavras_bloqueadas: list[str] = Field(default_factory=list)
    idade_maxima_dias: int = Field(default=14, ge=1)


class DadosPerfil(BaseModel):
    """Perfil profissional extraído do currículo e revisado pelo usuário."""

    resumo: str = ""
    cargos: list[str] = Field(default_factory=list)
    senioridade: str | None = None
    anos_experiencia: int | None = Field(default=None, ge=0)
    skills: list[Skill] = Field(default_factory=list)
    idiomas: list[Idioma] = Field(default_factory=list)
    setores: list[str] = Field(default_factory=list)
    certificacoes: list[str] = Field(default_factory=list)


class Perfil(BaseModel):
    nome: str
    telegram_chat_id: int | None = None
    dados: DadosPerfil = Field(default_factory=DadosPerfil)
    queries: list[Query] = Field(default_factory=list)
    filtros: Filtros = Field(default_factory=Filtros)
    preferencias: str = ""


class ResultadoFit(BaseModel):
    """Saída estruturada do scoring de uma vaga para um perfil."""

    score: int = Field(ge=0, le=100)
    pontos_fortes: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    dealbreakers: list[str] = Field(default_factory=list)
    resumo: str
