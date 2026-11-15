"""Bot do Telegram (python-telegram-bot, long polling). Camada fina sobre `acoes`.

Só chats ligados a um perfil (`telegram_chat_id`) são atendidos; o resto é ignorado,
exceto o /start, que responde o chat_id para o cadastro.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from sqlalchemy import Engine
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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
            + ".\n/buscar · /salvas · /pausar · /status",
        )

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
