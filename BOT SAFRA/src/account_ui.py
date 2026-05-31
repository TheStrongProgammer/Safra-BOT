from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord

from src.utils import format_currency, format_datetime, make_bank_embed, parse_amount

if TYPE_CHECKING:
    from src.bot import BancoSafraBot


class AccountCreationModal(discord.ui.Modal):
    def __init__(self, bot: "BancoSafraBot") -> None:
        super().__init__(title="Abertura de conta - Banco Safra")
        self.bot = bot

        self.nome_input = discord.ui.TextInput(
            label="Nome completo (RP)",
            placeholder="Ex: Joao Henrique da Silva",
            required=True,
            max_length=80,
        )
        self.senha_input = discord.ui.TextInput(
            label="Senha escolhida (RP)",
            placeholder="Crie uma senha para saldo e investimentos",
            required=True,
            min_length=4,
            max_length=64,
        )
        self.deposito_input = discord.ui.TextInput(
            label="Deposito inicial (minimo R$ 100)",
            placeholder="Ex: 15000",
            required=True,
            max_length=20,
        )
        self.tipo_conta_input = discord.ui.TextInput(
            label="Tipo de conta",
            placeholder="Corrente, Poupanca, Premium, Empresarial...",
            required=True,
            max_length=40,
        )
        self.telefone_input = discord.ui.TextInput(
            label="Telefone ou contato RP",
            placeholder="Ex: (11) 99999-0000",
            required=True,
            max_length=40,
        )

        self.add_item(self.nome_input)
        self.add_item(self.senha_input)
        self.add_item(self.deposito_input)
        self.add_item(self.tipo_conta_input)
        self.add_item(self.telefone_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            if self.bot.database.get_account_profile(interaction.user.id) is not None:
                await self.bot._reply_text(
                    interaction,
                    title="Conta ja existente",
                    description="Voce ja possui uma conta aberta no Banco Safra.",
                    color=0xB22222,
                    ephemeral=True,
                )
                return

            deposito_inicial = parse_amount(str(self.deposito_input.value))
            if deposito_inicial < 100:
                raise ValueError("O deposito inicial minimo para abrir a conta e de R$ 100,00.")

            await self.bot._defer_if_needed(interaction, ephemeral=True)

            now = datetime.now(UTC)
            next_fee_at = now + timedelta(days=7)
            password_hash = self.bot.hash_password(str(self.senha_input.value))

            self.bot.database.create_account_profile(
                user_id=interaction.user.id,
                nome_completo=str(self.nome_input.value).strip(),
                senha_rp_hash=password_hash,
                deposito_inicial=deposito_inicial,
                discord_id=str(interaction.user.id),
                tipo_conta=str(self.tipo_conta_input.value).strip(),
                telefone_rp=str(self.telefone_input.value).strip(),
                profissao_rp="Nao informado",
                created_at=now.isoformat(),
                next_fee_at=next_fee_at.isoformat(),
            )
            self.bot.database.set_user_password(
                interaction.user.id,
                "saldo",
                password_hash,
            )
            self.bot.database.set_user_password(
                interaction.user.id,
                "investimentos",
                password_hash,
            )
            self.bot.database.update_balance(interaction.user.id, deposito_inicial)
            profile = self.bot.database.get_account_profile(interaction.user.id)

            embed = make_bank_embed(
                "🏦 Conta criada com sucesso",
                "Sua conta RP foi aberta e o deposito inicial ja entrou no banco.",
                color=0x1E8E5A,
            )
            embed.add_field(
                name="📛 Nome RP",
                value=str(self.nome_input.value).strip(),
                inline=False,
            )
            embed.add_field(
                name="🪪 ID do Discord",
                value=f"`{interaction.user.id}`",
                inline=True,
            )
            embed.add_field(
                name="💳 Deposito inicial",
                value=f"**{format_currency(deposito_inicial)}**",
                inline=True,
            )
            embed.add_field(
                name="🎯 Tipo de conta",
                value=str(self.tipo_conta_input.value).strip(),
                inline=True,
            )
            embed.add_field(
                name="📱 Contato RP",
                value=str(self.telefone_input.value).strip(),
                inline=True,
            )
            embed.add_field(
                name="📅 Primeira tarifa",
                value=format_datetime(next_fee_at),
                inline=True,
            )
            embed.add_field(
                name="🔐 Areas protegidas",
                value="Saldo e investimentos ja foram vinculados a senha escolhida.",
                inline=False,
            )
            await self.bot._reply_embed(interaction, embed, ephemeral=True)
            await self.bot.send_transaction_log(
                title="🏦 Nova conta criada",
                lines=[
                    f"Cliente: {interaction.user.mention}",
                    f"Nome RP: **{str(self.nome_input.value).strip()}**",
                    f"Tipo de conta: **{str(self.tipo_conta_input.value).strip()}**",
                    f"Deposito inicial: **{format_currency(deposito_inicial)}**",
                ],
                color=0x1E8E5A,
            )
            if profile is not None:
                await self.bot.publish_account_post(
                    member=interaction.user,
                    profile=profile,
                    wallet=self.bot.economy.get_wallet(interaction.user.id),
                    balance=self.bot.economy.get_balance(interaction.user.id),
                    total=self.bot.economy.get_total_balance(interaction.user.id),
                    credit=self.bot.economy.get_credit(interaction.user.id),
                )
        except Exception as exc:
            await self.bot._reply_text(
                interaction,
                title="Abertura nao concluida",
                description=str(exc),
                color=0xB22222,
                ephemeral=True,
            )
