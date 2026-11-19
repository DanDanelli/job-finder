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
            "Hi! This chat is not linked to a profile yet. Sign in to the site, open "
            "<b>Account</b> and choose <b>Link Telegram</b>.\n\n"
            "If you set up profiles in YAML instead, this chat's id is "
            "<code>{chat_id}</code> (<code>telegram_chat_id</code>)."
        ),
        "start.linked": "✅ This chat is now linked to your profile.",
        "start.bad_code": (
            "This link code is wrong or has expired (they last 15 minutes and work once). "
            "Make a new one on the site, under <b>Account</b>."
        ),
        "start.taken": (
            "This chat is already linked to another profile. Unlink it on the site first "
            "(<b>Account</b>, signed in as that profile)."
        ),
        "start.known": "Hi, {name}! Digest at {times}.\n{commands}\n\n{how}",
        "search.busy": "A run is already in progress; the digest arrives when it ends.",
        "search.started": "Searching for jobs... the digest arrives in a few minutes.",
        "search.no_profile": "I couldn't find your profile.",
        "pause.on": "⏸ Digest paused. Send /{command} again to resume.",
        "pause.off": "▶️ Digest resumed.",
        "feedback.invalid": "Invalid action.",
        "feedback.saved": "Saved ✅ Reply to the job's message to say why.",
        "feedback.dismissed": "Dismissed. Reply to the job's message to say why.",
        "feedback.noted": "Note saved ✍️ Claude takes it into account.",
        "feedback.note_cleared": "Note removed.",
        "feedback.reply_hint": (
            "To add a note, reply to a job's message (the one with 👍/👎). To evaluate a "
            "job, send its link as a new message."
        ),
        "saved.none": "No saved jobs yet. Use 👍 on the digest's jobs.",
        "saved.title": "<b>Saved jobs</b> ({n})",
        "status.paused": " · ⏸ paused",
        "status.runs": "Runs: {times} ({timezone})",
        "status.last_run": "Last run: {when}, {status}, {new} new jobs",
        "status.jobs": (
            "Jobs: {new} new, {saved} saved, {applied} applied, {dismissed} dismissed,"
            " {pending} pending,"
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
        # site
        "web.login.title": "Sign in",
        "web.login.username": "Username",
        "web.login.password": "Password",
        "web.login.submit": "Sign in",
        "web.login.failed": "Wrong username or password.",
        "web.login.blocked": "Too many attempts. Try again in 15 minutes.",
        "web.logout": "Sign out",
        "web.nav.jobs": "Jobs",
        "web.nav.profile": "Profile",
        "web.tab.new": "New",
        "web.tab.saved": "Saved",
        "web.tab.applied": "Applied",
        "web.tab.dismissed": "Dismissed",
        "web.jobs.pending": "{n} more waiting to be scored in the next run.",
        "web.jobs.empty": "No jobs here.",
        "web.jobs.dismissed_window": (
            "Dismissed in the last {days} days. Older ones leave this list but keep teaching"
            " Claude."
        ),
        "web.jobs.min_score": "Minimum fit",
        "web.jobs.work_mode": "Work mode",
        "web.jobs.any": "Any",
        "web.jobs.filter": "Apply",
        "web.jobs.posted": "posted {date}",
        "web.action.save": "Save",
        "web.action.apply": "I applied",
        "web.action.unapply": "Back to saved",
        "web.action.dismiss": "Dismiss",
        "web.action.restore": "Back to new",
        "web.action.open": "Open",
        "web.note.label": "Note",
        "web.note.placeholder": "Why? (optional; Claude learns from it)",
        "web.note.save": "Save note",
        "web.profile.title": "Profile",
        "web.profile.searches": "Searches",
        "web.profile.queries.hint": (
            'One per line: "term" or "term | BR, PT" to pick countries. Start a '
            "line with # to pause it."
        ),
        "web.profile.filters": "Filters",
        "web.profile.filters.hint": "Jobs that fail a filter never reach Claude.",
        "web.profile.work_modes": "Work modes",
        "web.profile.none_is_all": "None ticked means all.",
        "web.profile.countries": "Countries for on-site and hybrid jobs",
        "web.profile.countries.hint": "Codes or names, separated by commas. Empty means any.",
        "web.profile.cities": "Cities for on-site and hybrid jobs",
        "web.profile.lines.hint": "One per line. Empty means any.",
        "web.profile.remote_from": "Countries you can work remotely from",
        "web.profile.remote_from.hint": (
            "A remote job passes when it accepts any of them. Empty means no filter."
        ),
        "web.profile.contract_types": "Contract types",
        "web.profile.salary": "Minimum salary",
        "web.profile.salary.hint": "Jobs with no salary pass, and so do those up to 10% below.",
        "web.profile.min_any": "Any contract",
        "web.profile.min_full_time": "Full-time (CLT)",
        "web.profile.min_contract": "Contractor (PJ)",
        "web.profile.blocked_companies": "Blocked companies",
        "web.profile.blocked_keywords": "Blocked words in the title",
        "web.profile.block.hint": "One per line.",
        "web.profile.max_age_days": "Ignore jobs older than (days)",
        "web.profile.preferences": "Preferences for Claude",
        "web.profile.preferences.hint": (
            "Free text Claude reads when scoring: what you are looking for and what to avoid."
        ),
        "web.profile.paused": "Paused: no searches and no digest",
        "web.profile.save": "Save",
        "web.profile.saved": "Saved.",
        "web.profile.saved_dropped": "Saved. {n} jobs no longer pass the filters and were removed.",
        "web.profile.errors": "Some fields need fixing.",
        "web.profile.not_recognized": "Not recognized: {value}",
        "contract.full_time": "Full-time",
        "contract.part_time": "Part-time",
        "contract.contract": "Contractor",
        "contract.freelance": "Freelance",
        "contract.internship": "Internship",
        "contract.temporary": "Temporary",
        "web.nav.cv": "CV",
        "web.nav.status": "Status",
        "web.nav.account": "Account",
        "web.jobs.sort": "Order",
        "web.sort.score_desc": "Best fit first",
        "web.sort.score_asc": "Lowest fit first",
        "web.sort.date_desc": "Newest first",
        "web.sort.date_asc": "Oldest first",
        "web.search.now": "Search now",
        "web.search.running": "Search in progress",
        "progress.start": "Starting...",
        "progress.collect": "Collecting from {source} ({i} of {n})",
        "progress.store": "Saving {n} jobs and removing duplicates",
        "progress.filter": "Applying {profile}'s filters",
        "progress.score": "Claude is scoring {profile}'s jobs ({i} of {n})",
        "progress.batch": "Waiting for Claude (batch of {n} jobs)",
        "web.status.search": "Search",
        "web.status.running_since": "Started at {when}",
        "web.status.kind.scheduled": "scheduled",
        "web.status.kind.on_demand": "on demand",
        "web.status.failed": "The search at {when} failed: {error}",
        "web.status.done": (
            "Last search ({when}): {approved} jobs passed your filters, {scored} scored by Claude."
        ),
        "web.status.see_jobs": "See the jobs",
        "web.status.search_hint": (
            "Searches only your profile's terms; it takes a few minutes and the Telegram "
            "digest arrives at the end."
        ),
        "web.status.paused": "Your profile is paused: scheduled searches skip it.",
        "web.status.next": "Next scheduled search: {when}",
        "web.status.waiting": "{n} jobs waiting to be scored",
        "web.status.claude": "Claude this month",
        "web.status.spent": "US$ {spent} of US$ {cap} (yours: US$ {mine})",
        "web.status.operation": "Use",
        "web.status.calls": "Calls",
        "web.status.yours": "Yours",
        "web.status.op.scoring": "Scoring jobs",
        "web.status.op.extract_profile": "Reading CVs",
        "web.status.op.extract_job": "Jobs sent to /evaluate",
        "web.status.op.learn_rules": "Rules from your feedback",
        "web.status.op.eval": "Model evals",
        "web.status.sources": "Sources",
        "web.status.source": "Source",
        "web.status.today": "Today",
        "web.status.month": "Month",
        "web.status.sources_hint": (
            "Requests used / allowed. The limits follow each source's terms and free plan."
        ),
        "web.status.runs": "Recent runs",
        "web.status.when": "When",
        "web.status.result": "Result",
        "web.status.collected": "Collected",
        "web.status.new": "New",
        "web.status.passed": "Passed",
        "web.status.run.ok": "ok",
        "web.status.run.partial": "partial",
        "web.status.run.error": "failed",
        "web.status.run.running": "running",
        "web.status.errors": "{n} warnings",
        "web.status.no_runs": "No runs yet.",
        "web.account.change": "Change password",
        "web.account.current": "Current password",
        "web.account.new": "New password",
        "web.account.repeat": "Repeat the new password",
        "web.account.hint": "At least 8 characters. Other devices are signed out.",
        "web.account.submit": "Change password",
        "web.account.changed": "Password changed. Other devices were signed out.",
        "web.account.wrong_current": "The current password is wrong.",
        "web.account.mismatch": "The two new passwords differ.",
        "web.account.too_short": "The new password needs at least {n} characters.",
        "web.telegram.title": "Telegram",
        "web.telegram.linked": "Linked: the digest and the bot's replies go to your chat.",
        "web.telegram.not_linked": "Not linked yet, so no digest reaches you.",
        "web.telegram.link": "Link Telegram",
        "web.telegram.relink": "Link another chat",
        "web.telegram.unlink": "Unlink",
        "web.telegram.open": "Open in Telegram",
        "web.telegram.manual": "Or send this message to the bot:",
        "web.telegram.expires": "The code works once, until {time}.",
        "web.telegram.done": "Sent it? Reload this page.",
        "web.telegram.no_bot": (
            "The Telegram bot is not running (no JOBFINDER_TELEGRAM_BOT_TOKEN), so there is "
            "nothing to link yet."
        ),
        "web.telegram.unlinked": "Telegram unlinked. No more digests until you link it again.",
        "web.learned.title": "Learned from your feedback",
        "web.learned.intro": (
            "Rules Claude found in the jobs you saved and dismissed. Once you approve "
            "them, they count when each new job is scored. Edit them freely."
        ),
        "web.learned.counts": "{saved} saved and {dismissed} dismissed jobs so far",
        "web.learned.checked": "Claude last looked {when}",
        "web.learned.thinking": "Claude is reading your decisions. This page updates itself.",
        "web.learned.failed": "Could not suggest rules: {error}",
        "web.learned.draft": "Suggested rules, waiting for you",
        "web.learned.draft_hint": (
            "Edit, delete or add lines, then approve. Until then, none of these count."
        ),
        "web.learned.current": "Rules in use",
        "web.learned.hint": "One rule per line, at most {n}. An empty box removes them all.",
        "web.learned.approve": "Approve these rules",
        "web.learned.save": "Save",
        "web.learned.discard": "Discard the suggestion",
        "web.learned.suggest_title": "Ask Claude",
        "web.learned.suggest_hint": (
            "Claude also does this weekly on its own when you have new decisions."
        ),
        "web.learned.suggest": "Suggest rules now",
        "web.learned.need_more": "Save or dismiss at least {n} jobs first.",
        "web.learned.saved": "Saved. The next scorings use these rules.",
        "web.learned.discarded": "Suggestion discarded.",
        "web.learned.no_change": "Claude found nothing new.",
        "web.learned.summary": "{n} rules in use",
        "web.learned.waiting": "new suggestion waiting",
        "learn.proposed": (
            "💡 Claude suggested {n} rules from the jobs you saved and dismissed. They only "
            "count after you approve them on the site: Profile → Learned from your feedback."
        ),
        "web.cv.saved": "Saved.",
        "web.cv.saved_terms": "Saved. {n} new searches added to your profile.",
        "web.cv.no_file": "Choose a PDF file.",
        "web.cv.too_big": "The file is larger than {mb} MB.",
        "web.cv.not_pdf": "That file is not a PDF.",
        "web.cv.upload": "Send a new CV",
        "web.cv.extracting": (
            "Claude is reading your CV. This takes about a minute; the page updates by itself."
        ),
        "web.cv.failed": "Could not read the CV: {error}",
        "web.cv.has_resume": "Your CV is on file.",
        "web.cv.no_resume": "No CV sent through the site yet.",
        "web.cv.upload_hint": (
            "PDF up to {mb} MB. Claude reads it and you review the result before anything changes."
        ),
        "web.cv.send": "Send and read",
        "web.cv.review": (
            "Review what Claude read from your CV, fix anything, then save. Nothing changes "
            "until you save."
        ),
        "web.cv.data": "Professional profile",
        "web.cv.data_hint": "Claude scores each job against this.",
        "web.cv.summary": "Summary",
        "web.cv.roles": "Roles",
        "web.cv.seniority": "Seniority",
        "web.cv.years": "Years of experience",
        "web.cv.skills": "Skills",
        "web.cv.skills_hint": 'One per line: "skill | weight", weight 1 (minor) to 5 (central).',
        "web.cv.languages": "Languages",
        "web.cv.languages_hint": 'One per line: "code | level", e.g. "en | fluent". Levels:',
        "web.cv.industries": "Industries",
        "web.cv.certifications": "Certifications",
        "web.cv.terms": "Suggested searches",
        "web.cv.terms_hint": (
            "Ticked terms are added to your searches. You can edit them later in Profile."
        ),
        "web.cv.save_draft": "Save to my profile",
        "web.cv.discard": "Discard this CV",
        "seniority.junior": "Junior",
        "seniority.mid": "Mid-level",
        "seniority.senior": "Senior",
        "seniority.lead": "Lead",
        "seniority.principal": "Principal",
        "seniority.manager": "Manager",
        "seniority.director": "Director",
        "level.basic": "basic",
        "level.intermediate": "intermediate",
        "level.advanced": "advanced",
        "level.fluent": "fluent",
        "level.native": "native",
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
            "Olá! Este chat ainda não está ligado a um perfil. Entre no site, abra "
            "<b>Conta</b> e escolha <b>Ligar Telegram</b>.\n\n"
            "Se você configura os perfis por YAML, o id deste chat é "
            "<code>{chat_id}</code> (<code>telegram_chat_id</code>)."
        ),
        "start.linked": "✅ Este chat agora está ligado ao seu perfil.",
        "start.bad_code": (
            "Este código de ligação está errado ou expirou (vale por 15 minutos e uma vez "
            "só). Gere outro no site, em <b>Conta</b>."
        ),
        "start.taken": (
            "Este chat já está ligado a outro perfil. Desligue-o no site antes "
            "(<b>Conta</b>, entrando com aquele perfil)."
        ),
        "start.known": "Olá, {name}! Digest às {times}.\n{commands}\n\n{how}",
        "search.busy": "Já tem uma rodada em andamento; o digest chega ao fim dela.",
        "search.started": "Buscando vagas... o digest chega em alguns minutos.",
        "search.no_profile": "Não encontrei o seu perfil.",
        "pause.on": "⏸ Digest pausado. Mande /{command} de novo para retomar.",
        "pause.off": "▶️ Digest retomado.",
        "feedback.invalid": "Ação inválida.",
        "feedback.saved": "Salva ✅ Responda à mensagem da vaga para dizer por quê.",
        "feedback.dismissed": "Descartada. Responda à mensagem da vaga para dizer por quê.",
        "feedback.noted": "Observação guardada ✍️ O Claude vai levar em conta.",
        "feedback.note_cleared": "Observação removida.",
        "feedback.reply_hint": (
            "Para deixar uma observação, responda à mensagem de uma vaga (a que tem 👍/👎). "
            "Para avaliar uma vaga, mande o link numa mensagem nova."
        ),
        "saved.none": "Nenhuma vaga salva ainda. Use 👍 nas vagas do digest.",
        "saved.title": "<b>Vagas salvas</b> ({n})",
        "status.paused": " · ⏸ pausado",
        "status.runs": "Rodadas: {times} ({timezone})",
        "status.last_run": "Última rodada: {when}, {status}, {new} vagas novas",
        "status.jobs": (
            "Vagas: {new} novas, {saved} salvas, {applied} candidatadas,"
            " {dismissed} descartadas, {pending} pendentes,"
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
        # site
        "web.login.title": "Entrar",
        "web.login.username": "Usuário",
        "web.login.password": "Senha",
        "web.login.submit": "Entrar",
        "web.login.failed": "Usuário ou senha incorretos.",
        "web.login.blocked": "Tentativas demais. Tente de novo em 15 minutos.",
        "web.logout": "Sair",
        "web.nav.jobs": "Vagas",
        "web.nav.profile": "Perfil",
        "web.tab.new": "Novas",
        "web.tab.saved": "Salvas",
        "web.tab.applied": "Candidatadas",
        "web.tab.dismissed": "Descartadas",
        "web.jobs.pending": "Mais {n} aguardando avaliação na próxima rodada.",
        "web.jobs.empty": "Nenhuma vaga aqui.",
        "web.jobs.dismissed_window": (
            "Descartadas nos últimos {days} dias. As mais antigas saem desta lista, mas"
            " continuam ensinando o Claude."
        ),
        "web.jobs.min_score": "Fit mínimo",
        "web.jobs.work_mode": "Modalidade",
        "web.jobs.any": "Qualquer",
        "web.jobs.filter": "Aplicar",
        "web.jobs.posted": "publicada em {date}",
        "web.action.save": "Salvar",
        "web.action.apply": "Me candidatei",
        "web.action.unapply": "Voltar para salvas",
        "web.action.dismiss": "Descartar",
        "web.action.restore": "Voltar para novas",
        "web.action.open": "Abrir",
        "web.note.label": "Observação",
        "web.note.placeholder": "Por quê? (opcional; o Claude aprende com isso)",
        "web.note.save": "Salvar observação",
        "web.profile.title": "Perfil",
        "web.profile.searches": "Buscas",
        "web.profile.queries.hint": (
            'Uma por linha: "termo" ou "termo | BR, PT" para escolher países. '
            "Comece a linha com # para pausá-la."
        ),
        "web.profile.filters": "Filtros",
        "web.profile.filters.hint": "Vagas que não passam num filtro nunca chegam ao Claude.",
        "web.profile.work_modes": "Modalidades",
        "web.profile.none_is_all": "Nenhuma marcada significa todas.",
        "web.profile.countries": "Países para vagas presenciais e híbridas",
        "web.profile.countries.hint": (
            "Códigos ou nomes, separados por vírgula. Vazio significa qualquer um."
        ),
        "web.profile.cities": "Cidades para vagas presenciais e híbridas",
        "web.profile.lines.hint": "Uma por linha. Vazio significa qualquer uma.",
        "web.profile.remote_from": "Países de onde você pode trabalhar remoto",
        "web.profile.remote_from.hint": (
            "Uma vaga remota passa quando aceita algum deles. Vazio significa sem filtro."
        ),
        "web.profile.contract_types": "Tipos de contrato",
        "web.profile.salary": "Salário mínimo",
        "web.profile.salary.hint": "Vagas sem salário passam, e também as até 10% abaixo.",
        "web.profile.min_any": "Qualquer contrato",
        "web.profile.min_full_time": "CLT",
        "web.profile.min_contract": "PJ",
        "web.profile.blocked_companies": "Empresas bloqueadas",
        "web.profile.blocked_keywords": "Palavras bloqueadas no título",
        "web.profile.block.hint": "Uma por linha.",
        "web.profile.max_age_days": "Ignorar vagas com mais de (dias)",
        "web.profile.preferences": "Preferências para o Claude",
        "web.profile.preferences.hint": (
            "Texto livre que o Claude lê ao avaliar: o que você procura e o que quer evitar."
        ),
        "web.profile.paused": "Pausado: sem buscas e sem digest",
        "web.profile.save": "Salvar",
        "web.profile.saved": "Salvo.",
        "web.profile.saved_dropped": (
            "Salvo. {n} vagas não passam mais nos filtros e foram removidas."
        ),
        "web.profile.errors": "Alguns campos precisam de ajuste.",
        "web.profile.not_recognized": "Não reconhecido: {value}",
        "contract.full_time": "CLT / tempo integral",
        "contract.part_time": "Meio período",
        "contract.contract": "PJ / contrato",
        "contract.freelance": "Freelance",
        "contract.internship": "Estágio",
        "contract.temporary": "Temporário",
        "web.nav.cv": "Currículo",
        "web.nav.status": "Status",
        "web.nav.account": "Conta",
        "web.jobs.sort": "Ordem",
        "web.sort.score_desc": "Maior fit primeiro",
        "web.sort.score_asc": "Menor fit primeiro",
        "web.sort.date_desc": "Mais novas primeiro",
        "web.sort.date_asc": "Mais antigas primeiro",
        "web.search.now": "Buscar agora",
        "web.search.running": "Busca em andamento",
        "progress.start": "Começando...",
        "progress.collect": "Coletando de {source} ({i} de {n})",
        "progress.store": "Salvando {n} vagas e removendo duplicadas",
        "progress.filter": "Aplicando os filtros de {profile}",
        "progress.score": "Claude está avaliando as vagas de {profile} ({i} de {n})",
        "progress.batch": "Aguardando o Claude (lote de {n} vagas)",
        "web.status.search": "Busca",
        "web.status.running_since": "Começou às {when}",
        "web.status.kind.scheduled": "agendada",
        "web.status.kind.on_demand": "manual",
        "web.status.failed": "A busca de {when} falhou: {error}",
        "web.status.done": (
            "Última busca ({when}): {approved} vagas passaram nos seus filtros, {scored} "
            "avaliadas pelo Claude."
        ),
        "web.status.see_jobs": "Ver as vagas",
        "web.status.search_hint": (
            "Busca só os termos do seu perfil; leva alguns minutos e o resumo do Telegram "
            "chega no fim."
        ),
        "web.status.paused": "Seu perfil está pausado: as buscas agendadas pulam ele.",
        "web.status.next": "Próxima busca agendada: {when}",
        "web.status.waiting": "{n} vagas esperando avaliação",
        "web.status.claude": "Claude no mês",
        "web.status.spent": "US$ {spent} de US$ {cap} (seu: US$ {mine})",
        "web.status.operation": "Uso",
        "web.status.calls": "Chamadas",
        "web.status.yours": "Seu",
        "web.status.op.scoring": "Avaliar vagas",
        "web.status.op.extract_profile": "Ler currículos",
        "web.status.op.extract_job": "Vagas enviadas ao /avaliar",
        "web.status.op.learn_rules": "Regras do seu feedback",
        "web.status.op.eval": "Comparação de modelos",
        "web.status.sources": "Fontes",
        "web.status.source": "Fonte",
        "web.status.today": "Hoje",
        "web.status.month": "Mês",
        "web.status.sources_hint": (
            "Requisições usadas / permitidas. Os limites seguem os termos e o plano "
            "gratuito de cada fonte."
        ),
        "web.status.runs": "Rodadas recentes",
        "web.status.when": "Quando",
        "web.status.result": "Resultado",
        "web.status.collected": "Coletadas",
        "web.status.new": "Novas",
        "web.status.passed": "Passaram",
        "web.status.run.ok": "ok",
        "web.status.run.partial": "parcial",
        "web.status.run.error": "falhou",
        "web.status.run.running": "rodando",
        "web.status.errors": "{n} avisos",
        "web.status.no_runs": "Nenhuma rodada ainda.",
        "web.account.change": "Trocar senha",
        "web.account.current": "Senha atual",
        "web.account.new": "Nova senha",
        "web.account.repeat": "Repita a nova senha",
        "web.account.hint": "Pelo menos 8 caracteres. Os outros aparelhos são desconectados.",
        "web.account.submit": "Trocar senha",
        "web.account.changed": "Senha trocada. Os outros aparelhos foram desconectados.",
        "web.account.wrong_current": "A senha atual está errada.",
        "web.account.mismatch": "As duas senhas novas são diferentes.",
        "web.account.too_short": "A nova senha precisa de pelo menos {n} caracteres.",
        "web.telegram.title": "Telegram",
        "web.telegram.linked": "Ligado: o resumo e as respostas do bot vão para o seu chat.",
        "web.telegram.not_linked": "Ainda não ligado, então nenhum resumo chega até você.",
        "web.telegram.link": "Ligar Telegram",
        "web.telegram.relink": "Ligar outro chat",
        "web.telegram.unlink": "Desligar",
        "web.telegram.open": "Abrir no Telegram",
        "web.telegram.manual": "Ou mande esta mensagem ao bot:",
        "web.telegram.expires": "O código vale uma vez, até {time}.",
        "web.telegram.done": "Mandou? Recarregue esta página.",
        "web.telegram.no_bot": (
            "O bot do Telegram não está rodando (falta JOBFINDER_TELEGRAM_BOT_TOKEN), então "
            "ainda não há o que ligar."
        ),
        "web.telegram.unlinked": "Telegram desligado. Sem resumos até ligar de novo.",
        "web.learned.title": "Aprendido com seu feedback",
        "web.learned.intro": (
            "Regras que o Claude encontrou nas vagas que você salvou e descartou. Depois "
            "que você aprova, elas contam na nota de cada vaga nova. Edite à vontade."
        ),
        "web.learned.counts": "{saved} vagas salvas e {dismissed} descartadas até agora",
        "web.learned.checked": "o Claude olhou pela última vez {when}",
        "web.learned.thinking": (
            "O Claude está lendo suas decisões. Esta página se atualiza sozinha."
        ),
        "web.learned.failed": "Não foi possível sugerir regras: {error}",
        "web.learned.draft": "Regras sugeridas, esperando por você",
        "web.learned.draft_hint": (
            "Edite, apague ou acrescente linhas e aprove. Até lá, nenhuma conta."
        ),
        "web.learned.current": "Regras em uso",
        "web.learned.hint": "Uma regra por linha, no máximo {n}. Caixa vazia remove todas.",
        "web.learned.approve": "Aprovar estas regras",
        "web.learned.save": "Salvar",
        "web.learned.discard": "Descartar a sugestão",
        "web.learned.suggest_title": "Pedir ao Claude",
        "web.learned.suggest_hint": (
            "O Claude também faz isso sozinho toda semana quando há decisões novas."
        ),
        "web.learned.suggest": "Sugerir regras agora",
        "web.learned.need_more": "Salve ou descarte pelo menos {n} vagas antes.",
        "web.learned.saved": "Salvo. As próximas notas usam estas regras.",
        "web.learned.discarded": "Sugestão descartada.",
        "web.learned.no_change": "O Claude não encontrou nada novo.",
        "web.learned.summary": "{n} regras em uso",
        "web.learned.waiting": "sugestão nova esperando",
        "learn.proposed": (
            "💡 O Claude sugeriu {n} regras a partir das vagas que você salvou e descartou. "
            "Elas só contam depois que você aprova no site: Perfil → Aprendido com seu feedback."
        ),
        "web.cv.saved": "Salvo.",
        "web.cv.saved_terms": "Salvo. {n} novas buscas adicionadas ao seu perfil.",
        "web.cv.no_file": "Escolha um arquivo PDF.",
        "web.cv.too_big": "O arquivo tem mais de {mb} MB.",
        "web.cv.not_pdf": "Esse arquivo não é um PDF.",
        "web.cv.upload": "Enviar um novo currículo",
        "web.cv.extracting": (
            "O Claude está lendo seu currículo. Leva cerca de um minuto; a página se "
            "atualiza sozinha."
        ),
        "web.cv.failed": "Não foi possível ler o currículo: {error}",
        "web.cv.has_resume": "Seu currículo está guardado.",
        "web.cv.no_resume": "Nenhum currículo enviado pelo site ainda.",
        "web.cv.upload_hint": (
            "PDF de até {mb} MB. O Claude lê e você revisa o resultado antes de qualquer mudança."
        ),
        "web.cv.send": "Enviar e ler",
        "web.cv.review": (
            "Revise o que o Claude leu do seu currículo, corrija o que precisar e salve. "
            "Nada muda até você salvar."
        ),
        "web.cv.data": "Perfil profissional",
        "web.cv.data_hint": "O Claude avalia cada vaga comparando com isto.",
        "web.cv.summary": "Resumo",
        "web.cv.roles": "Cargos",
        "web.cv.seniority": "Senioridade",
        "web.cv.years": "Anos de experiência",
        "web.cv.skills": "Competências",
        "web.cv.skills_hint": (
            'Uma por linha: "competência | peso", peso de 1 (secundária) a 5 (central).'
        ),
        "web.cv.languages": "Idiomas",
        "web.cv.languages_hint": (
            'Um por linha: "código | nível", por exemplo "en | fluente". Níveis:'
        ),
        "web.cv.industries": "Setores",
        "web.cv.certifications": "Certificações",
        "web.cv.terms": "Buscas sugeridas",
        "web.cv.terms_hint": (
            "Os termos marcados entram nas suas buscas. Dá para editar depois em Perfil."
        ),
        "web.cv.save_draft": "Salvar no meu perfil",
        "web.cv.discard": "Descartar este currículo",
        "seniority.junior": "Júnior",
        "seniority.mid": "Pleno",
        "seniority.senior": "Sênior",
        "seniority.lead": "Líder técnico",
        "seniority.principal": "Principal",
        "seniority.manager": "Gerente",
        "seniority.director": "Diretor",
        "level.basic": "básico",
        "level.intermediate": "intermediário",
        "level.advanced": "avançado",
        "level.fluent": "fluente",
        "level.native": "nativo",
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
