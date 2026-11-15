"""Interpretação de salários em texto livre, ex: "$35,3k - $52k", "€4.000/mês"."""

import re

from job_finder.domain import PeriodoSalario, Salario
from job_finder.texto import sem_acento

HORAS_POR_ANO = 2080
MESES_POR_ANO = 12

_MOEDAS = [  # a ordem importa: "R$" e "US$" antes de "$"
    ("R$", "BRL"),
    ("US$", "USD"),
    ("$", "USD"),
    ("€", "EUR"),
    ("£", "GBP"),
]
_CODIGOS = re.compile(r"\b(USD|EUR|GBP|BRL|CHF|CAD|AUD)\b", re.IGNORECASE)

_PERIODOS: list[tuple[re.Pattern[str], PeriodoSalario]] = [
    (re.compile(r"/\s*(hour|hr|h)\b|per hour|hourly|por hora|/hora"), "hora"),
    (re.compile(r"/\s*(month|mo|mes)\b|per month|monthly|por mes|mensal"), "mes"),
    (re.compile(r"/\s*(year|yr|ano)\b|per year|annual|yearly|a year|por ano|anual"), "ano"),
]
# Unidades que não dá para comparar com salário.
_NAO_SALARIO = re.compile(r"per (task|project|word|article)|por (tarefa|projeto)")

_NUMERO = re.compile(r"(\d+(?:[.,]\d+)*)\s*(k\b|mil\b)?", re.IGNORECASE)


def _valor(numero: str, sufixo: str | None) -> float:
    if sufixo:  # "35,3k" / "3.5k": vírgula ou ponto é decimal
        return float(numero.replace(",", ".")) * 1000
    partes = re.split(r"[.,]", numero)
    if len(partes) > 1 and all(len(p) == 3 for p in partes[1:]):
        return float("".join(partes))  # "100,000" / "4.000": separador de milhar
    return float(numero.replace(",", "."))


def interpretar_salario(texto: str | None) -> Salario | None:
    if not texto:
        return None
    limpo = sem_acento(texto).lower()
    if _NAO_SALARIO.search(limpo):
        return None

    moeda = next((codigo for simbolo, codigo in _MOEDAS if simbolo in texto), None)
    if achado := _CODIGOS.search(texto):
        moeda = achado.group(1).upper()

    achados = _NUMERO.findall(limpo)
    valores = [_valor(n, s) for n, s in achados]
    # "60-70k": o "k" de um lado da faixa vale para o outro.
    if any(s for _, s in achados):
        valores = [
            v * 1000 if not s and v < 1000 else v
            for v, (_, s) in zip(valores, achados, strict=True)
        ]
    valores = [v for v in valores if v > 0]
    if not valores:
        return None

    periodo = next((p for regex, p in _PERIODOS if regex.search(limpo)), None)
    if periodo is None:
        if moeda == "BRL":
            # No Brasil o salário é anunciado por mês; "R$ 150" sozinho é valor por hora.
            periodo = "mes" if max(valores) >= 1_000 else "hora"
        elif max(valores) >= 10_000:
            periodo = "ano"

    return Salario(minimo=min(valores), maximo=max(valores), moeda=moeda, periodo=periodo)


def anual(valor: float, periodo: PeriodoSalario) -> float:
    if periodo == "hora":
        return valor * HORAS_POR_ANO
    if periodo == "mes":
        return valor * MESES_POR_ANO
    return valor
