"""Perfis em YAML: o usuário revisa e edita o que foi extraído do currículo."""

from pathlib import Path

import yaml

from job_finder.domain import Perfil


def ler_perfil_yaml(caminho: Path) -> Perfil:
    dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    dados.setdefault("nome", caminho.stem)
    return Perfil.model_validate(dados)


def escrever_perfil_yaml(perfil: Perfil) -> str:
    """YAML no mesmo formato que `ler_perfil_yaml` lê, para revisar e carregar de volta."""
    dados = perfil.model_dump(mode="json", exclude_none=True)
    return yaml.safe_dump(dados, allow_unicode=True, sort_keys=False, width=100)
