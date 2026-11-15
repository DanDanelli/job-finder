"""Currículo em PDF -> DadosPerfil + termos de busca sugeridos (Claude Opus 5.5)."""

import base64
from pathlib import Path

from pydantic import BaseModel, Field

from job_finder.domain import DadosPerfil
from job_finder.llm.client import ClienteLLM

OPERACAO = "extrair_perfil"

INSTRUCOES = """\
Você analisa currículos para um assistente pessoal de busca de vagas. Extraia do currículo \
anexado um perfil profissional fiel ao documento. Não invente nada que não esteja escrito \
ou que não se deduza diretamente dele.

Campos:
- resumo: 2 a 4 frases, em português, sobre quem é a pessoa profissionalmente, o que faz \
de melhor e em que tipo de função rende mais.
- cargos: títulos de cargo que a pessoa ocupou ou para os quais está pronta, na forma \
como aparecem em anúncios de vaga (em inglês quando o mercado usa o termo em inglês).
- senioridade: uma de junior, pleno, senior, lead, principal, gerente, diretor.
- anos_experiencia: total de anos de experiência profissional relevante, inteiro.
- skills: competências técnicas e de negócio, com peso de 1 a 5 pela evidência no \
currículo (5 = central e recente na carreira, 1 = mencionada de passagem). No máximo 30, \
das mais para as menos importantes. Use o nome canônico (ex: "AWS", "Terraform", "SEO").
- idiomas: código ISO 639-1 e nível (basico, intermediario, avancado, fluente, nativo).
- setores: setores de mercado em que a pessoa trabalhou (ex: energia, varejo, moda).
- certificacoes: certificações formais, com o nome oficial.
- termos_busca: 3 a 8 termos curtos para buscar vagas em sites de emprego, do mais para o \
menos aderente. Misture títulos exatos e variações comuns do mercado.
"""


class ExtracaoPerfil(BaseModel):
    dados: DadosPerfil
    termos_busca: list[str] = Field(default_factory=list)


def extrair_perfil(
    cliente: ClienteLLM,
    pdf: Path,
    *,
    modelo: str,
    perfil_id: int | None = None,
) -> ExtracaoPerfil:
    conteudo = base64.standard_b64encode(pdf.read_bytes()).decode("ascii")
    return cliente.estruturado(
        ExtracaoPerfil,
        operacao=OPERACAO,
        modelo=modelo,
        perfil_id=perfil_id,
        max_tokens=16_000,
        # Tarefa única e barata; esforço alto para a extração sair completa.
        output_config={"effort": "high"},
        system=INSTRUCOES,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": conteudo,
                        },
                    },
                    {"type": "text", "text": "Extraia o perfil deste currículo."},
                ],
            }
        ],
    )
