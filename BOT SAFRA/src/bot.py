from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable

import discord
from discord import app_commands
from discord.app_commands import AppCommandContext, AppInstallationType
from discord.ext import commands, tasks

from src.account_ui import AccountCreationModal
from src.config import Settings, load_settings
from src.database import Database
from src.economy import EconomyService
from src.investment_ui import InvestmentHubView
from src.investments import FundUpdateResult, InvestmentService
from src.notification_ui import NotificationsView, build_notifications_embed
from src.notifications import NotificationService, enviar_notificacao
from src.security_ui import PasswordModal
from src.utils import format_currency, format_datetime, log_transaction, make_bank_embed


FUND_CHOICES = [
    app_commands.Choice(name="Conservador", value="conservador"),
    app_commands.Choice(name="Moderado", value="moderado"),
    app_commands.Choice(name="Agressivo", value="agressivo"),
]

ADMIN_CONTEXT = AppCommandContext(guild=True, dm_channel=False, private_channel=False)
ADMIN_INSTALL = AppInstallationType(guild=True, user=False)

PASSWORD_AREA_CHOICES = [
    app_commands.Choice(name="Saldo", value="saldo"),
    app_commands.Choice(name="Investimentos", value="investimentos"),
]

ACCOUNT_WEEKLY_FEE = 10000.0

ProtectedCallback = Callable[[discord.Interaction], Awaitable[None]]


class BancoSafraBot(commands.Bot):
    def __init__(self, settings: Settings, database: Database) -> None:
        intents = discord.Intents.default()
        intents.guilds = True

        super().__init__(
            command_prefix=settings.prefix,
            intents=intents,
            help_command=None,
        )
        self.settings = settings
        self.database = database
        self.economy = EconomyService(database)
        self.investments = InvestmentService(database)
        self.notifications = NotificationService(database)
        self._commands_synced = False

    async def setup_hook(self) -> None:
        self._register_commands()
        self.tree.on_error = self.on_app_command_error
        if not self.automation_loop.is_running():
            self.automation_loop.start()

    async def on_ready(self) -> None:
        if not self._commands_synced:
            for guild in self.guilds:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            self._commands_synced = True
        print(f"{self.settings.bot_name} conectado como {self.user}.")

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await self._reply_text(
                interaction,
                title="Permissao negada",
                description="Apenas administradores podem usar este comando.",
                color=0xB22222,
                ephemeral=True,
            )
            return

        original_error = getattr(error, "original", error)
        await self._reply_text(
            interaction,
            title="Operacao nao concluida",
            description=str(original_error),
            color=0xB22222,
            ephemeral=True,
        )

    @tasks.loop(minutes=5)
    async def automation_loop(self) -> None:
        updated_funds = self.investments.update_funds()
        matured_cdbs = self.investments.check_matured_investments()
        debts = self.database.list_active_debts()
        due_profiles = self.database.list_due_account_fees(datetime.now(UTC).isoformat())

        if updated_funds:
            log_transaction(
                self.settings.log_path,
                f"FUNDS_UPDATE | atualizados={len(updated_funds)}",
            )

        for update in updated_funds:
            await self._handle_fund_update(update)

        for investment in matured_cdbs:
            await enviar_notificacao(
                self,
                int(investment["user_id"]),
                "investimento_liberado",
                (
                    f"⏰ Seu CDB #{investment['id']} esta pronto para resgate. "
                    f"Valor liberado: {format_currency(float(investment['valor_atual']))}."
                ),
                dedupe_key=f"cdb_liberado:{investment['id']}",
            )

        for debt in debts:
            await self._process_debt_notification(debt)

        for profile in due_profiles:
            await self._process_weekly_account_fee(profile)

    @automation_loop.before_loop
    async def before_automation_loop(self) -> None:
        await self.wait_until_ready()

    def run_from_env(self) -> None:
        self.run(self.settings.token)

    async def _reply_embed(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        *,
        view: discord.ui.View | None = None,
        ephemeral: bool = False,
    ) -> None:
        file = discord.File(self.settings.logo_path, filename="logo.png")
        kwargs: dict[str, object] = {
            "embed": embed,
            "file": file,
            "ephemeral": ephemeral,
        }
        if view is not None:
            kwargs["view"] = view

        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
            return

        await interaction.response.send_message(**kwargs)

    async def _defer_if_needed(
        self,
        interaction: discord.Interaction,
        *,
        ephemeral: bool = False,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral)

    async def _reply_text(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
        color: int,
        ephemeral: bool = False,
    ) -> None:
        await self._reply_embed(
            interaction,
            make_bank_embed(title, description, color=color),
            ephemeral=ephemeral,
        )

    def hash_password(self, password: str) -> str:
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    def validate_password(self, user_id: int, area: str, password: str) -> bool:
        stored_hash = self.database.get_user_password(user_id, area)
        if stored_hash is None:
            return False
        return stored_hash == self.hash_password(password)

    async def _run_protected(
        self,
        interaction: discord.Interaction,
        *,
        area: str,
        callback: ProtectedCallback,
    ) -> None:
        if self.database.get_user_password(interaction.user.id, area) is None:
            await self._reply_text(
                interaction,
                title="Senha nao definida",
                description=(
                    "Defina uma senha antes de acessar essa area com "
                    "`/definir senha`."
                ),
                color=0xB22222,
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            PasswordModal(self, area=area, callback=callback)
        )

    def get_manager_id(self) -> int | None:
        raw = self.database.get_bot_setting("manager_user_id")
        return None if raw is None else int(raw)

    def get_transactions_channel_id(self) -> int | None:
        raw = self.database.get_bot_setting("transactions_channel_id")
        return None if raw is None else int(raw)

    def get_account_posts_channel_id(self) -> int | None:
        raw = self.database.get_bot_setting("account_posts_channel_id")
        return None if raw is None else int(raw)

    async def send_transaction_log(
        self,
        *,
        title: str,
        lines: list[str],
        color: int = 0x0B4EA2,
    ) -> None:
        channel_id = self.get_transactions_channel_id()
        if channel_id is None:
            return

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                print(f"[Transacoes] Falha ao buscar canal {channel_id}: {exc}")
                return

        if not isinstance(channel, discord.abc.Messageable):
            return

        embed = make_bank_embed(title, "\n".join(lines), color=color)
        file = discord.File(self.settings.logo_path, filename="logo.png")
        try:
            await channel.send(embed=embed, file=file)
        except discord.HTTPException as exc:
            print(f"[Transacoes] Falha ao enviar log no canal {channel_id}: {exc}")

    async def publish_account_post(
        self,
        *,
        member: discord.abc.User,
        profile,
        wallet: float,
        balance: float,
        total: float,
        credit: float,
    ) -> None:
        channel_id = self.get_account_posts_channel_id()
        if channel_id is None:
            return

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                print(f"[Contas] Falha ao buscar canal {channel_id}: {exc}")
                return

        embed = self._build_profile_embed(
            title="🏦 Nova conta registrada",
            member=member,
            profile=profile,
            wallet=wallet,
            balance=balance,
            total=total,
            credit=credit,
            color=0x1E8E5A,
        )
        embed.description = (
            "Cadastro aprovado e publicado no mural oficial de contas do Banco Safra."
        )
        profile_name = str(profile["nome_completo"]).strip()[:100]
        file = discord.File(self.settings.logo_path, filename="logo.png")

        try:
            if isinstance(channel, discord.ForumChannel):
                await channel.create_thread(
                    name=profile_name,
                    content=(
                        f"📛 **{profile['nome_completo']}**\n"
                        f"👤 Cliente: {member.mention}\n"
                        f"🎯 Tipo de conta: **{profile['tipo_conta']}**"
                    ),
                    embed=embed,
                    file=file,
                )
                return

            if isinstance(channel, discord.TextChannel):
                await channel.send(
                    content=f"## 📛 {profile['nome_completo']}",
                    embed=embed,
                    file=file,
                )
                return

            print(f"[Contas] Canal {channel_id} nao suporta publicacao de contas.")
        except discord.HTTPException as exc:
            print(f"[Contas] Falha ao publicar conta no canal {channel_id}: {exc}")

    async def _credit_manager_loss(
        self,
        amount: float,
        *,
        source: str,
        actor_user_id: int | None = None,
    ) -> None:
        if amount <= 0:
            return
        manager_id = self.get_manager_id()
        if manager_id is None:
            return

        new_balance = self.database.update_balance(manager_id, amount)
        message = (
            f"🎁 A conta gerente recebeu {format_currency(amount)} "
            f"de perdas em {source}."
        )
        if actor_user_id is not None:
            message += f" Conta de origem: <@{actor_user_id}>."

        await enviar_notificacao(
            self,
            manager_id,
            "bonus",
            f"{message} Saldo atual do gerente: {format_currency(new_balance)}.",
        )
        await self.send_transaction_log(
            title="🎁 Credito para conta gerente",
            lines=[
                message,
                f"Saldo atual do gerente: **{format_currency(new_balance)}**",
            ],
            color=0x7C3AED,
        )

    async def _notify_low_balance_if_needed(self, user_id: int) -> None:
        current_balance = self.economy.get_balance(user_id)
        if current_balance >= self.settings.low_balance_alert:
            return

        await enviar_notificacao(
            self,
            user_id,
            "saldo_baixo",
            (
                f"📉 Seu saldo no banco esta abaixo do limite de alerta. "
                f"Saldo atual: {format_currency(current_balance)}."
            ),
            dedupe_key=f"saldo_baixo:{user_id}",
            dedupe_window=timedelta(hours=6),
        )

    async def _handle_fund_update(self, update: FundUpdateResult) -> None:
        if update.delta < 0:
            await self._credit_manager_loss(
                abs(update.delta),
                source=f"fundo #{update.investment['id']}",
                actor_user_id=int(update.investment["user_id"]),
            )

    async def _process_debt_notification(self, debt) -> None:
        due_at = datetime.fromisoformat(str(debt["vencimento"]))
        now = datetime.now(UTC)
        last_alert = str(debt["ultimo_alerta"] or "")

        if due_at <= now and last_alert != "vencida":
            sent = await enviar_notificacao(
                self,
                int(debt["user_id"]),
                "divida_vencida",
                (
                    f"🚨 Sua divida #{debt['id']} venceu. "
                    f"Valor em aberto: {format_currency(float(debt['valor']))}."
                ),
                dedupe_key=f"divida_vencida:{debt['id']}",
            )
            if sent:
                self.database.update_debt_alert(int(debt["id"]), "vencida")
            return

        if due_at - now <= timedelta(hours=24) and last_alert not in {"vencendo", "vencida"}:
            sent = await enviar_notificacao(
                self,
                int(debt["user_id"]),
                "divida_vencendo",
                (
                    f"⚠️ Sua divida #{debt['id']} vence em breve. "
                    f"Valor: {format_currency(float(debt['valor']))} | "
                    f"Vencimento: {format_datetime(str(debt['vencimento']))}."
                ),
                dedupe_key=f"divida_vencendo:{debt['id']}",
            )
            if sent:
                self.database.update_debt_alert(int(debt["id"]), "vencendo")

    async def _process_weekly_account_fee(self, profile) -> None:
        manager_id = self.get_manager_id()
        if manager_id is None:
            print("[Conta] Cobranca semanal ignorada: conta gerente nao configurada.")
            return

        user_id = int(profile["user_id"])
        due_at = datetime.fromisoformat(str(profile["next_fee_at"]))
        now = datetime.now(UTC)
        next_fee_at = due_at
        while next_fee_at <= now:
            next_fee_at += timedelta(days=7)

        current_balance = self.economy.get_balance(user_id)
        charged = min(current_balance, ACCOUNT_WEEKLY_FEE)
        remaining = round(ACCOUNT_WEEKLY_FEE - charged, 2)

        if charged > 0:
            self.database.update_balance(user_id, -charged)
            manager_balance = self.database.update_balance(manager_id, charged)
            await enviar_notificacao(
                self,
                manager_id,
                "bonus",
                (
                    f"💼 A conta gerente recebeu {format_currency(charged)} "
                    f"da tarifa semanal da conta de <@{user_id}>. "
                    f"Saldo atual do gerente: {format_currency(manager_balance)}."
                ),
            )

        debt_text = "Nenhuma pendencia foi gerada."
        if remaining > 0:
            debt_id = self.database.create_debt(
                user_id=user_id,
                valor=remaining,
                vencimento=(now + timedelta(days=3)).isoformat(),
            )
            debt_text = (
                f"Foi gerada a divida **#{debt_id}** no valor de "
                f"**{format_currency(remaining)}**."
            )

        self.database.update_account_fee_date(user_id, next_fee_at.isoformat())

        await enviar_notificacao(
            self,
            user_id,
            "envio",
            (
                f"🏦 A tarifa semanal da sua conta foi processada.\n"
                f"Valor da tarifa: {format_currency(ACCOUNT_WEEKLY_FEE)}\n"
                f"Valor debitado: {format_currency(charged)}\n"
                f"{debt_text}"
            ),
            title="🏦 Tarifa semanal da conta",
            dedupe_key=f"tarifa_conta:{user_id}:{due_at.isoformat()}",
        )
        await self.send_transaction_log(
            title="🏦 Tarifa semanal processada",
            lines=[
                f"Cliente: <@{user_id}>",
                f"Tarifa prevista: **{format_currency(ACCOUNT_WEEKLY_FEE)}**",
                f"Valor debitado: **{format_currency(charged)}**",
                debt_text,
                f"Conta gerente: <@{manager_id}>",
            ],
            color=0x0B4EA2,
        )
        await self._notify_low_balance_if_needed(user_id)

    def build_investment_hub_embed(self) -> discord.Embed:
        embed = make_bank_embed(
            "Central de Investimentos - Banco Safra",
            (
                "Escolha uma modalidade para investir o saldo do banco com "
                "seguranca, risco calculado ou fundos de longo prazo."
            ),
            color=0x123E7C,
        )
        embed.add_field(
            name="\U0001F4C8 CDB travado",
            value="Prazo fixo com retorno previsivel.",
            inline=False,
        )
        embed.add_field(
            name="\U0001F3B2 Investimento de risco",
            value="Resultado imediato com chance de lucro ou perda.",
            inline=False,
        )
        embed.add_field(
            name="\U0001FA99 Fundos",
            value="Carteiras com variacao automatica ao longo do tempo.",
            inline=False,
        )
        return embed

    def build_notifications_embed(self, user_id: int) -> discord.Embed:
        return build_notifications_embed(self, user_id)

    def _build_account_embed(
        self,
        *,
        title: str,
        member: discord.abc.User,
        wallet: float,
        balance: float,
        total: float,
        color: int,
        credit: float | None = None,
    ) -> discord.Embed:
        embed = make_bank_embed(
            title,
            "Painel financeiro atualizado com sucesso.",
            color=color,
        )
        embed.add_field(name="\U0001F464 Cliente", value=member.mention, inline=False)
        embed.add_field(
            name="\U0001F4B5 Em maos / fisico",
            value=f"**{format_currency(wallet)}**",
            inline=True,
        )
        embed.add_field(
            name="\U0001F3E6 Depositado no banco",
            value=f"**{format_currency(balance)}**",
            inline=True,
        )
        embed.add_field(
            name="\U0001F4A0 Patrimonio total",
            value=f"**{format_currency(total)}**",
            inline=False,
        )
        if credit is not None:
            embed.add_field(
                name="\U0001F4B3 Credito disponivel",
                value=f"**{format_currency(credit)}**",
                inline=False,
            )
        return embed

    def _build_action_embed(
        self,
        *,
        title: str,
        color: int,
        lines: list[str],
    ) -> discord.Embed:
        return make_bank_embed(title, "\n".join(lines), color=color)

    def _build_profile_embed(
        self,
        *,
        title: str,
        member: discord.abc.User,
        profile,
        wallet: float,
        balance: float,
        total: float,
        credit: float,
        color: int,
    ) -> discord.Embed:
        embed = make_bank_embed(
            title,
            "Painel completo da conta bancaria RP.",
            color=color,
        )
        embed.add_field(name="📛 Nome RP", value=str(profile["nome_completo"]), inline=False)
        embed.add_field(name="👤 Cliente", value=member.mention, inline=True)
        embed.add_field(name="🪪 ID do Discord", value=f"`{profile['discord_id']}`", inline=True)
        embed.add_field(name="🎯 Tipo de conta", value=str(profile["tipo_conta"]).title(), inline=True)
        embed.add_field(
            name="📱 Contato RP",
            value=str(profile["telefone_rp"] or "Nao informado"),
            inline=True,
        )
        embed.add_field(
            name="📅 Conta criada",
            value=format_datetime(str(profile["created_at"])),
            inline=True,
        )
        embed.add_field(
            name="⏰ Proxima tarifa semanal",
            value=format_datetime(str(profile["next_fee_at"])),
            inline=True,
        )
        embed.add_field(
            name="🟢 Status",
            value=str(profile["status"]).title(),
            inline=True,
        )
        embed.add_field(
            name="💵 Em maos / fisico",
            value=f"**{format_currency(wallet)}**",
            inline=True,
        )
        embed.add_field(
            name="🏦 Depositado no banco",
            value=f"**{format_currency(balance)}**",
            inline=True,
        )
        embed.add_field(
            name="💠 Patrimonio total",
            value=f"**{format_currency(total)}**",
            inline=True,
        )
        embed.add_field(
            name="💳 Credito disponivel",
            value=f"**{format_currency(credit)}**",
            inline=True,
        )
        embed.add_field(
            name="💼 Deposito inicial",
            value=f"**{format_currency(float(profile['deposito_inicial']))}**",
            inline=True,
        )
        embed.add_field(
            name="🔐 Seguranca",
            value="Saldo e investimentos protegidos por senha.",
            inline=True,
        )
        return embed

    def _build_investments_overview_embed(self, user_id: int) -> discord.Embed:
        investments = self.investments.get_all_active_investments(user_id)
        embed = make_bank_embed(
            "\U0001F4DA Carteira de investimentos",
            "Resumo dos seus investimentos ativos.",
            color=0x1F3C88,
        )
        if not investments:
            embed.description = "Voce nao possui investimentos ativos no momento."
            return embed

        for investment in investments[:10]:
            if investment["tipo"] == "cdb":
                remaining = self.investments.investment_remaining(investment)
                status = (
                    "Liberado"
                    if remaining is not None and remaining.total_seconds() == 0
                    else self.investments.format_remaining_time(remaining or timedelta(0))
                )
                embed.add_field(
                    name=f"\U0001F4C8 CDB #{investment['id']}",
                    value=(
                        f"Valor atual: **{format_currency(float(investment['valor_atual']))}**\n"
                        f"Resgate: **{format_datetime(str(investment['data_resgate']))}**\n"
                        f"Tempo restante: **{status}**"
                    ),
                    inline=False,
                )
            elif investment["tipo"] == "fundo":
                delta, percent = self.investments.describe_fund_performance(investment)
                sign = "+" if delta >= 0 else "-"
                embed.add_field(
                    name=f"\U0001FA99 Fundo #{investment['id']} - {str(investment['subtipo']).title()}",
                    value=(
                        f"Atual: **{format_currency(float(investment['valor_atual']))}**\n"
                        f"Variacao: **{sign}{format_currency(abs(delta))} ({sign}{abs(percent):.2f}%)**\n"
                        f"Inicio: **{format_datetime(str(investment['data_inicio']))}**"
                    ),
                    inline=False,
                )
        return embed

    def _build_fund_status_embed(self, user_id: int) -> discord.Embed:
        funds = self.investments.list_user_funds(user_id)
        embed = make_bank_embed(
            "\U0001FA99 Status dos fundos",
            "Acompanhe o desempenho dos seus fundos ativos.",
            color=0x8C6B00,
        )
        if not funds:
            embed.description = "Voce nao possui fundos ativos no momento."
            return embed

        for fund in funds[:10]:
            delta, percent = self.investments.describe_fund_performance(fund)
            sign = "+" if delta >= 0 else "-"
            embed.add_field(
                name=f"Fundo #{fund['id']} - {str(fund['subtipo']).title()}",
                value=(
                    f"Aplicado: **{format_currency(float(fund['valor_inicial']))}**\n"
                    f"Atual: **{format_currency(float(fund['valor_atual']))}**\n"
                    f"Resultado: **{sign}{format_currency(abs(delta))} ({sign}{abs(percent):.2f}%)**"
                ),
                inline=False,
            )
        return embed

    def _build_help_embed(self) -> discord.Embed:
        embed = make_bank_embed(
            "\U0001F4D8 Painel de comandos",
            "Central de comandos do Banco Safra BOT.",
            color=0x0B4EA2,
        )
        embed.add_field(
            name="\U0001F512 Seguranca",
            value="`/criar_conta`\n`/conta`\n`/definir senha`",
            inline=True,
        )
        embed.add_field(
            name="\U0001F4B5 Economia",
            value="`/depositar`\n`/sacar`\n`/pagar`\n`/saldo`\n`/credito`",
            inline=True,
        )
        embed.add_field(
            name="\U0001F4C8 Investimentos",
            value="`/investir`\n`/resgatar`\n`/investimentos`\n`/fundo investir`\n`/fundo status`\n`/fundo sacar`",
            inline=True,
        )
        embed.add_field(
            name="\U0001F514 Notificacoes",
            value="`/notificacoes`",
            inline=True,
        )
        embed.add_field(
            name="\U0001F6E0\ufe0f Administracao",
            value=(
                "`/addmoney`\n`/removemoney`\n`/addcredito`\n"
                "`/removecredito`\n`/consultar saldo`\n"
                "`/consultar conta`\n`/gerente conta`\n"
                "`/canal transacoes`\n`/canal contas`"
            ),
            inline=False,
        )
        return embed

    def _register_commands(self) -> None:
        consultar_group = app_commands.Group(
            name="consultar",
            description="Comandos administrativos de consulta.",
        )
        fundo_group = app_commands.Group(
            name="fundo",
            description="Comandos de fundos de investimento.",
        )
        canal_group = app_commands.Group(
            name="canal",
            description="Configuracoes de canais do bot.",
        )
        gerente_group = app_commands.Group(
            name="gerente",
            description="Configuracoes administrativas do gerente.",
        )
        definir_group = app_commands.Group(
            name="definir",
            description="Comandos de configuracao pessoal.",
        )

        @self.tree.command(name="depositar", description="Deposita dinheiro que esta em maos.")
        async def depositar(
            interaction: discord.Interaction,
            valor: app_commands.Range[float, 0.01, None],
        ) -> None:
            async def action(inner: discord.Interaction) -> None:
                amount = round(float(valor), 2)
                result = self.economy.deposit(inner.user.id, amount)
                log_transaction(
                    self.settings.log_path,
                    f"DEPOSITO | user={inner.user.id} | valor={amount:.2f}",
                )
                embed = self._build_action_embed(
                    title="\u2705 Deposito aprovado",
                    color=0x137D3E,
                    lines=[
                        f"\U0001F4B0 Valor depositado: **{format_currency(amount)}**",
                        f"\U0001F4B5 Em maos agora: **{format_currency(result.wallet or 0)}**",
                        f"\U0001F3E6 No banco agora: **{format_currency(result.balance or 0)}**",
                    ],
                )
                await self._reply_embed(inner, embed)
                await self.send_transaction_log(
                    title="✅ Deposito registrado",
                    lines=[
                        f"Usuario: {inner.user.mention}",
                        f"Valor depositado: **{format_currency(amount)}**",
                        f"No banco agora: **{format_currency(result.balance or 0)}**",
                    ],
                    color=0x137D3E,
                )
                await self._notify_low_balance_if_needed(inner.user.id)

            await self._run_protected(interaction, area="saldo", callback=action)

        @self.tree.command(name="sacar", description="Saca dinheiro do banco para sua mao.")
        async def sacar(
            interaction: discord.Interaction,
            valor: app_commands.Range[float, 0.01, None],
        ) -> None:
            async def action(inner: discord.Interaction) -> None:
                amount = round(float(valor), 2)
                result = self.economy.withdraw(inner.user.id, amount)
                log_transaction(
                    self.settings.log_path,
                    f"SAQUE | user={inner.user.id} | valor={amount:.2f}",
                )
                embed = self._build_action_embed(
                    title="\U0001F4B8 Saque aprovado",
                    color=0xC97C00,
                    lines=[
                        f"\U0001F3E7 Valor sacado: **{format_currency(amount)}**",
                        f"\U0001F4B5 Em maos agora: **{format_currency(result.wallet or 0)}**",
                        f"\U0001F3E6 No banco agora: **{format_currency(result.balance or 0)}**",
                    ],
                )
                await self._reply_embed(inner, embed)
                await self.send_transaction_log(
                    title="💸 Saque registrado",
                    lines=[
                        f"Usuario: {inner.user.mention}",
                        f"Valor sacado: **{format_currency(amount)}**",
                        f"Em maos agora: **{format_currency(result.wallet or 0)}**",
                    ],
                    color=0xC97C00,
                )

            await self._run_protected(interaction, area="saldo", callback=action)

        @self.tree.command(
            name="pagar",
            description="Transfere dinheiro que esta em maos para outro usuario.",
        )
        async def pagar(
            interaction: discord.Interaction,
            usuario: discord.Member,
            valor: app_commands.Range[float, 0.01, None],
        ) -> None:
            async def action(inner: discord.Interaction) -> None:
                amount = round(float(valor), 2)
                result = self.economy.pay(inner.user.id, usuario.id, amount)
                log_transaction(
                    self.settings.log_path,
                    (
                        f"PAGAMENTO | de={inner.user.id} | para={usuario.id} "
                        f"| valor={amount:.2f}"
                    ),
                )
                embed = self._build_action_embed(
                    title="\U0001F91D Transferencia concluida",
                    color=0x0B4EA2,
                    lines=[
                        f"\U0001F464 Destinatario: {usuario.mention}",
                        f"\U0001F4B8 Valor enviado: **{format_currency(amount)}**",
                        f"\U0001F4B5 Em maos agora: **{format_currency(result.wallet or 0)}**",
                    ],
                )
                await self._reply_embed(inner, embed)
                await self.send_transaction_log(
                    title="🤝 Transferencia registrada",
                    lines=[
                        f"Remetente: {inner.user.mention}",
                        f"Destino: {usuario.mention}",
                        f"Valor: **{format_currency(amount)}**",
                    ],
                )
                await enviar_notificacao(
                    self,
                    inner.user.id,
                    "envio",
                    f"💸 Seu pagamento de {format_currency(amount)} para {usuario.mention} foi confirmado.",
                )
                await enviar_notificacao(
                    self,
                    usuario.id,
                    "recebimento",
                    f"💰 Voce recebeu {format_currency(amount)} de {inner.user.mention}.",
                )

            await self._run_protected(interaction, area="saldo", callback=action)

        @self.tree.command(name="saldo", description="Mostra dinheiro em maos, banco e total.")
        async def saldo(interaction: discord.Interaction) -> None:
            async def action(inner: discord.Interaction) -> None:
                wallet = self.economy.get_wallet(inner.user.id)
                balance = self.economy.get_balance(inner.user.id)
                total = self.economy.get_total_balance(inner.user.id)
                embed = self._build_account_embed(
                    title="\U0001F4CA Saldo atual",
                    member=inner.user,
                    wallet=wallet,
                    balance=balance,
                    total=total,
                    color=0x0B4EA2,
                )
                await self._reply_embed(inner, embed)

            await self._run_protected(interaction, area="saldo", callback=action)

        @self.tree.command(name="credito", description="Mostra o seu credito atual.")
        async def credito(interaction: discord.Interaction) -> None:
            async def action(inner: discord.Interaction) -> None:
                credit = self.economy.get_credit(inner.user.id)
                embed = make_bank_embed(
                    "\U0001F4B3 Credito atual",
                    "Limite consultado com sucesso.",
                    color=0x8C6B00,
                )
                embed.add_field(
                    name="\U0001F464 Cliente",
                    value=inner.user.mention,
                    inline=False,
                )
                embed.add_field(
                    name="\U0001F4B3 Credito disponivel",
                    value=f"**{format_currency(credit)}**",
                    inline=False,
                )
                await self._reply_embed(inner, embed)

            await self._run_protected(interaction, area="saldo", callback=action)

        @self.tree.command(
            name="criar_conta",
            description="Abre o questionario de abertura da sua conta RP.",
        )
        async def criar_conta(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(AccountCreationModal(self))

        @self.tree.command(
            name="conta",
            description="Abre o painel completo da sua conta RP.",
        )
        async def conta(interaction: discord.Interaction) -> None:
            profile = self.database.get_account_profile(interaction.user.id)
            if profile is None:
                await self._reply_text(
                    interaction,
                    title="Conta nao encontrada",
                    description=(
                        "Voce ainda nao possui conta cadastrada. "
                        "Use `/criar_conta` para iniciar o cadastro."
                    ),
                    color=0xB22222,
                    ephemeral=True,
                )
                return

            async def action(inner: discord.Interaction) -> None:
                wallet = self.economy.get_wallet(inner.user.id)
                balance = self.economy.get_balance(inner.user.id)
                total = self.economy.get_total_balance(inner.user.id)
                credit = self.economy.get_credit(inner.user.id)
                embed = self._build_profile_embed(
                    title="🏦 Painel da conta",
                    member=inner.user,
                    profile=profile,
                    wallet=wallet,
                    balance=balance,
                    total=total,
                    credit=credit,
                    color=0x0B4EA2,
                )
                await self._reply_embed(inner, embed, ephemeral=True)

            await self._run_protected(interaction, area="saldo", callback=action)

        @self.tree.command(
            name="notificacoes",
            description="Gerencia suas notificacoes inteligentes por DM.",
        )
        async def notificacoes(interaction: discord.Interaction) -> None:
            async def action(inner: discord.Interaction) -> None:
                await self._reply_embed(
                    inner,
                    self.build_notifications_embed(inner.user.id),
                    view=NotificationsView(self, inner.user.id),
                )

            await self._run_protected(interaction, area="saldo", callback=action)

        @self.tree.command(
            name="investir",
            description="Abre a central de investimentos do Banco Safra.",
        )
        async def investir(interaction: discord.Interaction) -> None:
            async def action(inner: discord.Interaction) -> None:
                await self._reply_embed(
                    inner,
                    self.build_investment_hub_embed(),
                    view=InvestmentHubView(self, inner.user.id),
                )

            await self._run_protected(interaction, area="investimentos", callback=action)

        @self.tree.command(
            name="resgatar",
            description="Resgata um CDB vencido pelo ID.",
        )
        async def resgatar(
            interaction: discord.Interaction,
            investimento_id: int | None = None,
        ) -> None:
            async def action(inner: discord.Interaction) -> None:
                investment = self.investments.redeem_cdb(inner.user.id, investimento_id)
                profit = round(
                    float(investment["valor_atual"]) - float(investment["valor_inicial"]),
                    2,
                )
                embed = make_bank_embed(
                    "\U0001F4E6 Resgate concluido",
                    "Seu CDB foi encerrado e o valor voltou para o saldo do banco.",
                    color=0x1E8E5A,
                )
                embed.add_field(name="ID", value=f"`{investment['id']}`", inline=True)
                embed.add_field(
                    name="Recebido",
                    value=f"**{format_currency(float(investment['valor_atual']))}**",
                    inline=True,
                )
                embed.add_field(
                    name="Lucro",
                    value=f"**{format_currency(profit)}**",
                    inline=True,
                )
                embed.add_field(
                    name="Saldo atual no banco",
                    value=f"**{format_currency(self.economy.get_balance(inner.user.id))}**",
                    inline=False,
                )
                await self._reply_embed(inner, embed)
                if profit >= 0:
                    await enviar_notificacao(
                        self,
                        inner.user.id,
                        "lucro_investimento",
                        f"📈 Seu CDB #{investment['id']} rendeu {format_currency(profit)}.",
                    )
                await self.send_transaction_log(
                    title="📦 Resgate de CDB",
                    lines=[
                        f"Usuario: {inner.user.mention}",
                        f"Investimento ID: `{investment['id']}`",
                        f"Valor recebido: **{format_currency(float(investment['valor_atual']))}**",
                        f"Lucro: **{format_currency(profit)}**",
                    ],
                    color=0x1E8E5A,
                )

            await self._run_protected(interaction, area="investimentos", callback=action)

        @self.tree.command(
            name="investimentos",
            description="Lista seus investimentos ativos com IDs e status.",
        )
        async def investimentos(interaction: discord.Interaction) -> None:
            async def action(inner: discord.Interaction) -> None:
                embed = self._build_investments_overview_embed(inner.user.id)
                await self._reply_embed(inner, embed)

            await self._run_protected(interaction, area="investimentos", callback=action)

        @fundo_group.command(
            name="investir",
            description="Aplica em um fundo de investimento.",
        )
        @app_commands.choices(subtipo=FUND_CHOICES)
        async def fundo_investir(
            interaction: discord.Interaction,
            subtipo: app_commands.Choice[str],
            valor: app_commands.Range[float, 0.01, None],
        ) -> None:
            async def action(inner: discord.Interaction) -> None:
                amount = round(float(valor), 2)
                investment = self.investments.create_fund(
                    inner.user.id,
                    amount,
                    subtipo.value,
                )
                embed = make_bank_embed(
                    "\U0001FA99 Fundo contratado",
                    "Seu dinheiro entrou no fundo e passara por variacoes automaticas.",
                    color=0x8C6B00,
                )
                embed.add_field(name="ID", value=f"`{investment['id']}`", inline=True)
                embed.add_field(name="Perfil", value=subtipo.name, inline=True)
                embed.add_field(
                    name="Valor aplicado",
                    value=f"**{format_currency(float(investment['valor_inicial']))}**",
                    inline=True,
                )
                await self._reply_embed(inner, embed)
                await self.send_transaction_log(
                    title="🪙 Fundo contratado",
                    lines=[
                        f"Usuario: {inner.user.mention}",
                        f"Perfil: **{subtipo.name}**",
                        f"Valor aplicado: **{format_currency(float(investment['valor_inicial']))}**",
                        f"ID do fundo: `{investment['id']}`",
                    ],
                    color=0x8C6B00,
                )
                await self._notify_low_balance_if_needed(inner.user.id)

            await self._run_protected(interaction, area="investimentos", callback=action)

        @fundo_group.command(
            name="sacar",
            description="Saca um fundo pelo ID.",
        )
        async def fundo_sacar(
            interaction: discord.Interaction,
            investimento_id: int | None = None,
        ) -> None:
            async def action(inner: discord.Interaction) -> None:
                fund = self.investments.redeem_fund(inner.user.id, investimento_id)
                delta, percent = self.investments.describe_fund_performance(fund)
                sign = "+" if delta >= 0 else "-"
                embed = make_bank_embed(
                    "\U0001F4E4 Fundo encerrado",
                    "O valor do fundo foi devolvido ao saldo do banco.",
                    color=0x8C6B00,
                )
                embed.add_field(name="ID", value=f"`{fund['id']}`", inline=True)
                embed.add_field(
                    name="Valor resgatado",
                    value=f"**{format_currency(float(fund['valor_atual']))}**",
                    inline=True,
                )
                embed.add_field(
                    name="Resultado",
                    value=f"**{sign}{format_currency(abs(delta))} ({sign}{abs(percent):.2f}%)**",
                    inline=False,
                )
                await self._reply_embed(inner, embed)
                if delta >= 0:
                    await enviar_notificacao(
                        self,
                        inner.user.id,
                        "lucro_investimento",
                        f"📈 Seu fundo #{fund['id']} gerou lucro de {format_currency(delta)}.",
                    )
                else:
                    await enviar_notificacao(
                        self,
                        inner.user.id,
                        "perda_investimento",
                        f"📉 Seu fundo #{fund['id']} registrou perda de {format_currency(abs(delta))}.",
                    )
                await self.send_transaction_log(
                    title="📤 Fundo encerrado",
                    lines=[
                        f"Usuario: {inner.user.mention}",
                        f"Fundo ID: `{fund['id']}`",
                        f"Valor resgatado: **{format_currency(float(fund['valor_atual']))}**",
                        f"Resultado: **{sign}{format_currency(abs(delta))} ({sign}{abs(percent):.2f}%)**",
                    ],
                    color=0x8C6B00,
                )

            await self._run_protected(interaction, area="investimentos", callback=action)

        @fundo_group.command(
            name="status",
            description="Mostra o status dos seus fundos ativos.",
        )
        async def fundo_status(interaction: discord.Interaction) -> None:
            async def action(inner: discord.Interaction) -> None:
                embed = self._build_fund_status_embed(inner.user.id)
                await self._reply_embed(inner, embed)

            await self._run_protected(interaction, area="investimentos", callback=action)

        @self.tree.command(
            name="addmoney",
            description="Admin: adiciona dinheiro em maos para um usuario.",
        )
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def addmoney(
            interaction: discord.Interaction,
            usuario: discord.Member,
            valor: app_commands.Range[float, 0.01, None],
        ) -> None:
            amount = round(float(valor), 2)
            result = self.economy.add_money(usuario.id, amount)
            log_transaction(
                self.settings.log_path,
                (
                    f"ADMIN_ADDMONEY | admin={interaction.user.id} | user={usuario.id} "
                    f"| valor={amount:.2f}"
                ),
            )
            embed = self._build_action_embed(
                title="\U0001F6E0\ufe0f Dinheiro em maos ajustado",
                color=0x137D3E,
                lines=[
                    f"\U0001F464 Usuario: {usuario.mention}",
                    f"\u2795 Valor adicionado: **{format_currency(amount)}**",
                    f"\U0001F4B5 Novo valor em maos: **{format_currency(result.wallet or 0)}**",
                ],
            )
            await self._reply_embed(interaction, embed, ephemeral=True)
            await enviar_notificacao(
                self,
                usuario.id,
                "bonus",
                f"🎁 Voce recebeu um bonus de {format_currency(amount)}.",
            )
            await self.send_transaction_log(
                title="🛠️ AddMoney aplicado",
                lines=[
                    f"Admin: {interaction.user.mention}",
                    f"Usuario: {usuario.mention}",
                    f"Valor: **{format_currency(amount)}**",
                ],
                color=0x137D3E,
            )

        @self.tree.command(
            name="removemoney",
            description="Admin: remove dinheiro em maos de um usuario.",
        )
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def removemoney(
            interaction: discord.Interaction,
            usuario: discord.Member,
            valor: app_commands.Range[float, 0.01, None],
        ) -> None:
            amount = round(float(valor), 2)
            result = self.economy.remove_money(usuario.id, amount)
            log_transaction(
                self.settings.log_path,
                (
                    f"ADMIN_REMOVEMONEY | admin={interaction.user.id} | user={usuario.id} "
                    f"| valor={amount:.2f}"
                ),
            )
            embed = self._build_action_embed(
                title="\U0001F9FE Dinheiro em maos reduzido",
                color=0xB45F06,
                lines=[
                    f"\U0001F464 Usuario: {usuario.mention}",
                    f"\u2796 Valor removido: **{format_currency(amount)}**",
                    f"\U0001F4B5 Novo valor em maos: **{format_currency(result.wallet or 0)}**",
                ],
            )
            await self._reply_embed(interaction, embed, ephemeral=True)
            await self.send_transaction_log(
                title="🧾 RemoveMoney aplicado",
                lines=[
                    f"Admin: {interaction.user.mention}",
                    f"Usuario: {usuario.mention}",
                    f"Valor: **{format_currency(amount)}**",
                ],
                color=0xB45F06,
            )

        @self.tree.command(
            name="addcredito",
            description="Admin: adiciona credito para um usuario.",
        )
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def addcredito(
            interaction: discord.Interaction,
            usuario: discord.Member,
            valor: app_commands.Range[float, 0.01, None],
        ) -> None:
            amount = round(float(valor), 2)
            result = self.economy.add_credit(usuario.id, amount)
            log_transaction(
                self.settings.log_path,
                (
                    f"ADMIN_ADDCREDITO | admin={interaction.user.id} | user={usuario.id} "
                    f"| valor={amount:.2f}"
                ),
            )
            embed = self._build_action_embed(
                title="\U0001F3E6 Credito ajustado",
                color=0x8C6B00,
                lines=[
                    f"\U0001F464 Usuario: {usuario.mention}",
                    f"\u2795 Credito adicionado: **{format_currency(amount)}**",
                    f"\U0001F4B3 Novo credito: **{format_currency(result.credit or 0)}**",
                ],
            )
            await self._reply_embed(interaction, embed, ephemeral=True)
            await enviar_notificacao(
                self,
                usuario.id,
                "credito_atualizado",
                f"💳 Seu credito foi atualizado para {format_currency(result.credit or 0)}.",
            )
            await self.send_transaction_log(
                title="💳 Credito adicionado",
                lines=[
                    f"Admin: {interaction.user.mention}",
                    f"Usuario: {usuario.mention}",
                    f"Valor: **{format_currency(amount)}**",
                    f"Novo credito: **{format_currency(result.credit or 0)}**",
                ],
                color=0x8C6B00,
            )

        @self.tree.command(
            name="removecredito",
            description="Admin: remove credito de um usuario.",
        )
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def removecredito(
            interaction: discord.Interaction,
            usuario: discord.Member,
            valor: app_commands.Range[float, 0.01, None],
        ) -> None:
            amount = round(float(valor), 2)
            result = self.economy.remove_credit(usuario.id, amount)
            log_transaction(
                self.settings.log_path,
                (
                    f"ADMIN_REMOVECREDITO | admin={interaction.user.id} | user={usuario.id} "
                    f"| valor={amount:.2f}"
                ),
            )
            embed = self._build_action_embed(
                title="\U0001F4C9 Credito reduzido",
                color=0x7A4A10,
                lines=[
                    f"\U0001F464 Usuario: {usuario.mention}",
                    f"\u2796 Credito removido: **{format_currency(amount)}**",
                    f"\U0001F4B3 Novo credito: **{format_currency(result.credit or 0)}**",
                ],
            )
            await self._reply_embed(interaction, embed, ephemeral=True)
            await enviar_notificacao(
                self,
                usuario.id,
                "credito_atualizado",
                f"💳 Seu credito foi atualizado para {format_currency(result.credit or 0)}.",
            )
            await self.send_transaction_log(
                title="📉 Credito removido",
                lines=[
                    f"Admin: {interaction.user.mention}",
                    f"Usuario: {usuario.mention}",
                    f"Valor: **{format_currency(amount)}**",
                    f"Novo credito: **{format_currency(result.credit or 0)}**",
                ],
                color=0x7A4A10,
            )

        @self.tree.command(name="ajuda", description="Lista os comandos do Banco Safra BOT.")
        async def ajuda(interaction: discord.Interaction) -> None:
            await self._reply_embed(interaction, self._build_help_embed())

        @gerente_group.command(
            name="conta",
            description="Define ou consulta a conta gerente que recebe perdas e tarifas.",
        )
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def gerente_conta(
            interaction: discord.Interaction,
            usuario: discord.Member | None = None,
        ) -> None:
            if usuario is not None:
                self.database.set_bot_setting("manager_user_id", str(usuario.id))
            manager_id = self.get_manager_id()
            if manager_id is None:
                description = "Nenhuma conta gerente foi configurada ainda."
            else:
                description = f"Conta gerente atual: <@{manager_id}>."
            await self._reply_text(
                interaction,
                title="Conta gerente",
                description=description,
                color=0x0B4EA2,
                ephemeral=True,
            )

        @canal_group.command(
            name="transacoes",
            description="Define ou consulta o canal de transacoes do bot.",
        )
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def canal_transacoes(
            interaction: discord.Interaction,
            canal: discord.TextChannel | None = None,
        ) -> None:
            if canal is not None:
                self.database.set_bot_setting("transactions_channel_id", str(canal.id))
            channel_id = self.get_transactions_channel_id()
            if channel_id is None:
                description = (
                    "Nenhum canal de transacoes foi configurado ainda. "
                    "Use `/canal transacoes #canal`."
                )
            else:
                description = f"Canal de transacoes atual: <#{channel_id}>."
            await self._reply_text(
                interaction,
                title="Canal de transacoes",
                description=description,
                color=0x0B4EA2,
                ephemeral=True,
            )

        @canal_group.command(
            name="contas",
            description="Define ou consulta o canal onde novas contas serao publicadas.",
        )
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def canal_contas(
            interaction: discord.Interaction,
            canal: discord.TextChannel | discord.ForumChannel | None = None,
        ) -> None:
            if canal is not None:
                self.database.set_bot_setting("account_posts_channel_id", str(canal.id))

            channel_id = self.get_account_posts_channel_id()
            if channel_id is None:
                description = (
                    "Nenhum canal de contas foi configurado ainda. "
                    "Use `/canal contas` e escolha um canal de texto ou forum."
                )
            else:
                description = (
                    f"Canal atual de publicacao das contas: <#{channel_id}>."
                )

            await self._reply_text(
                interaction,
                title="Canal de contas",
                description=description,
                color=0x0B4EA2,
                ephemeral=True,
            )

        @definir_group.command(
            name="senha",
            description="Define ou atualiza sua senha por area.",
        )
        @app_commands.choices(area=PASSWORD_AREA_CHOICES)
        async def definir_senha(
            interaction: discord.Interaction,
            area: app_commands.Choice[str],
            senha: str,
        ) -> None:
            self.database.set_user_password(
                interaction.user.id,
                area.value,
                self.hash_password(senha),
            )
            await self._reply_text(
                interaction,
                title="Senha definida",
                description=f"Sua senha da area **{area.name}** foi atualizada com sucesso.",
                color=0x1E8E5A,
                ephemeral=True,
            )

        @consultar_group.command(
            name="saldo",
            description="Admin: consulta o saldo de outro usuario.",
        )
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def consultar_saldo(
            interaction: discord.Interaction,
            usuario: discord.Member,
        ) -> None:
            wallet = self.economy.get_wallet(usuario.id)
            balance = self.economy.get_balance(usuario.id)
            total = self.economy.get_total_balance(usuario.id)
            credit = self.economy.get_credit(usuario.id)
            embed = self._build_account_embed(
                title="\U0001F50E Consulta de saldo",
                member=usuario,
                wallet=wallet,
                balance=balance,
                total=total,
                credit=credit,
                color=0x1F3C88,
            )
            await self._reply_embed(interaction, embed, ephemeral=True)

        @consultar_group.command(
            name="conta",
            description="Admin: consulta o cadastro completo da conta de um usuario.",
        )
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def consultar_conta(
            interaction: discord.Interaction,
            usuario: discord.Member,
        ) -> None:
            profile = self.database.get_account_profile(usuario.id)
            if profile is None:
                await self._reply_text(
                    interaction,
                    title="Conta nao encontrada",
                    description="Esse usuario ainda nao possui uma conta cadastrada.",
                    color=0xB22222,
                    ephemeral=True,
                )
                return

            wallet = self.economy.get_wallet(usuario.id)
            balance = self.economy.get_balance(usuario.id)
            total = self.economy.get_total_balance(usuario.id)
            credit = self.economy.get_credit(usuario.id)
            embed = self._build_profile_embed(
                title="🔎 Consulta de conta",
                member=usuario,
                profile=profile,
                wallet=wallet,
                balance=balance,
                total=total,
                credit=credit,
                color=0x1F3C88,
            )
            await self._reply_embed(interaction, embed, ephemeral=True)

        self.tree.add_command(consultar_group)
        self.tree.add_command(fundo_group)
        self.tree.add_command(canal_group)
        self.tree.add_command(gerente_group)
        self.tree.add_command(definir_group)


def create_bot() -> BancoSafraBot:
    _load_dotenv_file()
    settings = load_settings()
    database = Database(settings.database_path)
    return BancoSafraBot(settings=settings, database=database)


def _load_dotenv_file(env_path: str = ".env") -> None:
    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
