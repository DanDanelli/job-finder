"""VagaBruta (formato de cada fonte) → Vaga (formato único)."""

from datetime import UTC

from job_finder.domain import Modalidade, TipoContrato, Vaga, VagaBruta
from job_finder.pipeline.locais import WORLDWIDE, interpretar_local
from job_finder.pipeline.salario import interpretar_salario
from job_finder.texto import chave

_MODALIDADES = {
    Modalidade.REMOTO: {"remote", "remoto", "anywhere", "work from home", "home office", "wfh"},
    Modalidade.HIBRIDO: {"hybrid", "hibrido", "flexible"},
    Modalidade.PRESENCIAL: {"onsite", "on site", "presencial", "in office", "office"},
}

_CONTRATOS = {
    TipoContrato.TEMPO_INTEGRAL: {
        "full time", "fulltime", "permanent", "tempo integral", "efetivo", "clt", "cdi",
    },
    TipoContrato.MEIO_PERIODO: {"part time", "parttime", "meio periodo"},
    TipoContrato.CONTRATO: {
        "contract", "contractor", "pj", "contrato", "prestador de servicos",
    },
    TipoContrato.FREELANCE: {"freelance", "freelancer"},
    TipoContrato.ESTAGIO: {"internship", "intern", "estagio"},
    TipoContrato.TEMPORARIO: {"temporary", "temp", "temporario"},
}  # fmt: skip


def _modalidade(texto: str | None) -> Modalidade:
    if not texto:
        return Modalidade.DESCONHECIDA
    k = chave(texto)
    for modalidade, termos in _MODALIDADES.items():
        if k in termos:
            return modalidade
    return Modalidade.DESCONHECIDA


def _tipo_contrato(texto: str | None) -> TipoContrato | None:
    if not texto:
        return None
    k = chave(texto)
    for tipo, termos in _CONTRATOS.items():
        if k in termos:
            return tipo
    return None


def normalizar(bruta: VagaBruta) -> Vaga:
    modalidade = _modalidade(bruta.modalidade)
    locais = interpretar_local(bruta.local)
    paises = [c for c in locais if len(c) == 2]

    publicada = bruta.publicada_em
    if publicada is not None and publicada.tzinfo is None:
        publicada = publicada.replace(tzinfo=UTC)

    return Vaga(
        titulo=" ".join(bruta.titulo.split()),
        empresa=" ".join(bruta.empresa.split()) if bruta.empresa else None,
        # País da vaga só quando o local aponta para um único país.
        pais=paises[0] if len(paises) == 1 and len(locais) == 1 else None,
        local_texto=bruta.local,
        locais_elegiveis=[WORLDWIDE] if WORLDWIDE in locais else locais,
        modalidade=modalidade,
        tipo_contrato=_tipo_contrato(bruta.tipo_contrato),
        descricao=bruta.descricao,
        salario=bruta.salario or interpretar_salario(bruta.salario_texto),
        publicada_em=publicada,
        url=bruta.url,
        url_candidatura=bruta.url_candidatura,
    )
