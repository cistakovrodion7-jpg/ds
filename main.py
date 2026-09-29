
import discord
from discord.ext import commands
import asyncio
import re
import json
import os
import sys
import socket
import traceback
from datetime import timedelta, datetime, timezone

# Токен ТОЛЬКО через переменную окружения DISCORD_TOKEN.
# На Bothost: панель бота -> Переменные окружения -> DISCORD_TOKEN = <токен>
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    print("❌ Переменная окружения DISCORD_TOKEN не задана!")
    print("   Добавь её в настройках Bothost (раздел «Переменные окружения»).")
    sys.exit(1)

PREFIX = '//'

intents = discord.Intents.all()
intents.typing = False
intents.presences = False
intents.message_content = True
intents.messages = True

# help_command=None -> отключаем стандартный //help, ниже сделан свой (только для создателя)
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# ID канала для отправки приглашений
INVITE_CHANNEL_ID = 1490756010783281233

# Файлы для хранения данных
REACTIONS_FILE = "reaction_roles.json"
STAFF_FILE = "staff.json"
LOG_CHANNELS_FILE = "log_channels.json"

MAX_WARNS = 3


# ========== ХРАНИЛИЩЕ ==========

def load_json(path):
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def load_reaction_roles():
    return load_json(REACTIONS_FILE)


def save_reaction_roles(data):
    save_json(REACTIONS_FILE, data)


reaction_roles = load_reaction_roles()
staff_data = load_json(STAFF_FILE)

# Структура log_channels:
# {
#   "<guild_id>": {
#       "roles": <channel_id>,              # куда логировать смену ролей
#       "chat_output": <channel_id>,         # куда логировать правки/удаления
#       "chat_watch": [<channel_id>, ...]    # за какими каналами следить
#   }
# }
log_channels = load_json(LOG_CHANNELS_FILE)


def save_log_channels():
    save_json(LOG_CHANNELS_FILE, log_channels)


def get_log(guild_id, key):
    return log_channels.get(str(guild_id), {}).get(key)


def set_log(guild_id, key, channel_id):
    g = log_channels.setdefault(str(guild_id), {})
    g[key] = channel_id
    save_log_channels()


def clear_log(guild_id, key):
    g = log_channels.get(str(guild_id), {})
    if key in g:
        del g[key]
        save_log_channels()
        return True
    return False


def get_watched_channels(guild_id):
    return log_channels.get(str(guild_id), {}).get("chat_watch", [])


def add_watched_channel(guild_id, channel_id):
    g = log_channels.setdefault(str(guild_id), {})
    watch = g.setdefault("chat_watch", [])
    if channel_id not in watch:
        watch.append(channel_id)
        save_log_channels()
        return True
    return False


def remove_watched_channel(guild_id, channel_id):
    g = log_channels.get(str(guild_id), {})
    watch = g.get("chat_watch", [])
    if channel_id in watch:
        watch.remove(channel_id)
        save_log_channels()
        return True
    return False


def gdata(guild_id):
    """Данные сервера: роли стаффа и выговоры."""
    g = staff_data.setdefault(str(guild_id), {})
    g.setdefault("roles", {})
    g.setdefault("warns", {})
    g.setdefault("main_roles", [])
    return g


def save_staff():
    save_json(STAFF_FILE, staff_data)


# ========== РОЛИ / РАНГИ ==========
# 4 - создатель сервера, 3 - зам (admin_zam), 2 - admin2, 1 - admin1, 0 - обычный участник

ROLE_RANK = {"admin1": 1, "admin2": 2, "zam": 3}
RANK_LABEL = {
    4: "👑 Создатель",
    3: "🛡️ Зам (admin_zam)",
    2: "⚔️ Админ 2",
    1: "🔰 Админ 1",
    0: "👤 Участник",
}
MANAGE_RANK = {"zam": 4, "admin2": 3, "admin1": 2}
RANK_WHO = {
    4: "создателю сервера",
    3: "создателю и заму (admin_zam)",
    2: "создателю, заму и admin2",
    1: "администрации (admin1 и выше)",
}


def get_rank(guild, user_id):
    if user_id == guild.owner_id:
        return 4
    role = gdata(guild.id)["roles"].get(str(user_id))
    return ROLE_RANK.get(role, 0)


class NotEnoughRank(commands.CheckFailure):
    def __init__(self, needed):
        self.needed = needed
        super().__init__(f"Нужен ранг {needed}")


def min_rank(n):
    async def predicate(ctx):
        if ctx.guild is None:
            return False
        if get_rank(ctx.guild, ctx.author.id) >= n:
            return True
        raise NotEnoughRank(n)
    return commands.check(predicate)


def is_main_member(g, member):
    ids = set(g["main_roles"])
    return any(r.id in ids for r in member.roles)


def warn_count(g, user_id):
    return len(g["warns"].get(str(user_id), []))


# ========== ЛОГИ (роли + чат) ==========

async def log_role_change(guild, text):
    """Отправить запись в канал логов ролей."""
    chan_id = get_log(guild.id, "roles")
    if not chan_id:
        return
    chan = guild.get_channel(chan_id)
    if not chan:
        return
    em = discord.Embed(
        title="📋 Изменение роли",
        description=text,
        color=0xffd700,
        timestamp=datetime.now(timezone.utc),
    )
    try:
        await chan.send(embed=em)
    except Exception:
        pass


async def log_chat_change(guild, text):
    """Отправить запись в канал логов чата."""
    chan_id = get_log(guild.id, "chat_output")
    if not chan_id:
        return
    chan = guild.get_channel(chan_id)
    if not chan:
        return
    em = discord.Embed(
        title="✏️ История сообщений",
        description=text,
        color=0x00aaff,
        timestamp=datetime.now(timezone.utc),
    )
    try:
        await chan.send(embed=em)
    except Exception:
        pass


async def dm_user(user, title, color, text):
    """Отправить ЛС пользователю."""
    try:
        em = discord.Embed(title=title, description=text, color=color)
        await user.send(embed=em)
    except Exception:
        pass


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def parse_time(time_str):
    match = re.match(r'^(\d+)([mhd])$', time_str.lower())
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    if unit == 'm':
        return timedelta(minutes=value)
    elif unit == 'h':
        return timedelta(hours=value)
    elif unit == 'd':
        return timedelta(days=value)
    return None


def get_member_from_arg(ctx, arg):
    if not arg or ctx.guild is None:
        return None
    arg = arg.strip()
    m = re.fullmatch(r'<@!?(\d+)>', arg)
    if m:
        return ctx.guild.get_member(int(m.group(1)))
    if arg.isdigit():
        return ctx.guild.get_member(int(arg))
    for member in ctx.guild.members:
        if member.name.lower() == arg.lower():
            return member
    return None


async def can_moderate(ctx, target, verb):
    if target.id == ctx.author.id:
        await ctx.send(f"Нельзя {verb} самого себя!")
        return False
    if target.id == ctx.guild.owner_id:
        await ctx.send(f"Нельзя {verb} создателя сервера!")
        return False
    if get_rank(ctx.guild, target.id) >= get_rank(ctx.guild, ctx.author.id):
        await ctx.send(f"❌ У вас нет прав {verb} этого пользователя (его роль не ниже вашей).")
        return False
    return True


async def send_chunks(ctx, title, lines, color=0x00aaff, footer=None):
    if not lines:
        lines = ["— пусто —"]
    chunks, current, size = [], [], 0
    for line in lines:
        if size + len(line) + 1 > 3800 and current:
            chunks.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks, 1):
        t = title if len(chunks) == 1 else f"{title} ({i}/{len(chunks)})"
        embed = discord.Embed(title=t, description="\n".join(chunk), color=color)
        if footer and i == len(chunks):
            embed.set_footer(text=footer)
        await ctx.send(embed=embed)


async def create_and_send_invite(user, guild):
    try:
        invite_channel = bot.get_channel(INVITE_CHANNEL_ID)
        if not invite_channel:
            invite_channel = guild.text_channels[0]

        invite = await invite_channel.create_invite(max_age=3600, max_uses=1, reason=f"Разбан пользователя {user}")

        em = discord.Embed(title="✅ ВЫ РАЗБАНЕНЫ", color=0x00ff00)
        em.add_field(name="📋 Сервер", value=guild.name, inline=False)
        em.add_field(name="🔗 Приглашение", value=f"[Нажмите чтобы зайти]({invite.url})", inline=False)
        em.add_field(name="⏰ Приглашение действительно", value="1 час", inline=False)
        await user.send(embed=em)
        return True
    except Exception:
        return False


@bot.event
async def on_ready():
    print(f'Бот {bot.user} запущен! Префикс: {PREFIX}')


# ========== //help (ТОЛЬКО СОЗДАТЕЛЬ) ==========

@bot.command(name='help')
@min_rank(4)
async def help_cmd(ctx):
    embed = discord.Embed(title="📖 КОМАНДЫ БОТА", color=0x00aaff)
    embed.add_field(name="👑 Только создатель", value=(
        "`//help` — это меню\n"
        "`//admin_zam @user` — назначить зама\n"
        "`//offadmin_zam @user` — снять зама\n"
        "`//istoriaroll` — включить лог смены ролей в этом канале\n"
        "`//chatik <id>` — следить за правками/удалениями в канале <id> (логи — сюда)\n"
        "`//offchatik <id>` — перестать следить за каналом\n"
        "`//chatiklist` — показать отслеживаемые каналы\n"
        "`//stopistori` — выключить лог ролей/чата в этом канале"
    ), inline=False)
    embed.add_field(name="🛡️ Создатель и зам", value=(
        "`//admin2 @user` — назначить admin2\n"
        "`//offadmin2 @user` — снять admin2\n"
        "`//all` — все участники, роли, муты, баны\n"
        "`//alladmin` — вся администрация (admin1, admin2, зам)\n"
        "`//main @роль1 @роль2 ...` — задать «главные» роли (`//main clear` — очистить)\n"
        "`//mainx` — показать главные роли и у кого они есть\n"
        "`//warn @user [причина]` — выдать выговор (макс. 3)\n"
        "`//unwarn @user` — снять один выговор"
    ), inline=False)
    embed.add_field(name="⚔️ Создатель, зам, admin2", value=(
        "`//admin1 @user` — назначить admin1\n"
        "`//offadmin1 @user` — снять admin1"
    ), inline=False)
    embed.add_field(name="🔰 Вся администрация (admin1 и выше)", value=(
        "`//kick @user [причина]`\n"
        "`//ban @user 1m/1h/1d [причина]`\n"
        "`//mute @user 1m/1h/1d [причина]`\n"
        "`//unmute @user`\n"
        "`//clear 1-50`"
    ), inline=False)
    embed.add_field(name="🎭 Реакции-роли (права администратора Discord)", value=(
        "`//addreactrole <message_id> <emoji> @role` — привязать эмодзи к роли\n"
        "`//removereactrole <message_id> <emoji>` — убрать связь\n"
        "`//showreactroles [message_id]` — показать все настройки\n"
        "`//createreactmessage [текст]` — создать сообщение для выбора ролей\n"
        "_В одном сообщении — только одна роль (ставишь вторую реакцию → первая снимается)_"
    ), inline=False)
    await ctx.send(embed=embed)


# ========== НАЗНАЧЕНИЕ / СНЯТИЕ РОЛЕЙ ==========

async def assign_role(ctx, member_arg, role_key, usage):
    if not member_arg:
        await ctx.send(f"Использование: `{usage}`")
        return
    target = get_member_from_arg(ctx, member_arg)
    if not target:
        await ctx.send(f"Пользователь {member_arg} не найден.")
        return
    if target.bot:
        await ctx.send("Ботам роли назначать нельзя.")
        return
    if target.id == ctx.guild.owner_id:
        await ctx.send("Создателю сервера роль не нужна 👑")
        return
    if target.id == ctx.author.id:
        await ctx.send("Нельзя назначить роль самому себе.")
        return

    author_rank = get_rank(ctx.guild, ctx.author.id)
    target_rank = get_rank(ctx.guild, target.id)
    if target_rank >= author_rank:
        await ctx.send("❌ Нельзя менять роль пользователя, чья роль не ниже вашей.")
        return

    g = gdata(ctx.guild.id)
    old = g["roles"].get(str(target.id))
    if old == role_key:
        await ctx.send(f"{target.mention} уже имеет эту роль.")
        return

    g["roles"][str(target.id)] = role_key
    save_staff()
    await ctx.send(f"✅ {target.mention} назначен: **{RANK_LABEL[ROLE_RANK[role_key]]}**")

    old_label = RANK_LABEL[ROLE_RANK[old]] if old in ROLE_RANK else "нет роли"
    new_label = RANK_LABEL[ROLE_RANK[role_key]]
    await log_role_change(ctx.guild,
        f"👤 {target.mention} (`{target.id}`)\n"
        f"📝 {old_label} → **{new_label}**\n"
        f"🛠️ Выдал: {ctx.author.mention}")
    await dm_user(target, "✅ Вам выдана роль", 0x00ff00,
        f"Сервер: **{ctx.guild.name}**\nРоль: **{new_label}**")


async def remove_role(ctx, member_arg, role_key, usage):
    if not member_arg:
        await ctx.send(f"Использование: `{usage}`")
        return
    target = get_member_from_arg(ctx, member_arg)
    if not target:
        await ctx.send(f"Пользователь {member_arg} не найден.")
        return

    g = gdata(ctx.guild.id)
    if g["roles"].get(str(target.id)) != role_key:
        await ctx.send(f"❌ У {target.mention} нет этой роли.")
        return
    if get_rank(ctx.guild, target.id) >= get_rank(ctx.guild, ctx.author.id):
        await ctx.send("❌ Нельзя снять роль у пользователя, чья роль не ниже вашей.")
        return

    old_label = RANK_LABEL[ROLE_RANK[role_key]]
    del g["roles"][str(target.id)]
    g["warns"].pop(str(target.id), None)
    save_staff()
    await ctx.send(f"✅ С {target.mention} снята роль **{old_label}**")

    await log_role_change(ctx.guild,
        f"👤 {target.mention} (`{target.id}`)\n"
        f"📝 **{old_label}** → нет роли\n"
        f"🛠️ Снял: {ctx.author.mention}")
    await dm_user(target, "❌ С вас снята роль", 0xff0000,
        f"Сервер: **{ctx.guild.name}**\nРоль: **{old_label}**")


@bot.command(name='admin_zam')
@min_rank(MANAGE_RANK["zam"])
async def admin_zam(ctx, member=None):
    await assign_role(ctx, member, "zam", "//admin_zam @user")


@bot.command(name='offadmin_zam')
@min_rank(MANAGE_RANK["zam"])
async def offadmin_zam(ctx, member=None):
    await remove_role(ctx, member, "zam", "//offadmin_zam @user")


@bot.command(name='admin2')
@min_rank(MANAGE_RANK["admin2"])
async def admin2(ctx, member=None):
    await assign_role(ctx, member, "admin2", "//admin2 @user")


@bot.command(name='offadmin2')
@min_rank(MANAGE_RANK["admin2"])
async def offadmin2(ctx, member=None):
    await remove_role(ctx, member, "admin2", "//offadmin2 @user")


@bot.command(name='admin1')
@min_rank(MANAGE_RANK["admin1"])
async def admin1(ctx, member=None):
    await assign_role(ctx, member, "admin1", "//admin1 @user")


@bot.command(name='offadmin1')
@min_rank(MANAGE_RANK["admin1"])
async def offadmin1(ctx, member=None):
    await remove_role(ctx, member, "admin1", "//offadmin1 @user")


# ========== КОМАНДЫ ДЛЯ ЛОГОВ (ТОЛЬКО СОЗДАТЕЛЬ) ==========

@bot.command(name='istoriaroll')
@min_rank(4)
async def istoriaroll(ctx):
    """Включить лог смены ролей в этом канале."""
    set_log(ctx.guild.id, "roles", ctx.channel.id)
    await ctx.send(f"✅ Логи смены ролей будут отправляться в {ctx.channel.mention}")


@bot.command(name='chatik')
@min_rank(4)
async def chatik(ctx, channel_id: int = None):
    """//chatik <id канала> — следить за правками/удалениями в этом канале,
    логи присылать туда, где введена команда."""
    if channel_id is None:
        await ctx.send("Использование: `//chatik <id_канала>`\n"
                       "Логи будут приходить в канал, где ты вводишь команду.")
        return

    target = ctx.guild.get_channel(channel_id)
    if target is None:
        await ctx.send(f"❌ Канал с ID `{channel_id}` не найден на этом сервере.")
        return

    set_log(ctx.guild.id, "chat_output", ctx.channel.id)
    added = add_watched_channel(ctx.guild.id, channel_id)

    if added:
        await ctx.send(
            f"✅ Слежу за каналом {target.mention}\n"
            f"📤 Логи правок/удалений будут приходить в {ctx.channel.mention}\n"
            f"⚠️ Убедись, что включён **Message Content Intent** в Developer Portal."
        )
    else:
        await ctx.send(f"⚠️ Канал {target.mention} уже в списке отслеживаемых.\n"
                       f"📤 Логи по-прежнему идут в {ctx.channel.mention}.")


@bot.command(name='offchatik')
@min_rank(4)
async def offchatik(ctx, channel_id: int = None):
    """//offchatik <id канала> — перестать следить за каналом."""
    if channel_id is None:
        await ctx.send("Использование: `//offchatik <id_канала>`")
        return

    if remove_watched_channel(ctx.guild.id, channel_id):
        await ctx.send(f"✅ Канал с ID `{channel_id}` больше не отслеживается.")
    else:
        await ctx.send(f"⚠️ Канал с ID `{channel_id}` не был в списке отслеживаемых.")


@bot.command(name='chatiklist')
@min_rank(4)
async def chatik_list(ctx):
    """Показать отслеживаемые каналы и куда идут логи."""
    watch = get_watched_channels(ctx.guild.id)
    out_id = get_log(ctx.guild.id, "chat_output")
    out_chan = ctx.guild.get_channel(out_id) if out_id else None

    lines = []
    lines.append(f"📤 Канал вывода логов: {out_chan.mention if out_chan else '*не задан*'}")
    lines.append(f"👀 Отслеживаемых каналов: {len(watch)}")
    if not watch:
        lines.append("— никого —")
    for cid in watch:
        ch = ctx.guild.get_channel(cid)
        lines.append(f"• {ch.mention if ch else f'*не найден* (`{cid}`)'}")

    await ctx.send("\n".join(lines))


@bot.command(name='stopistori')
@min_rank(4)
async def stopistori(ctx):
    """Выключить лог в этом канале (роли или чат)."""
    removed = []
    if get_log(ctx.guild.id, "roles") == ctx.channel.id:
        clear_log(ctx.guild.id, "roles")
        removed.append("смена ролей")
    if get_log(ctx.guild.id, "chat_output") == ctx.channel.id:
        clear_log(ctx.guild.id, "chat_output")
        removed.append("правки/удаления сообщений (канал вывода)")

    if removed:
        await ctx.send(f"✅ Отключено в этом канале: {', '.join(removed)}")
    else:
        await ctx.send("⚠️ В этом канале не было включено ни одного лога.")


# ========== СПИСКИ: //all, //alladmin, //main ==========

@bot.command(name='all')
@min_rank(3)
async def all_members(ctx):
    g = gdata(ctx.guild.id)
    members = sorted(
        ctx.guild.members,
        key=lambda m: (-get_rank(ctx.guild, m.id), m.display_name.lower())
    )

    lines = []
    for m in members:
        rank = get_rank(ctx.guild, m.id)
        line = f"{m.mention} — {RANK_LABEL[rank]}"
        if m.bot:
            line += " 🤖"
        if is_main_member(g, m):
            line += " ⭐ главный"
        if 1 <= rank <= 3 or is_main_member(g, m):
            line += f" ⚠️ {warn_count(g, m.id)}/{MAX_WARNS}"
        if m.is_timed_out():
            line += f" 🔇 мут до {discord.utils.format_dt(m.timed_out_until, 'R')}"
        lines.append(line)

    try:
        banned = []
        async for entry in ctx.guild.bans(limit=None):
            banned.append(f"🔨 {entry.user} (`{entry.user.id}`)")
        if banned:
            lines.append("")
            lines.append(f"**Забанены ({len(banned)}):**")
            lines.extend(banned)
    except Exception:
        pass

    await send_chunks(ctx, "👥 ВСЕ УЧАСТНИКИ", lines, footer=f"Участников: {len(members)}")


@bot.command(name='alladmin')
@min_rank(3)
async def all_admin(ctx):
    g = gdata(ctx.guild.id)
    groups = [("zam", "🛡️ Замы (admin_zam)"), ("admin2", "⚔️ Админы 2"), ("admin1", "🔰 Админы 1")]

    lines = []
    for key, title in groups:
        ids = [uid for uid, r in g["roles"].items() if r == key]
        lines.append(f"**{title}** — {len(ids)}")
        if not ids:
            lines.append("— никого —")
        for uid in ids:
            lines.append(f"<@{uid}> ⚠️ выговоры: {warn_count(g, uid)}/{MAX_WARNS}")
        lines.append("")

    await send_chunks(ctx, "🛠️ АДМИНИСТРАЦИЯ", lines, color=0xffa500,
                      footer=f"Выговор: //warn @user [причина] · снять: //unwarn @user (макс. {MAX_WARNS})")


@bot.command(name='main')
@min_rank(3)
async def main_set(ctx, *, arg=None):
    g = gdata(ctx.guild.id)

    if arg and arg.strip().lower() in ("clear", "очистить"):
        g["main_roles"] = []
        save_staff()
        await ctx.send("✅ Список главных ролей очищен.")
        return

    roles = []
    for r in ctx.message.role_mentions:
        if r.id not in [x.id for x in roles]:
            roles.append(r)
    if not roles:
        await ctx.send("Использование: `//main @роль1 @роль2 ...`\nОчистить: `//main clear`\nПосмотреть: `//mainx`")
        return

    g["main_roles"] = [r.id for r in roles]
    save_staff()
    await ctx.send("✅ Главные роли сохранены: " + ", ".join(r.mention for r in roles)
                   + "\nПосмотреть, у кого они есть: `//mainx`",
                   allowed_mentions=discord.AllowedMentions.none())


@bot.command(name='mainx')
@min_rank(3)
async def main_show(ctx):
    g = gdata(ctx.guild.id)
    if not g["main_roles"]:
        await ctx.send("📭 Главные роли не заданы. Задай: `//main @роль1 @роль2 ...`")
        return

    lines = []
    for rid in g["main_roles"]:
        role = ctx.guild.get_role(rid)
        if not role:
            lines.append(f"**Роль не найдена** (ID: {rid})")
            lines.append("")
            continue
        lines.append(f"**{role.mention}** — {len(role.members)}")
        if not role.members:
            lines.append("— никого —")
        for m in role.members:
            line = f"{m.mention} ⚠️ выговоры: {warn_count(g, m.id)}/{MAX_WARNS}"
            if m.is_timed_out():
                line += " 🔇"
            lines.append(line)
        lines.append("")

    await send_chunks(ctx, "🏛️ ГЛАВНЫЕ", lines, color=0xffd700,
                      footer="Выговор: //warn @user [причина] · снять: //unwarn @user")


# ========== ВЫГОВОРЫ ==========

@bot.command(name='warn', aliases=['vygovor', 'выговор'])
@min_rank(3)
async def warn(ctx, member=None, *, reason="Причина не указана"):
    if not member:
        await ctx.send("Использование: `//warn @user [причина]`")
        return
    target = get_member_from_arg(ctx, member)
    if not target:
        await ctx.send(f"Пользователь {member} не найден.")
        return

    target_rank = get_rank(ctx.guild, target.id)
    if target_rank == 0 and not is_main_member(gdata(ctx.guild.id), target):
        await ctx.send("❌ Выговоры выдаются только администрации и «главным» (роли из //main).")
        return
    if target_rank == 4:
        await ctx.send("❌ Создателю выговор выдать нельзя.")
        return
    if target_rank >= get_rank(ctx.guild, ctx.author.id):
        await ctx.send("❌ Нельзя выдать выговор пользователю, чья роль не ниже вашей.")
        return

    g = gdata(ctx.guild.id)
    warns = g["warns"].setdefault(str(target.id), [])
    if len(warns) >= MAX_WARNS:
        await ctx.send(f"❌ У {target.mention} уже {MAX_WARNS}/{MAX_WARNS} выговоров — это максимум.")
        return

    warns.append({
        "reason": reason,
        "by": ctx.author.id,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    save_staff()

    try:
        em = discord.Embed(title="⚠️ ВАМ ВЫДАН ВЫГОВОР", color=0xff0000)
        em.add_field(name="📋 Сервер", value=ctx.guild.name, inline=False)
        em.add_field(name="👤 Выдал", value=ctx.author.mention, inline=False)
        em.add_field(name="📝 Причина", value=reason, inline=False)
        em.add_field(name="📊 Всего", value=f"{len(warns)}/{MAX_WARNS}", inline=False)
        await target.send(embed=em)
    except Exception:
        pass

    msg = f"⚠️ {target.mention} получил выговор ({len(warns)}/{MAX_WARNS}). Причина: {reason}"
    if len(warns) >= MAX_WARNS:
        msg += "\n🚨 Достигнут максимум выговоров!"
    await ctx.send(msg)


@bot.command(name='unwarn', aliases=['unvygovor'])
@min_rank(3)
async def unwarn(ctx, member=None):
    if not member:
        await ctx.send("Использование: `//unwarn @user`")
        return
    target = get_member_from_arg(ctx, member)
    if not target:
        await ctx.send(f"Пользователь {member} не найден.")
        return
    if get_rank(ctx.guild, target.id) >= get_rank(ctx.guild, ctx.author.id):
        await ctx.send("❌ Нельзя снимать выговоры пользователю, чья роль не ниже вашей.")
        return

    g = gdata(ctx.guild.id)
    warns = g["warns"].get(str(target.id), [])
    if not warns:
        await ctx.send(f"У {target.mention} нет выговоров.")
        return
    warns.pop()
    if not warns:
        g["warns"].pop(str(target.id), None)
    save_staff()
    await ctx.send(f"✅ С {target.mention} снят выговор ({len(warns)}/{MAX_WARNS}).")


# ========== КОМАНДЫ ДЛЯ РЕАКЦИЙ-РОЛЕЙ ==========

@bot.command(name='addreactrole')
@commands.has_permissions(administrator=True)
async def add_react_role(ctx, message_id: int, emoji: str, role: discord.Role):
    """Добавить реакцию-роль к сообщению."""
    try:
        message = await ctx.channel.fetch_message(message_id)

        message_id_str = str(message_id)
        if message_id_str not in reaction_roles:
            reaction_roles[message_id_str] = {}

        reaction_roles[message_id_str][emoji] = role.id
        await message.add_reaction(emoji)
        save_reaction_roles(reaction_roles)

        await ctx.send(f"✅ Добавлена реакция {emoji} → {role.mention}")

    except discord.NotFound:
        await ctx.send("❌ Сообщение не найдено!")
    except Exception as e:
        await ctx.send(f"❌ Ошибка: {e}")


@bot.command(name='removereactrole')
@commands.has_permissions(administrator=True)
async def remove_react_role(ctx, message_id: int, emoji: str):
    """Удалить реакцию-роль из сообщения."""
    try:
        message_id_str = str(message_id)

        if message_id_str not in reaction_roles:
            await ctx.send("❌ Для этого сообщения нет настроенных ролей!")
            return

        if emoji not in reaction_roles[message_id_str]:
            await ctx.send(f"❌ Эмодзи {emoji} не найден для этого сообщения!")
            return

        del reaction_roles[message_id_str][emoji]

        if not reaction_roles[message_id_str]:
            del reaction_roles[message_id_str]

        save_reaction_roles(reaction_roles)
        await ctx.send(f"✅ Удалена реакция-роль для эмодзи {emoji}")

    except Exception as e:
        await ctx.send(f"❌ Ошибка: {e}")


@bot.command(name='showreactroles')
async def show_react_roles(ctx, message_id: int = None):
    """Показать все настроенные реакции-роли."""
    if not reaction_roles:
        await ctx.send("📭 Нет настроенных реакций-ролей!")
        return

    if message_id:
        message_id_str = str(message_id)
        if message_id_str not in reaction_roles:
            await ctx.send(f"❌ Для сообщения {message_id} нет настроенных ролей!")
            return

        embed = discord.Embed(
            title=f"📌 РЕАКЦИИ-РОЛИ ДЛЯ СООБЩЕНИЯ {message_id}",
            color=0x00aaff
        )

        for emoji, role_id in reaction_roles[message_id_str].items():
            role = ctx.guild.get_role(role_id)
            role_name = role.mention if role else f"Роль не найдена (ID: {role_id})"
            embed.add_field(name=f"😊 {emoji}", value=role_name, inline=False)

        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(
            title="📌 ВСЕ НАСТРОЕННЫЕ РЕАКЦИИ-РОЛИ",
            color=0x00aaff
        )

        for message_id_str, roles in reaction_roles.items():
            role_list = []
            for emoji, role_id in roles.items():
                role = ctx.guild.get_role(role_id)
                role_name = role.name if role else f"Не найдена (ID: {role_id})"
                role_list.append(f"{emoji} → {role_name}")

            embed.add_field(
                name=f"📝 Сообщение ID: {message_id_str}",
                value="\n".join(role_list) if role_list else "Нет ролей",
                inline=False
            )

        await ctx.send(embed=embed)


@bot.command(name='createreactmessage')
@commands.has_permissions(administrator=True)
async def create_react_message(ctx, *, text=None):
    """Создать новое сообщение для реакций-ролей."""
    if not text:
        text = ("Нажмите на эмодзи под этим сообщением, чтобы получить роль!\n\n"
                "*Поставить вторую реакцию — первая роль снимется автоматически.*")

    embed = discord.Embed(
        title="🎭 ПОЛУЧЕНИЕ РОЛЕЙ",
        description=text,
        color=0x00ff00
    )
    embed.set_footer(text="Используйте //addreactrole чтобы добавить реакции")

    message = await ctx.send(embed=embed)
    await ctx.send(f"✅ Сообщение создано! ID: `{message.id}`\nПример: `//addreactrole {message.id} 🎮 @роль`")


# ========== ОБРАБОТЧИКИ РЕАКЦИЙ ==========

@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    message_id_str = str(payload.message_id)
    if message_id_str not in reaction_roles:
        return

    emoji = str(payload.emoji)
    if emoji not in reaction_roles[message_id_str]:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    member = guild.get_member(payload.user_id)
    if not member:
        return

    role_id = reaction_roles[message_id_str][emoji]
    role = guild.get_role(role_id)
    if not role:
        return

    try:
        await member.add_roles(role)
    except Exception:
        return

    await log_role_change(guild,
        f"🎭 {member.mention} (`{member.id}`) взял роль **{role.name}** (реакция {emoji})")
    await dm_user(member, "🎭 Вы взяли роль", 0x00ff00,
        f"Сервер: **{guild.name}**\nРоль: **{role.name}**")

    # Убираем другие реакции этой же группы с сообщения у пользователя
    other_emojis = [e for e in reaction_roles[message_id_str] if e != emoji]
    if other_emojis:
        try:
            channel = guild.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            message = None

        for other_emoji in other_emojis:
            other_role_id = reaction_roles[message_id_str][other_emoji]
            other_role = guild.get_role(other_role_id)

            if other_role and other_role in member.roles:
                try:
                    await member.remove_roles(other_role)
                    await log_role_change(guild,
                        f"🎭 {member.mention} (`{member.id}`) потерял роль **{other_role.name}** "
                        f"(переключился на {emoji})")
                    await dm_user(member, "🎭 С вас снята роль", 0xffa500,
                        f"Сервер: **{guild.name}**\nРоль: **{other_role.name}**\n"
                        f"Причина: переключение на другую реакцию")
                except Exception:
                    pass

            if message:
                try:
                    await message.remove_reaction(other_emoji, member)
                except Exception:
                    pass


@bot.event
async def on_raw_reaction_remove(payload):
    message_id_str = str(payload.message_id)
    if message_id_str not in reaction_roles:
        return

    emoji = str(payload.emoji)
    if emoji not in reaction_roles[message_id_str]:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    member = guild.get_member(payload.user_id)
    if not member:
        return

    role_id = reaction_roles[message_id_str][emoji]
    role = guild.get_role(role_id)
    if not role:
        return

    try:
        await member.remove_roles(role)
    except Exception:
        return

    await log_role_change(guild,
        f"🎭 {member.mention} (`{member.id}`) снял роль **{role.name}** (убрал реакцию {emoji})")
    await dm_user(member, "🎭 С вас снята роль", 0xffa500,
        f"Сервер: **{guild.name}**\nРоль: **{role.name}**\n"
        f"Причина: вы убрали реакцию")


# ========== ЛОГ ПРАВОК И УДАЛЕНИЙ СООБЩЕНИЙ ==========

@bot.event
async def on_message_edit(before, after):
    if before.author.bot:
        return
    if before.guild is None:
        return

    # Логируем только если этот канал в списке отслеживаемых
    if before.channel.id not in get_watched_channels(before.guild.id):
        return

    # Не логируем сам канал логов, чтобы не зацикливаться
    if get_log(before.guild.id, "chat_output") == before.channel.id:
        return

    # Только если реально изменился контент
    if before.content == after.content:
        return

    old = before.content or "*[нет текста]*"
    new = after.content or "*[нет текста]*"
    if len(old) > 500:
        old = old[:500] + "…"
    if len(new) > 500:
        new = new[:500] + "…"

    await log_chat_change(before.guild,
        f"✏️ **{before.author.mention}** (`{before.author.id}`) изменил сообщение\n"
        f"📌 Канал: {before.channel.mention}\n"
        f"🔗 [Перейти к сообщению]({after.jump_url})\n"
        f"**Было:**\n```{old}```\n"
        f"**Стало:**\n```{new}```")


@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return
    if message.guild is None:
        return

    if message.channel.id not in get_watched_channels(message.guild.id):
        return

    if get_log(message.guild.id, "chat_output") == message.channel.id:
        return

    content = message.content or "*[нет текста / только вложение]*"
    if len(content) > 500:
        content = content[:500] + "…"

    attach = ""
    if message.attachments:
        attach = f"\n📎 Вложений: {len(message.attachments)}"

    await log_chat_change(message.guild,
        f"🗑️ **{message.author.mention}** (`{message.author.id}`) удалил сообщение\n"
        f"📌 Канал: {message.channel.mention}\n"
        f"**Содержимое:**\n```{content}```{attach}")


# ========== МОДЕРАЦИЯ ==========

@bot.command(name='kick')
@min_rank(1)
async def kick(ctx, member=None, *, reason="Причина не указана"):
    if not member:
        await ctx.send("Использование: //kick @user [причина]")
        return
    target = get_member_from_arg(ctx, member)
    if not target:
        await ctx.send(f"Пользователь {member} не найден.")
        return
    if not await can_moderate(ctx, target, "кикнуть"):
        return
    try:
        await target.kick(reason=reason)
        await ctx.send(f"{target.mention} был кикнут. Причина: {reason}")
    except Exception as e:
        await ctx.send(f"Ошибка: {e}")


@bot.command(name='ban')
@min_rank(1)
async def ban(ctx, member=None, duration=None, *, reason="Причина не указана"):
    if not member or not duration:
        await ctx.send("Использование: //ban @user 1h/d/m [причина]")
        return
    target = get_member_from_arg(ctx, member)
    if not target:
        await ctx.send(f"Пользователь {member} не найден.")
        return
    if not await can_moderate(ctx, target, "забанить"):
        return
    time_delta = parse_time(duration)
    if not time_delta:
        await ctx.send("Неверный формат времени. Используйте: 1m, 2h, 3d")
        return
    max_duration = timedelta(days=28)
    if time_delta > max_duration:
        time_delta = max_duration
        await ctx.send("Время бана сокращено до 28 дней.")

    try:
        em = discord.Embed(title="🔨 ВАС ЗАБАНИЛИ", color=0xff0000)
        em.add_field(name="📋 Сервер", value=ctx.guild.name, inline=False)
        em.add_field(name="👤 Выдал", value=ctx.author.mention, inline=False)
        em.add_field(name="📝 Причина", value=reason, inline=False)
        em.add_field(name="⏱️ Время", value=duration, inline=False)
        await target.send(embed=em)
    except Exception:
        pass

    try:
        ban_reason = f"{reason} | Длительность: {duration} | Модератор: {ctx.author}"
        await target.ban(reason=ban_reason)

        guild = ctx.guild

        async def auto_unban():
            await asyncio.sleep(time_delta.total_seconds())
            try:
                await guild.unban(target)
                await create_and_send_invite(target, guild)
            except Exception:
                pass

        bot.loop.create_task(auto_unban())

        await ctx.send(f"{target.mention} забанен на {duration}. Причина: {reason}")
    except Exception as e:
        await ctx.send(f"Ошибка: {e}")


@bot.command(name='mute')
@min_rank(1)
async def mute(ctx, member=None, duration=None, *, reason="Нарушение правил"):
    if not member or not duration:
        await ctx.send("Использование: //mute @user 1h/d/m [причина]")
        return
    target = get_member_from_arg(ctx, member)
    if not target:
        await ctx.send(f"Пользователь {member} не найден.")
        return
    if not await can_moderate(ctx, target, "замутить"):
        return
    time_delta = parse_time(duration)
    if not time_delta:
        await ctx.send("Неверный формат времени. Используйте: 1m, 2h, 3d")
        return
    max_duration = timedelta(days=28)
    if time_delta > max_duration:
        time_delta = max_duration
        await ctx.send("Время мута сокращено до 28 дней.")

    try:
        em = discord.Embed(title="🔇 ВАС ЗАМУТИЛИ", color=0xffa500)
        em.add_field(name="📋 Сервер", value=ctx.guild.name, inline=False)
        em.add_field(name="👤 Выдал", value=ctx.author.mention, inline=False)
        em.add_field(name="📝 Причина", value=reason, inline=False)
        em.add_field(name="⏱️ Время", value=duration, inline=False)
        await target.send(embed=em)
    except Exception:
        pass

    try:
        await target.timeout(time_delta, reason=reason)
        await ctx.send(f"{target.mention} получил мут на {duration}. Причина: {reason}")
    except Exception as e:
        await ctx.send(f"Ошибка: {e}")


@bot.command(name='unmute')
@min_rank(1)
async def unmute(ctx, member=None):
    if not member:
        await ctx.send("Использование: //unmute @user")
        return

    target = get_member_from_arg(ctx, member)
    if not target:
        await ctx.send(f"Пользователь {member} не найден.")
        return

    try:
        await target.timeout(None)

        try:
            em = discord.Embed(title="✅ ВАС РАЗМУТИЛИ", color=0x00ff00)
            em.add_field(name="📋 Сервер", value=ctx.guild.name, inline=False)
            em.add_field(name="👤 Размутил", value=ctx.author.mention, inline=False)
            await target.send(embed=em)
        except Exception:
            pass

        await ctx.send(f"{target.mention} размучен")
    except Exception as e:
        await ctx.send(f"Ошибка: {e}")


@bot.command(name='clear')
@min_rank(1)
async def clear(ctx, amount=None):
    if amount is None:
        await ctx.send("Использование: //clear 1-50")
        return
    try:
        amount = int(amount)
    except ValueError:
        await ctx.send("Количество должно быть числом.")
        return
    if amount < 1 or amount > 50:
        await ctx.send("Можно удалить 1-50 сообщений.")
        return
    deleted = await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"Удалено {max(len(deleted) - 1, 0)} сообщений.", delete_after=5)


# ========== ОБРАБОТКА ОШИБОК ==========

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, NotEnoughRank):
        await ctx.send(f"❌ Эта команда доступна только {RANK_WHO.get(error.needed, 'администрации')}.", delete_after=8)
    elif isinstance(error, commands.CheckFailure) and not isinstance(error, commands.MissingPermissions):
        return
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Недостаточно прав.", delete_after=5)
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Не хватает аргументов!\nИспользование: `{ctx.prefix}{ctx.command.name} {ctx.command.signature}`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Неверный аргумент. Проверь, что ввёл всё правильно.")
    else:
        traceback.print_exception(type(error), error, error.__traceback__)
        original = getattr(error, "original", error)
        await ctx.send(f"❌ Ошибка: {original}")


# ========== ЗАПУСК ==========

if __name__ == "__main__":
    _lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _lock.bind(("127.0.0.1", 47653))
    except OSError:
        print("❌ Бот уже запущен в другом окне! Закрой старый процесс и запусти снова.")
        sys.exit(1)

    bot.run(TOKEN)