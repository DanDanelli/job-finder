"""User-facing messages (Telegram and the text Claude writes) in each supported language.

The language comes from `Settings.language` (JOBFINDER_LANGUAGE). Messages are Telegram
HTML: parameters that come from outside (job titles, errors) must be escaped by the
caller.
"""

from typing import Any

DEFAULT_LANGUAGE = "en"

# Name of the language as Claude is asked to write it.
LANGUAGE_NAMES = {"en": "English", "pt-BR": "Brazilian Portuguese"}

# Bot commands: the English name always works; each language adds its own alias.
COMMANDS: dict[str, dict[str, str]] = {
    "search": {"pt-BR": "buscar"},
    "saved": {"pt-BR": "salvas"},
    "pause": {"pt-BR": "pausar"},
    "status": {},
    "evaluate": {"pt-BR": "avaliar"},
}

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        # digest
        "digest.none": "No new jobs since the last digest.",
        "digest.header": "<b>{new} new jobs</b> · {above} with fit ≥ {min_score}",
        "digest.more": "Showing the best {shown}; the rest comes in the next digest.",
        "digest.warnings": "<b>Warnings</b>",
        "location.unknown": "location not stated",
        "work_mode.remote": "Remote",
        "work_mode.hybrid": "Hybrid",
        "work_mode.onsite": "On-site",
        "period.year": "year",
        "period.month": "month",
        "period.hour": "hour",
        "button.save": "👍 Save",
        "button.saved": "✅ Saved",
        "button.dismiss": "👎 Dismiss",
        "button.dismissed": "🗑 Dismissed",
        "button.open": "🔗 Open",
        # commands
        "start.unknown": (
            "Hi! This chat is not linked to a profile yet.\n"
            "chat_id: <code>{chat_id}</code>\n"
            "Put this number in <code>telegram_chat_id</code> in the profile's YAML."
        ),
        "start.known": "Hi, {name}! Digest at {times}.\n{commands}\n\n{how}",
        "search.busy": "A run is already in progress; the digest arrives when it ends.",
        "search.started": "Searching for jobs... the digest arrives in a few minutes.",
        "search.no_profile": "I couldn't find your profile.",
        "pause.on": "⏸ Digest paused. Send /{command} again to resume.",
        "pause.off": "▶️ Digest resumed.",
        "feedback.invalid": "Invalid action.",
        "feedback.saved": "Saved ✅",
        "feedback.dismissed": "Dismissed",
        "saved.none": "No saved jobs yet. Use 👍 on the digest's jobs.",
        "saved.title": "<b>Saved jobs</b> ({n})",
        "status.paused": " · ⏸ paused",
        "status.runs": "Runs: {times} ({timezone})",
        "status.last_run": "Last run: {when}, {status}, {new} new jobs",
        "status.jobs": (
            "Jobs: {new} new, {saved} saved, {dismissed} dismissed, {pending} pending,"
            " {skipped} off-profile"
        ),
        "status.claude": "Claude this month: US$ {spent} of US$ {cap}",
        "status.quota": "Quota today: {quotas}",
        # run warnings
        "warn.unscored": "{n} jobs without a score (retried in the next run)",
        "warn.no_key": "no ANTHROPIC_API_KEY: jobs stay unscored",
        "warn.claude_down": "Claude unavailable: {error}",
        # ad-hoc evaluation
        "evaluate.how": (
            "Send me the link of a job you saw (LinkedIn, Indeed, Gupy, a company's "
            "site...) or paste its full text, and I'll tell you its fit with your profile."
        ),
        "evaluate.working": "🔎 Evaluating the job...",
        "evaluate.no_key": "ANTHROPIC_API_KEY is not set: I can't evaluate jobs.",
        "evaluate.cost_cap": "This month's Claude spend cap was reached; I can't evaluate.",
        "evaluate.retry": "I couldn't evaluate it now. Try again in a few minutes.",
        "evaluate.error": "Something went wrong evaluating this job. Try again later.",
        "evaluate.already": "🔁 This job had already been evaluated.",
        "evaluate.closed": "⚠️ The page says applications are closed.",
        "evaluate.would_discard": "🔎 Your filters would discard this job: {reason}.",
        "input.empty": "Send the job's link or paste its full text.",
        "input.scheme": "The link must start with http:// or https://.",
        "input.host": "I couldn't find the site {host}.",
        "input.not_public": "This link does not point to a public site.",
        "input.http_status": (
            "The site answered {status} (it probably asks for a login or blocks bots). "
            "Copy the job's text and paste it here."
        ),
        "input.not_page": "The link is not a web page. Paste the job's text here.",
        "input.redirects": "The link redirects too many times. Paste the job's text here.",
        "input.unreachable": "I couldn't open the link. Copy the job's text and paste it here.",
        "input.not_a_job": (
            "I couldn't find a job on this page (it may ask for a login or block bots). "
            "Copy the job's text and paste it here."
        ),
        # filter discard reasons
        "reason.too_old": "posted longer ago than your limit",
        "reason.work_mode": "work mode outside the ones you accept",
        "reason.location": "does not accept remote work from where you are",
        "reason.country": "country outside the ones you accept",
        "reason.city": "city outside the ones you accept",
        "reason.contract_type": "contract type outside the ones you accept",
        "reason.salary": "salary below your minimum",
        "reason.blocked_company": "company on your block list",
        "reason.blocked_keyword": "title has a blocked keyword",
    },
    "pt-BR": {
        "digest.none": "Nenhuma vaga nova desde o último digest.",
        "digest.header": "<b>{new} vagas novas</b> · {above} com fit ≥ {min_score}",
        "digest.more": "Mostrando as {shown} melhores; o resto vem no próximo digest.",
        "digest.warnings": "<b>Avisos</b>",
        "location.unknown": "local não informado",
        "work_mode.remote": "Remoto",
        "work_mode.hybrid": "Híbrido",
        "work_mode.onsite": "Presencial",
        "period.year": "ano",
        "period.month": "mês",
        "period.hour": "hora",
        "button.save": "👍 Salvar",
        "button.saved": "✅ Salva",
        "button.dismiss": "👎 Descartar",
        "button.dismissed": "🗑 Descartada",
        "button.open": "🔗 Abrir",
        "start.unknown": (
            "Olá! Este chat ainda não está ligado a um perfil.\n"
            "chat_id: <code>{chat_id}</code>\n"
            "Coloque esse número em <code>telegram_chat_id</code> no YAML do perfil."
        ),
        "start.known": "Olá, {name}! Digest às {times}.\n{commands}\n\n{how}",
        "search.busy": "Já tem uma rodada em andamento; o digest chega ao fim dela.",
        "search.started": "Buscando vagas... o digest chega em alguns minutos.",
        "search.no_profile": "Não encontrei o seu perfil.",
        "pause.on": "⏸ Digest pausado. Mande /{command} de novo para retomar.",
        "pause.off": "▶️ Digest retomado.",
        "feedback.invalid": "Ação inválida.",
        "feedback.saved": "Salva ✅",
        "feedback.dismissed": "Descartada",
        "saved.none": "Nenhuma vaga salva ainda. Use 👍 nas vagas do digest.",
        "saved.title": "<b>Vagas salvas</b> ({n})",
        "status.paused": " · ⏸ pausado",
        "status.runs": "Rodadas: {times} ({timezone})",
        "status.last_run": "Última rodada: {when}, {status}, {new} vagas novas",
        "status.jobs": (
            "Vagas: {new} novas, {saved} salvas, {dismissed} descartadas, {pending} pendentes,"
            " {skipped} fora do perfil"
        ),
        "status.claude": "Claude no mês: US$ {spent} de US$ {cap}",
        "status.quota": "Quota hoje: {quotas}",
        "warn.unscored": "{n} vagas sem nota (nova tentativa na próxima rodada)",
        "warn.no_key": "sem ANTHROPIC_API_KEY: vagas ficam sem nota",
        "warn.claude_down": "Claude indisponível: {error}",
        "evaluate.how": (
            "Mande o link de uma vaga que você viu (LinkedIn, Indeed, Catho, Gupy, site da "
            "empresa...) ou cole o texto completo dela, e eu digo o fit com o seu perfil."
        ),
        "evaluate.working": "🔎 Avaliando a vaga...",
        "evaluate.no_key": "Sem ANTHROPIC_API_KEY configurada: não consigo avaliar vagas.",
        "evaluate.cost_cap": (
            "O teto de gasto com o Claude deste mês foi atingido; não consigo avaliar."
        ),
        "evaluate.retry": "Não consegui avaliar agora. Tente de novo em alguns minutos.",
        "evaluate.error": "Deu erro ao avaliar essa vaga. Tente de novo mais tarde.",
        "evaluate.already": "🔁 Essa vaga já tinha sido avaliada.",
        "evaluate.closed": "⚠️ A página diz que as candidaturas estão encerradas.",
        "evaluate.would_discard": "🔎 Seus filtros descartariam essa vaga: {reason}.",
        "input.empty": "Mande o link da vaga ou cole o texto completo dela.",
        "input.scheme": "O link precisa começar com http:// ou https://.",
        "input.host": "Não encontrei o site {host}.",
        "input.not_public": "Esse link não aponta para um site público.",
        "input.http_status": (
            "O site respondeu {status} (deve pedir login ou bloquear robôs). "
            "Copie o texto da vaga e cole aqui."
        ),
        "input.not_page": "O link não é uma página. Cole o texto da vaga aqui.",
        "input.redirects": "O link redireciona demais. Cole o texto da vaga aqui.",
        "input.unreachable": "Não consegui abrir o link. Copie o texto da vaga e cole aqui.",
        "input.not_a_job": (
            "Não achei uma vaga nessa página (pode pedir login ou bloquear robôs). "
            "Copie o texto da vaga e cole aqui."
        ),
        "reason.too_old": "publicada há mais tempo que o seu limite",
        "reason.work_mode": "modalidade fora das que você aceita",
        "reason.location": "não aceita trabalho remoto de onde você está",
        "reason.country": "país fora dos que você aceita",
        "reason.city": "cidade fora das que você aceita",
        "reason.contract_type": "tipo de contrato fora dos que você aceita",
        "reason.salary": "salário abaixo do seu mínimo",
        "reason.blocked_company": "empresa na sua lista de bloqueio",
        "reason.blocked_keyword": "título com palavra bloqueada",
    },
}


def t(language: str, key: str, **params: Any) -> str:
    """The message `key` in `language`, falling back to English."""
    template = MESSAGES.get(language, {}).get(key) or MESSAGES[DEFAULT_LANGUAGE][key]
    return template.format(**params)


def language_name(language: str) -> str:
    return LANGUAGE_NAMES.get(language, LANGUAGE_NAMES[DEFAULT_LANGUAGE])


def command(name: str, language: str) -> str:
    """The command name users see in `language` (e.g. "search" -> "buscar")."""
    return COMMANDS[name].get(language, name)


def command_aliases(name: str) -> list[str]:
    """Every name the command answers to: the English one plus each language's alias."""
    return [name, *sorted(set(COMMANDS[name].values()) - {name})]
