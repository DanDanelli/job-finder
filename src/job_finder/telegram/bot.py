"""Bot do Telegram (python-telegram-bot, long polling). Camada fina sobre `acoes`.

Só chats ligados a um perfil (`telegram_chat_id`) são atendidos; o resto é ignorado,
exceto o /start, que responde o chat_id para o cadastro.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Any

from sqlalchemy import Engine
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from job_finder.config import Settings
from job_finder.scheduler import Agendador
from job_finder.telegram import acoes
from job_finder.telegram.acoes import PerfilChat
from job_finder.telegram.digest import Botao, botoes_da_vaga

log = logging.getLogger(__name__)

Contexto = ContextTypes.DEFAULT_TYPE
Handler = Callable[[Update, Contexto, PerfilChat], Coroutine[Any, Any, None]]


def teclado(botoes: Sequence[Botao]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b.texto, callback_data=b.callback, url=b.url) for b in botoes]]
    )


class EnviadorTelegram:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def enviar(self, chat_id: int, texto: str, botoes: Sequence[Botao] | None = None) -> None:
        await self.bot.send_message(
            chat_id,
            texto,
            parse_mode=ParseMode.HTML,
            reply_markup=teclado(botoes) if botoes else None,
            disable_web_page_preview=True,
        )


class Agrupador:
    """Junta mensagens seguidas do mesmo chat antes de processar.

    O Telegram corta um texto longo colado em várias mensagens de até 4096 caracteres;
    sem juntar, cada pedaço viraria uma avaliação.
    """

    def __init__(self, processar: Callable[[int, str], Awaitable[None]], espera_s: float = 2.0):
        self.processar = processar
        self.espera_s = espera_s
        self._partes: dict[int, list[str]] = {}
        self._tarefas: dict[int, asyncio.Task[None]] = {}

    def adicionar(self, chat_id: int, texto: str) -> None:
        self._partes.setdefault(chat_id, []).append(texto)
        anterior = self._tarefas.pop(chat_id, None)
        if anterior is not None:
            anterior.cancel()
        self._tarefas[chat_id] = asyncio.create_task(self._depois(chat_id))

    async def _depois(self, chat_id: int) -> None:
        await asyncio.sleep(self.espera_s)
        # Sai do registro antes de processar: uma mensagem nova não cancela o que já começou.
        self._tarefas.pop(chat_id, None)
        texto = "\n".join(self._partes.pop(chat_id, []))
        try:
            await self.processar(chat_id, texto)
        except Exception:
            log.exception("falha ao processar mensagem do chat %s", chat_id)


def registrar_handlers(
    app: Application,
    engine: Engine,
    settings: Settings,
    agendador: Agendador,
) -> None:
    def autorizado(handler: Handler) -> Callable[[Update, Contexto], Coroutine[Any, Any, None]]:
        async def wrapper(update: Update, context: Contexto) -> None:
            chat = update.effective_chat
            if chat is None:
                return
            perfil = await asyncio.to_thread(acoes.perfil_do_chat, engine, chat.id)
            if perfil is None:
                log.warning("chat %s não autorizado", chat.id)
                return
            await handler(update, context, perfil)

        return wrapper

    async def responder(update: Update, texto: str) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                texto, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )

    async def start(update: Update, context: Contexto) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        perfil = await asyncio.to_thread(acoes.perfil_do_chat, engine, chat.id)
        if perfil is None:
            log.info("/start de chat não cadastrado: %s (%s)", chat.id, chat.full_name)
            await responder(
                update,
                f"Olá! Este chat ainda não está ligado a um perfil.\n"
                f"chat_id: <code>{chat.id}</code>\n"
                "Coloque esse número em <code>telegram_chat_id</code> no YAML do perfil.",
            )
            return
        await responder(
            update,
            f"Olá, {perfil.nome}! Digest às "
            + ", ".join(h.strftime("%H:%M") for h in settings.horarios)
            + ".\n/buscar · /salvas · /pausar · /status\n\n"
            + acoes.COMO_AVALIAR,
        )

    enviador = EnviadorTelegram(app.bot)

    async def avaliar_e_responder(chat_id: int, texto: str) -> None:
        perfil = await asyncio.to_thread(acoes.perfil_do_chat, engine, chat_id)
        if perfil is None:
            return
        await enviador.enviar(chat_id, "🔎 Avaliando a vaga...")
        try:
            resposta, botoes = await asyncio.to_thread(
                acoes.avaliar_no_chat, engine, settings, perfil.id, texto
            )
        except Exception:
            log.exception("avaliação avulsa falhou")
            resposta, botoes = "Deu erro ao avaliar essa vaga. Tente de novo mais tarde.", []
        await enviador.enviar(chat_id, resposta, botoes)

    agrupador = Agrupador(avaliar_e_responder)

    @autorizado
    async def avaliar(update: Update, context: Contexto, perfil: PerfilChat) -> None:
        # O texto inteiro da mensagem, não `context.args`, para manter as quebras de linha.
        mensagem = update.effective_message.text if update.effective_message else None
        partes = (mensagem or "").split(maxsplit=1)
        texto = partes[1] if len(partes) > 1 else ""
        if not texto or update.effective_chat is None:
            await responder(update, acoes.COMO_AVALIAR)
            return
        await avaliar_e_responder(update.effective_chat.id, texto)

    @autorizado
    async def texto_livre(update: Update, context: Contexto, perfil: PerfilChat) -> None:
        if update.effective_chat and update.effective_message and update.effective_message.text:
            agrupador.adicionar(update.effective_chat.id, update.effective_message.text)

    @autorizado
    async def buscar(update: Update, context: Contexto, perfil: PerfilChat) -> None:
        if agendador.ocupado:
            await responder(update, "Já tem uma rodada em andamento; o digest chega ao fim dela.")
            return
        await responder(update, "Buscando vagas... o digest chega em alguns minutos.")
        relatorio = await agendador.rodar_e_notificar([perfil.nome], tipo="sob_demanda")
        if relatorio is None:
            await responder(update, "Não encontrei o seu perfil.")

    @autorizado
    async def salvas(update: Update, context: Contexto, perfil: PerfilChat) -> None:
        await responder(update, await asyncio.to_thread(acoes.texto_salvas, engine, perfil.id))

    @autorizado
    async def pausar(update: Update, context: Contexto, perfil: PerfilChat) -> None:
        pausado = await asyncio.to_thread(acoes.alternar_pausa, engine, perfil.id)
        await responder(
            update,
            "⏸ Digest pausado. Mande /pausar de novo para retomar."
            if pausado
            else "▶️ Digest retomado.",
        )

    @autorizado
    async def status(update: Update, context: Contexto, perfil: PerfilChat) -> None:
        texto = await asyncio.to_thread(acoes.texto_status, engine, perfil.id, settings)
        await responder(update, texto)

    async def feedback(update: Update, context: Contexto) -> None:
        query = update.callback_query
        chat = update.effective_chat
        if query is None or chat is None or not query.data:
            return
        resultado = await asyncio.to_thread(acoes.registrar_feedback, engine, chat.id, query.data)
        if resultado is None:
            await query.answer("Ação inválida.")
            return
        await query.answer("Salva ✅" if resultado.status == "salva" else "Descartada")
        await query.edit_message_reply_markup(
            teclado(botoes_da_vaga(resultado.match_id, resultado.url, resultado.status))
        )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buscar", buscar, block=False))
    app.add_handler(CommandHandler("salvas", salvas))
    app.add_handler(CommandHandler("pausar", pausar))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("avaliar", avaliar, block=False))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto_livre))
    app.add_handler(CallbackQueryHandler(feedback, pattern=r"^fb:"))


def criar_aplicacao(token: str) -> Application:
    # Timeouts maiores que o padrão (5 s): conexões lentas eventuais não derrubam o envio.
    return (
        Application.builder()
        .token(token)
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .build()
    )
