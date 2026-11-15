"""Carregamento de perfis a partir de YAML (provisório até o upload de currículo no M2)."""

from pathlib import Path

import yaml

from job_finder.domain import Perfil


def ler_perfil_yaml(caminho: Path) -> Perfil:
    dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    dados.setdefault("nome", caminho.stem)
    return Perfil.model_validate(dados)
