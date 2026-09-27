"""
Monthly giveaway leaderboard bot.
Points for chatting, daily check-ins, reactions, and invites, with anti-farming
checks. Posts a live leaderboard, closes each month automatically, and awards a
separate prize to the top VIP.
"""
import os
import re
import difflib
import datetime as dt
from collections import defaultdict, deque
from zoneinfo import ZoneInfo

import asyncpg
import discord
from discord import app_commands
from discord.ext import tasks


# ---------- Config (set these as Railway variables) ----------
def env_int(name, default=0):
    v = os.getenv(name)
    return int(v) if v else default


TOKEN = os.environ["DISCORD_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
GUILD_ID = int(os.environ["GUILD_ID"])
VIP_ROLE_ID = env_int("VIP_ROLE_ID")
CHAMPION_ROLE_ID = env_int("CHAMPION_ROLE_ID")
BOARD_CHANNEL_ID = env_int("LEADERBOARD_CHANNEL_ID")
WINNERS_CHANNEL_ID = env_int("WINNERS_CHANNEL_ID")
STAFF_CHANNEL_ID = env_int("STAFF_CHANNEL_ID")
ALLOWED_CHANNELS = {int(x) for x in os.getenv("ALLOWED_CHANNEL_IDS", "").split(",") if x.strip()}
TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Chicago"))
PRIZE = os.getenv("PRIZE_TEXT", "[Set PRIZE_TEXT]")
VIP_PRIZE = os.getenv("VIP_PRIZE_TEXT", "[Set VIP_PRIZE_TEXT]")

# ---------- Point rules ----------
MSG_POINTS = 1
MSG_COOLDOWN_SEC = 60
MSG_MIN_CHARS = 5
MSG_DAILY_CAP = 50
MSG_SIMILARITY = 0.85          # how close counts as a repeat

DAILY_POINTS = 10
STREAK_BONUS = 25
STREAK_LEN = 7

REACT_POINTS = 1
REACT_DAILY_CAP = 20
REACTOR_MIN_DAYS = 7           # reactor must have been in the server this long

INVITE_POINTS = 50
INVITE_STAY_DAYS = 7
INVITE_ACCOUNT_MIN_DAYS = 30   # invited account must be this old
INVITE_MIN_MSGS = 3
INVITE_EXPIRE_DAYS = 14

REVIEW_REACTOR_SHARE = 0.40    # flag if one person gave 40%+ of someone's reactions

SCHEMA = """
CREATE TABLE IF NOT EXISTS points (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    month TEXT NOT NULL,
    amount INT NOT NULL,
    reason TEXT NOT NULL,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS points_month_idx ON points (guild_id, month, user_id);
CREATE TABLE IF NOT EXISTS user_state (
    guild_id BIGINT, user_id BIGINT,
    msg_count INT NOT NULL DEFAULT 0,
    daily_last DATE, streak INT NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS reaction_awards (
    guild_id BIGINT, message_id BIGINT, reactor_id BIGINT,
    receiver_id BIGINT NOT NULL, month TEXT NOT NULL,
    PRIMARY KEY (guild_id, message_id, reactor_id)
);
CREATE TABLE IF NOT EXISTS invites (
    guild_id BIGINT, invitee_id BIGINT,
    inviter_id BIGINT NOT NULL,
    joined_at TIMESTAMPTZ NOT NULL, account_created TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL, awarded_month TEXT,
    PRIMARY KEY (guild_id, invitee_id)
);
CREATE TABLE IF NOT EXISTS disqualified (
    guild_id BIGINT, user_id BIGINT, month TEXT,
    PRIMARY KEY (guild_id, user_id, month)
);
CREATE TABLE IF NOT EXISTS winners (
    guild_id BIGINT, month TEXT, kind TEXT, place INT,
    user_id BIGINT, points INT,
    PRIMARY KEY (guild_id, month, kind, place)
);
CREATE TABLE IF NOT EXISTS settings (
    guild_id BIGINT, key TEXT, value TEXT,
    PRIMARY KEY (guild_id, key)
);
"""


# ---------- Helpers ----------
def now():
    return dt.datetime.now(TZ)


def month_key(d=None):
    return (d or now()).strftime("%Y-%m")


def day_start():
    return now().replace(hour=0, minute=0, second=0, microsecond=0)


def next_month_start():
    first = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first + dt.timedelta(days=32)).replace(day=1)


URL_RE = re.compile(r"https?://\S+")
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
MENTION_RE = re.compile(r"<[@#][!&]?\d+>")
REPEAT_RE = re.compile(r"(.)\1{2,}")


def clean_text(text):
    """Strip links, emojis, mentions and stretched letters. Returns (normalized, real_char_count)."""
    t = URL_RE.sub("", text)
    t = CUSTOM_EMOJI_RE.sub("", t)
    t = MENTION_RE.sub("", t)
    t = REPEAT_RE.sub(r"\1", t.lower())
    normalized = "".join(c for c in t if c.isalnum() or c == " ").strip()
    return normalized, sum(c.isalnum() for c in normalized)


def is_vip(guild, user_id):
    m = guild.get_member(user_id)
    return bool(m and VIP_ROLE_ID and any(r.id == VIP_ROLE_ID for r in m.roles))


def display(guild, user_id):
    m = guild.get_member(user_id)
    return m.display_name if m else f"User {user_id}"


# ---------- Bot ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class GiveawayBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.pool = None

    async def setup_hook(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        async with self.pool.acquire() as con:
            await con.execute(SCHEMA)
        self.add_view(BoardView())
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        ticker.start()


bot = GiveawayBot()
last_counted = {}                               # user_id -> datetime of last counted message
recent_msgs = defaultdict(lambda: deque(maxlen=5))
invite_cache = {}                               # code -> uses


async def award(user_id, amount, reason, note=None, month=None):
    await bot.pool.execute(
        "INSERT INTO points (guild_id, user_id, month, amount, reason, note) VALUES ($1,$2,$3,$4,$5,$6)",
        GUILD_ID, user_id, month or month_key(), amount, reason, note,
    )


async def points_today(user_id, reason):
    return await bot.pool.fetchval(
        "SELECT COALESCE(SUM(amount),0) FROM points WHERE guild_id=$1 AND user_id=$2 AND reason=$3 AND created_at >= $4",
        GUILD_ID, user_id, reason, day_start(),
    )


async def get_setting(key):
    return await bot.pool.fetchval("SELECT value FROM settings WHERE guild_id=$1 AND key=$2", GUILD_ID, key)


async def set_setting(key, value):
    await bot.pool.execute(
        "INSERT INTO settings VALUES ($1,$2,$3) ON CONFLICT (guild_id, key) DO UPDATE SET value=EXCLUDED.value",
        GUILD_ID, key, str(value),
    )


async def top(month, limit=10):
    return await bot.pool.fetch(
        """SELECT user_id, SUM(amount)::int AS pts FROM points
           WHERE guild_id=$1 AND month=$2
             AND user_id NOT IN (SELECT user_id FROM disqualified WHERE guild_id=$1 AND month=$2)
           GROUP BY user_id HAVING SUM(amount) > 0
           ORDER BY pts DESC, MAX(created_at) ASC LIMIT $3""",
        GUILD_ID, month, limit,
    )


# ---------- Earning: messages ----------
@bot.event
async def on_message(msg):
    if msg.author.bot or not msg.guild or msg.guild.id != GUILD_ID:
        return
    if ALLOWED_CHANNELS and msg.channel.id not in ALLOWED_CHANNELS:
        return
    uid = msg.author.id
    normalized, real_chars = clean_text(msg.content)
    if real_chars < MSG_MIN_CHARS:
        return
    is_repeat = any(difflib.SequenceMatcher(None, normalized, old).ratio() >= MSG_SIMILARITY
                    for old in recent_msgs[uid])
    recent_msgs[uid].append(normalized)
    if is_repeat:
        return
    t = now()
    last = last_counted.get(uid)
    if last and (t - last).total_seconds() < MSG_COOLDOWN_SEC:
        return
    if await points_today(uid, "message") >= MSG_DAILY_CAP:
        return
    last_counted[uid] = t
    await award(uid, MSG_POINTS, "message")
    await bot.pool.execute(
        """INSERT INTO user_state (guild_id, user_id, msg_count) VALUES ($1,$2,1)
           ON CONFLICT (guild_id, user_id) DO UPDATE SET msg_count = user_state.msg_count + 1""",
        GUILD_ID, uid,
    )


# ---------- Earning: reactions ----------
@bot.event
async def on_raw_reaction_add(p):
    if p.guild_id != GUILD_ID or p.member is None or p.member.bot:
        return
    if ALLOWED_CHANNELS and p.channel_id not in ALLOWED_CHANNELS:
        return
    author_id = getattr(p, "message_author_id", None)
    if author_id is None:
        try:
            m = await bot.get_channel(p.channel_id).fetch_message(p.message_id)
            author_id = m.author.id
        except Exception:
            return
    if author_id == p.user_id:
        return
    guild = bot.get_guild(GUILD_ID)
    author = guild.get_member(author_id)
    if author is None or author.bot:
        return
    if not p.member.joined_at or (discord.utils.utcnow() - p.member.joined_at).days < REACTOR_MIN_DAYS:
        return
    inserted = await bot.pool.fetchval(
        """INSERT INTO reaction_awards (guild_id, message_id, reactor_id, receiver_id, month)
           VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING RETURNING 1""",
        GUILD_ID, p.message_id, p.user_id, author_id, month_key(),
    )
    if not inserted:
        return   # this person already gave a point on this message
    if await points_today(author_id, "reaction") >= REACT_DAILY_CAP:
        return
    await award(author_id, REACT_POINTS, "reaction")


# ---------- Earning: invites ----------
async def refresh_invite_cache(guild):
    try:
        invs = await guild.invites()
    except discord.Forbidden:
        print("Missing Manage Server permission: invite tracking is off.")
        return
    invite_cache.clear()
    invite_cache.update({i.code: (i.uses or 0) for i in invs})


@bot.event
async def on_ready():
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await refresh_invite_cache(guild)
    print(f"Logged in as {bot.user}")


@bot.event
async def on_invite_create(invite):
    if invite.guild and invite.guild.id == GUILD_ID:
        invite_cache[invite.code] = invite.uses or 0


@bot.event
async def on_invite_delete(invite):
    invite_cache.pop(invite.code, None)


@bot.event
async def on_member_join(member):
    if member.guild.id != GUILD_ID or member.bot:
        return
    before = dict(invite_cache)
    try:
        invs = await member.guild.invites()
    except discord.Forbidden:
        return
    inviter_id = None
    for i in invs:
        if (i.uses or 0) > before.get(i.code, 0) and i.inviter:
            inviter_id = i.inviter.id
            break
    invite_cache.clear()
    invite_cache.update({i.code: (i.uses or 0) for i in invs})
    if inviter_id is None or inviter_id == member.id:
        return
    # ON CONFLICT DO NOTHING: leaving and rejoining never counts twice
    await bot.pool.execute(
        """INSERT INTO invites (guild_id, invitee_id, inviter_id, joined_at, account_created, status)
           VALUES ($1,$2,$3,$4,$5,'pending') ON CONFLICT DO NOTHING""",
        GUILD_ID, member.id, inviter_id, member.joined_at or discord.utils.utcnow(), member.created_at,
    )


@bot.event
async def on_member_remove(member):
    if member.guild.id != GUILD_ID:
        return
    row = await bot.pool.fetchrow(
        "SELECT * FROM invites WHERE guild_id=$1 AND invitee_id=$2", GUILD_ID, member.id)
    if not row:
        return
    if row["status"] == "pending":
        await bot.pool.execute(
            "UPDATE invites SET status='rejected' WHERE guild_id=$1 AND invitee_id=$2", GUILD_ID, member.id)
    elif row["status"] == "awarded" and row["awarded_month"] == month_key():
        await award(row["inviter_id"], -INVITE_POINTS, "invite_revoked", note=str(member.id))
        await bot.pool.execute(
            "UPDATE invites SET status='revoked' WHERE guild_id=$1 AND invitee_id=$2", GUILD_ID, member.id)


async def check_pending_invites(guild):
    utc = discord.utils.utcnow()
    rows = await bot.pool.fetch(
        "SELECT * FROM invites WHERE guild_id=$1 AND status='pending' AND joined_at <= $2",
        GUILD_ID, utc - dt.timedelta(days=INVITE_STAY_DAYS),
    )
    for r in rows:
        new_status = None
        if guild.get_member(r["invitee_id"]) is None:
            new_status = "rejected"
        elif (r["joined_at"] - r["account_created"]).days < INVITE_ACCOUNT_MIN_DAYS:
            new_status = "rejected"   # likely an alt account
        else:
            msgs = await bot.pool.fetchval(
                "SELECT msg_count FROM user_state WHERE guild_id=$1 AND user_id=$2", GUILD_ID, r["invitee_id"])
            if (msgs or 0) >= INVITE_MIN_MSGS:
                await award(r["inviter_id"], INVITE_POINTS, "invite", note=str(r["invitee_id"]))
                await bot.pool.execute(
                    "UPDATE invites SET status='awarded', awarded_month=$3 WHERE guild_id=$1 AND invitee_id=$2",
                    GUILD_ID, r["invitee_id"], month_key())
                continue
            if r["joined_at"] <= utc - dt.timedelta(days=INVITE_EXPIRE_DAYS):
                new_status = "rejected"
        if new_status:
            await bot.pool.execute(
                "UPDATE invites SET status=$3 WHERE guild_id=$1 AND invitee_id=$2",
                GUILD_ID, r["invitee_id"], new_status)


# ---------- Embeds ----------
MEDALS = ["🥇", "🥈", "🥉"]


async def current_champion():
    """Last closed month's #1, or None."""
    return await bot.pool.fetchrow(
        """SELECT user_id, month, points FROM winners
           WHERE guild_id=$1 AND kind='main' AND place=1 ORDER BY month DESC LIMIT 1""", GUILD_ID)


async def crown_champion(guild, user_id):
    """Move the Monthly Champion role to the new winner."""
    role = guild.get_role(CHAMPION_ROLE_ID) if CHAMPION_ROLE_ID else None
    if role is None:
        return
    for m in list(role.members):
        if m.id != user_id:
            await m.remove_roles(role, reason="New Monthly Champion crowned")
    winner = guild.get_member(user_id)
    if winner and role not in winner.roles:
        await winner.add_roles(role, reason="Monthly Champion")


async def board_embed(guild):
    rows = await top(month_key(), 10)
    champ = await current_champion()
    lines = []
    if champ:
        champ_month = dt.datetime.strptime(champ["month"], "%Y-%m").strftime("%B")
        lines.append(f"🏆 **Monthly Champion:** {display(guild, champ['user_id'])} "
                     f"— won {champ_month} with {champ['points']:,} pts\n")
    for i, r in enumerate(rows):
        rank = MEDALS[i] if i < 3 else f"`#{i + 1}`"
        name = display(guild, r["user_id"])
        if champ and r["user_id"] == champ["user_id"]:
            name = f"🏆 {name}"
        if is_vip(guild, r["user_id"]):
            lines.append(f"{rank} 👑 **{name}** — `{r['pts']:,} pts`")
        else:
            lines.append(f"{rank} {name} — `{r['pts']:,} pts`")
    e = discord.Embed(
        title=f"🏆 {now().strftime('%B')} leaderboard",
        description="\n".join(lines) if rows else "\n".join(lines + ["No points yet this month. Start chatting!"]),
        color=0xF0B232,
    )
    e.add_field(name="Prize", value=PRIZE, inline=True)
    e.add_field(name="Top VIP bonus", value=VIP_PRIZE, inline=True)
    e.add_field(name="Ends", value=discord.utils.format_dt(next_month_start(), "R"), inline=True)
    e.set_footer(text="🏆 = Monthly Champion · 👑 = VIP member · Updates every 10 minutes")
    return e


def earn_embed():
    e = discord.Embed(title="How to earn points", color=0x5865F2)
    e.description = (
        f"💬 Send a message (5+ characters, once per minute) — **+{MSG_POINTS}**\n"
        f"📅 Daily check-in with `/daily` — **+{DAILY_POINTS}**\n"
        f"🔥 {STREAK_LEN}-day check-in streak — **+{STREAK_BONUS}**\n"
        f"⭐ Reaction on your message (max {REACT_DAILY_CAP}/day) — **+{REACT_POINTS}**\n"
        f"📨 Invite someone who stays {INVITE_STAY_DAYS} days and chats — **+{INVITE_POINTS}**\n\n"
        f"Message points cap at {MSG_DAILY_CAP}/day. Spam, repeats, self-reactions and alt accounts "
        "don't count, and farming gets you disqualified for the month.\n"
        "Everyone earns the same points. The top VIP also wins the VIP bonus prize."
    )
    return e


async def stats_embed(guild, user):
    mk = month_key()
    rows = await bot.pool.fetch(
        "SELECT reason, SUM(amount)::int AS s FROM points WHERE guild_id=$1 AND user_id=$2 AND month=$3 GROUP BY reason",
        GUILD_ID, user.id, mk)
    by_reason = {r["reason"]: r["s"] for r in rows}
    total = sum(by_reason.values())
    dq = await bot.pool.fetchval(
        "SELECT 1 FROM disqualified WHERE guild_id=$1 AND user_id=$2 AND month=$3", GUILD_ID, user.id, mk)
    if dq:
        rank_text = "Disqualified this month"
    else:
        rank = await bot.pool.fetchval(
            """SELECT COUNT(*) + 1 FROM (
                 SELECT user_id, SUM(amount) AS s FROM points
                 WHERE guild_id=$1 AND month=$2
                   AND user_id NOT IN (SELECT user_id FROM disqualified WHERE guild_id=$1 AND month=$2)
                 GROUP BY user_id) t WHERE s > $3""",
            GUILD_ID, mk, total)
        rank_text = f"#{rank}"
    streak = await bot.pool.fetchval(
        "SELECT streak FROM user_state WHERE guild_id=$1 AND user_id=$2", GUILD_ID, user.id) or 0
    today_msgs = await points_today(user.id, "message")
    e = discord.Embed(title=f"Your stats for {now().strftime('%B')}", color=0xF0B232)
    e.add_field(name="Points", value=f"{total:,}", inline=True)
    e.add_field(name="Rank", value=rank_text, inline=True)
    e.add_field(name="Check-in streak", value=f"{streak} days", inline=True)
    e.add_field(
        name="Breakdown",
        value=(f"Messages: {by_reason.get('message', 0)}\n"
               f"Check-ins: {by_reason.get('daily', 0) + by_reason.get('streak', 0)}\n"
               f"Reactions: {by_reason.get('reaction', 0)}\n"
               f"Invites: {by_reason.get('invite', 0) + by_reason.get('invite_revoked', 0)}"),
        inline=False)
    e.set_footer(text=f"Message points today: {today_msgs}/{MSG_DAILY_CAP}")
    return e


async def winners_embed(guild):
    rows = await bot.pool.fetch(
        """SELECT * FROM winners WHERE guild_id=$1
           AND month IN (SELECT DISTINCT month FROM winners WHERE guild_id=$1 ORDER BY month DESC LIMIT 3)
           ORDER BY month DESC, kind ASC, place ASC""", GUILD_ID)
    e = discord.Embed(title="Past winners", color=0xF0B232)
    if not rows:
        e.description = "No winners yet. This month could be yours."
        return e
    months = {}
    for r in rows:
        label = "👑 Top VIP" if r["kind"] == "vip" else MEDALS[r["place"] - 1]
        months.setdefault(r["month"], []).append(
            f"{label} {display(guild, r['user_id'])} — {r['points']:,} pts")
    for m, lines in months.items():
        name = dt.datetime.strptime(m, "%Y-%m").strftime("%B %Y")
        e.add_field(name=name, value="\n".join(lines), inline=False)
    return e


class BoardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="My stats", style=discord.ButtonStyle.secondary, custom_id="lb:stats")
    async def my_stats(self, interaction, button):
        await interaction.response.send_message(
            embed=await stats_embed(interaction.guild, interaction.user), ephemeral=True)

    @discord.ui.button(label="How to earn", style=discord.ButtonStyle.secondary, custom_id="lb:earn")
    async def how_to_earn(self, interaction, button):
        await interaction.response.send_message(embed=earn_embed(), ephemeral=True)

    @discord.ui.button(label="Past winners", style=discord.ButtonStyle.secondary, custom_id="lb:winners")
    async def past_winners(self, interaction, button):
        await interaction.response.send_message(embed=await winners_embed(interaction.guild), ephemeral=True)


# ---------- Live board + month close ----------
async def refresh_board(guild):
    ch = guild.get_channel(BOARD_CHANNEL_ID)
    if ch is None:
        return
    embed = await board_embed(guild)
    mid = await get_setting("board_message_id")
    if mid:
        try:
            await ch.get_partial_message(int(mid)).edit(embed=embed, view=BoardView())
            return
        except discord.NotFound:
            pass
    msg = await ch.send(embed=embed, view=BoardView())
    await set_setting("board_message_id", msg.id)


async def review_lines(guild, user_id, month):
    rows = await bot.pool.fetch(
        "SELECT reason, SUM(amount)::int AS s FROM points WHERE guild_id=$1 AND user_id=$2 AND month=$3 GROUP BY reason",
        GUILD_ID, user_id, month)
    parts = ", ".join(f"{r['reason']}: {r['s']}" for r in rows)
    flags = []
    reacts = await bot.pool.fetch(
        """SELECT reactor_id, COUNT(*)::int AS c FROM reaction_awards
           WHERE guild_id=$1 AND receiver_id=$2 AND month=$3 GROUP BY reactor_id ORDER BY c DESC""",
        GUILD_ID, user_id, month)
    total_reacts = sum(r["c"] for r in reacts)
    if total_reacts >= 10 and reacts[0]["c"] / total_reacts >= REVIEW_REACTOR_SHARE:
        share = round(100 * reacts[0]["c"] / total_reacts)
        flags.append(f"⚠️ {share}% of reactions came from {display(guild, reacts[0]['reactor_id'])}")
    return parts, flags


async def close_month(guild, month):
    rows = await top(month, 200)
    podium = rows[:3]
    vip_row = next((r for r in rows if is_vip(guild, r["user_id"])), None)

    for place, r in enumerate(podium, start=1):
        await bot.pool.execute(
            "INSERT INTO winners VALUES ($1,$2,'main',$3,$4,$5) ON CONFLICT DO NOTHING",
            GUILD_ID, month, place, r["user_id"], r["pts"])
    if vip_row:
        await bot.pool.execute(
            "INSERT INTO winners VALUES ($1,$2,'vip',1,$3,$4) ON CONFLICT DO NOTHING",
            GUILD_ID, month, vip_row["user_id"], vip_row["pts"])

    month_name = dt.datetime.strptime(month, "%Y-%m").strftime("%B")
    winners_ch = guild.get_channel(WINNERS_CHANNEL_ID)
    if winners_ch and podium:
        e = discord.Embed(title=f"🎉 {month_name} winners", color=0xF0B232)
        e.description = "\n".join(
            f"{MEDALS[i]} <@{r['user_id']}> — {r['pts']:,} pts" for i, r in enumerate(podium))
        e.add_field(name="Prize", value=f"<@{podium[0]['user_id']}> wins {PRIZE}", inline=False)
        if vip_row:
            e.add_field(name="👑 Top VIP", value=f"<@{vip_row['user_id']}> wins {VIP_PRIZE}", inline=False)
        e.add_field(name="🏆 New Monthly Champion",
                    value=f"<@{podium[0]['user_id']}> holds the title until the next winner is crowned.", inline=False)
        e.set_footer(text="Prizes go out after a quick staff check. New month, new race!")
        await winners_ch.send(embed=e)

    if podium:
        try:
            await crown_champion(guild, podium[0]["user_id"])
        except discord.Forbidden:
            print("Can't manage the Monthly Champion role: move the bot's role above it.")

    staff_ch = guild.get_channel(STAFF_CHANNEL_ID)
    if staff_ch:
        e = discord.Embed(title=f"Review before payout: {month_name}", color=0xED4245)
        review = list(rows[:5])
        if vip_row and vip_row not in review:
            review.append(vip_row)
        for i, r in enumerate(review):
            parts, flags = await review_lines(guild, r["user_id"], month)
            tag = " 👑" if is_vip(guild, r["user_id"]) else ""
            e.add_field(
                name=f"#{rows.index(r) + 1} {display(guild, r['user_id'])}{tag} — {r['pts']:,} pts",
                value=(parts or "no points") + ("\n" + "\n".join(flags) if flags else ""),
                inline=False)
        e.set_footer(text="If someone cheated: /disqualify them for that month, then re-announce.")
        await staff_ch.send(embed=e)


async def maybe_close_month(guild):
    current = month_key()
    active = await get_setting("active_month")
    if active is None:
        await set_setting("active_month", current)
        return
    if active != current:
        await set_setting("active_month", current)
        await close_month(guild, active)
        await set_setting("board_message_id", "")   # start a fresh board post for the new month


@tasks.loop(minutes=10)
async def ticker():
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    for step in (maybe_close_month, check_pending_invites, refresh_board):
        try:
            await step(guild)
        except Exception as exc:
            print(f"{step.__name__} failed: {exc!r}")


@ticker.before_loop
async def before_ticker():
    await bot.wait_until_ready()


# ---------- Slash commands ----------
@bot.tree.command(name="daily", description="Check in once a day for points")
async def daily(interaction: discord.Interaction):
    uid = interaction.user.id
    today = now().date()
    row = await bot.pool.fetchrow(
        "SELECT daily_last, streak FROM user_state WHERE guild_id=$1 AND user_id=$2", GUILD_ID, uid)
    last, streak = (row["daily_last"], row["streak"]) if row else (None, 0)
    if last == today:
        await interaction.response.send_message(
            f"You already checked in today. Streak: {streak} days. Come back tomorrow!", ephemeral=True)
        return
    streak = streak + 1 if last == today - dt.timedelta(days=1) else 1
    await bot.pool.execute(
        """INSERT INTO user_state (guild_id, user_id, daily_last, streak) VALUES ($1,$2,$3,$4)
           ON CONFLICT (guild_id, user_id) DO UPDATE SET daily_last=$3, streak=$4""",
        GUILD_ID, uid, today, streak)
    await award(uid, DAILY_POINTS, "daily")
    text = f"✅ +{DAILY_POINTS} points. Streak: {streak} days."
    if streak % STREAK_LEN == 0:
        await award(uid, STREAK_BONUS, "streak")
        text += f" 🔥 {STREAK_LEN}-day streak bonus: +{STREAK_BONUS}!"
    await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(name="leaderboard", description="See this month's leaderboard")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=await board_embed(interaction.guild), view=BoardView(), ephemeral=True)


@bot.tree.command(name="points_adjust", description="Staff: add or remove points")
@app_commands.default_permissions(manage_guild=True)
async def points_adjust(interaction: discord.Interaction, member: discord.Member, amount: int, reason: str):
    await award(member.id, amount, "admin", note=f"{interaction.user.id}: {reason}")
    await interaction.response.send_message(
        f"Adjusted {member.mention} by {amount:+} points ({reason}).", ephemeral=True)


@bot.tree.command(name="disqualify", description="Staff: remove someone from this month's race")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(month="YYYY-MM, defaults to this month")
async def disqualify(interaction: discord.Interaction, member: discord.Member, month: str = None):
    m = month or month_key()
    await bot.pool.execute("INSERT INTO disqualified VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                           GUILD_ID, member.id, m)
    await interaction.response.send_message(
        f"{member.mention} is disqualified for {m}. If they already won that month, run /recrown.", ephemeral=True)


@bot.tree.command(name="recrown", description="Staff: redo a closed month's winners after a disqualification")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(month="YYYY-MM of the month to redo")
async def recrown(interaction: discord.Interaction, month: str):
    await interaction.response.defer(ephemeral=True)
    await bot.pool.execute("DELETE FROM winners WHERE guild_id=$1 AND month=$2", GUILD_ID, month)
    await close_month(interaction.guild, month)
    await refresh_board(interaction.guild)
    await interaction.followup.send(f"Re-announced {month} and moved the Monthly Champion role.", ephemeral=True)


@bot.tree.command(name="undisqualify", description="Staff: restore someone to the race")
@app_commands.default_permissions(manage_guild=True)
async def undisqualify(interaction: discord.Interaction, member: discord.Member, month: str = None):
    m = month or month_key()
    await bot.pool.execute("DELETE FROM disqualified WHERE guild_id=$1 AND user_id=$2 AND month=$3",
                           GUILD_ID, member.id, m)
    await interaction.response.send_message(f"{member.mention} is back in the race for {m}.", ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
