# libaries and imports and env tokens n shit
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()  # reads the .env file so we can grab the token n test server id
TOKEN = os.getenv("DISCORD_TOKEN")
TEST_SRV = os.getenv("TEST_SRV")  # test server

# bot config ####################################

TZ = ZoneInfo("Europe/London")  # week resets use london time (handles bst/gmt for us)
DATA_FILE = Path(__file__).parent / "data.json"  # where all the events n points get saved

# hicom role, only they can run the /points commands
HICOM_ROLE = 1436839651519238196

# points each event type is worth
EVENT_POINTS = {
    "Patrol": 1,
    "GIA event": 1,
    "DT": 2,
    "Spar": 1,
}

# ID ROLE STORE
# 1436846066401280161 - tryout perm
# 1436811613956083762 - s40 hr
# 1436811650719027280 - aiwha hr
# 1467154318913441855 - RS
# 1436809851912196227 - delta squad
# 1436809523930206252 - s40
# 1436809879921754245 - aiwha
# 1436852566137770144 - indoc

# weekly points required from ATTENDING (0 = requirement removed, takes priority)
pointFromAttend = {
    1467154318913441855: 0,  # rs
    1436809851912196227: 1,  # delta
    1436809523930206252: 1,  # s40
    1436809879921754245: 1,  # aiwha
    1436852566137770144: 0,  # indoc

    1560570388910571582: 1   # testing role
}

# weekly points required from HOSTING (only these roles earn host points)
pointFromHost = {
    1436846066401280161: 1,  # tryout perm
    1436811613956083762: 2,  # s40 hr
    1436811650719027280: 2,  # aiwha hr

    1560570374029189171: 1   # testing role
}

# data storage ###########################################


def load_data():
    # if data.json exists read it, otherwise start with an empty one
    if DATA_FILE.exists():
        with DATA_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {"next_event_id": 1, "events": [], "awards": []}


def save_data():
    # write to a temp file then swap it in, so a crash can't corrupt data.json
    tmp = DATA_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_FILE)


data = load_data()  # loaded once at startup, kept in memory while the bot runs

# points helpers ###########################################


def week_start():
    # works out when this week started: monday 00:00 london time
    now = datetime.now(TZ)
    monday = now - timedelta(days=now.weekday())  # weekday() is 0 on monday
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def weekly_totals(start):
    # adds up everyones points since the week started
    # result looks like {user_id: {"attend": 2, "host": 1}}
    totals = {}
    for a in data["awards"]:
        if datetime.fromisoformat(a["timestamp"]) < start:
            continue  # award is from a previous week, ignore it
        t = totals.setdefault(a["user"], {"attend": 0, "host": 0})
        if a["source"] in t:
            t[a["source"]] += a["points"]
    return totals


def get_reqs(member):
    # grabs the point requirement from each of the members roles, lowest one wins
    # None = they have no role in that dict so theres nothing to check
    att = [pointFromAttend[r.id] for r in member.roles if r.id in pointFromAttend]
    host = [pointFromHost[r.id] for r in member.roles if r.id in pointFromHost]
    return (min(att) if att else None, min(host) if host else None)


def check_member(member, totals):
    # compares a members points to their requirements
    # returns attend req, host req, attend points, host points, and if they passed
    att_req, host_req = get_reqs(member)
    t = totals.get(member.id, {"attend": 0, "host": 0})
    passed = True
    if att_req is not None and t["attend"] < att_req:
        passed = False
    if host_req is not None and t["host"] < host_req:
        passed = False
    return att_req, host_req, t["attend"], t["host"], passed


def fmt(pts, req):
    # makes the points look nice e.g. "1/2", or says if theres no requirement / exempt
    if req is None:
        return f"{pts} (no req)"
    if req == 0:
        return f"{pts} (exempt)"
    return f"{pts}/{req}"


async def hicom_only(interaction):
    # returns True if the person running the command has the hicom role
    # if they dont, tell them (only they can see it) and return False
    if any(r.id == HICOM_ROLE for r in interaction.user.roles):
        return True
    await interaction.response.send_message("HICOM only.", ephemeral=True)
    return False


async def send_chunks(interaction, lines):
    # discord caps messages at 2000 chars so pack the lines into messages under that
    # allowed_mentions none = shows the names but doesnt ping anyone
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 1900:
            await interaction.followup.send(chunk, allowed_mentions=discord.AllowedMentions.none())
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await interaction.followup.send(chunk, allowed_mentions=discord.AllowedMentions.none())

# bot main #############################################

intents = discord.Intents.default()
intents.members = True  # needed for the audit; must also be enabled in the Developer Portal

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)


@bot.event
async def setup_hook():
    # runs once at startup: sync slash commands
    if TEST_SRV:
        guild = discord.Object(id=int(TEST_SRV))
        bot.tree.copy_global_to(guild=guild)  # copies commands to the test server so they show up instantly
        await bot.tree.sync(guild=guild)
        print(f"Synced commands to guild {TEST_SRV}")
    else:
        await bot.tree.sync()
        print("Synced commands globally")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


# bot commands #

@bot.tree.command(name="ping", description="Check the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")


# /event log #

event_group = app_commands.Group(name="event", description="Event commands")


@event_group.command(name="log", description="Log an event and award points")
@app_commands.describe(event_type="Type of event", attendees="Ping everyone who attended")
@app_commands.choices(event_type=[app_commands.Choice(name=n, value=n) for n in EVENT_POINTS])
async def event_log(
    interaction: discord.Interaction,
    event_type: app_commands.Choice[str],
    attendees: str,
):
    # only works in a server (we need roles n members)
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    host = interaction.user

    # only members holding a role in pointFromHost can log events
    if not any(r.id in pointFromHost for r in host.roles):
        await interaction.response.send_message(
            "Only members with a hosting role can log events.", ephemeral=True
        )
        return

    points = EVENT_POINTS[event_type.value]  # how many points this event type gives
    now = datetime.now(TZ).isoformat()  # timestamp saved on everything so we know which week it counts for

    # pull user IDs out of the pings, remove duplicates, drop the host and bots
    ids = {int(i) for i in re.findall(r"<@!?(\d+)>", attendees)}
    ids.discard(host.id)
    members = [m for m in (guild.get_member(i) for i in ids) if m ]
    if not members:
        await interaction.response.send_message(
            "No valid attendees found. Ping the attendees (not the host) in the attendees field.",
            ephemeral=True,
        )
        return

    # save the event itself
    event_id = data["next_event_id"]
    data["next_event_id"] += 1
    data["events"].append({
        "id": event_id,
        "type": event_type.value,
        "host": host.id,
        "attendees": [m.id for m in members],
        "timestamp": now,
    })
    # one award per attendee, tagged "attend" so the audit knows where it came from
    for m in members:
        data["awards"].append({
            "event_id": event_id, "user": m.id, "points": points,
            "source": "attend", "timestamp": now,
        })
    # host gets their own award tagged "host"
    data["awards"].append({
        "event_id": event_id, "user": host.id, "points": points,
        "source": "host", "timestamp": now,
    })
    save_data()

    # embed fields cap at 1024 chars so cut the attendee list if its huge
    attendee_text = " ".join(m.mention for m in members)
    if len(attendee_text) > 1000:
        attendee_text = attendee_text[:1000] + "..."

    # the summary message that gets posted in the channel
    embed = discord.Embed(title=f"Event logged: {event_type.value}", color=discord.Color.green())
    embed.add_field(name=f"Host (+{points})", value=host.mention, inline=False)
    embed.add_field(name=f"Attendees (+{points} each)", value=attendee_text, inline=False)
    embed.set_footer(text=f"Event #{event_id}")
    await interaction.response.send_message(embed=embed)


bot.tree.add_command(event_group)

# /points check + /points audit #

points_group = app_commands.Group(name="points", description="Weekly point commands", guild_only=True)


@points_group.command(name="check", description="Check someone's points for this week")
@app_commands.describe(user="Who to check (leave empty to check yourself)")
async def points_check(interaction: discord.Interaction, user: discord.Member = None):
    if not await hicom_only(interaction):
        return

    member = user or interaction.user  # no user picked = check yourself
    start = week_start()
    totals = weekly_totals(start)
    att_req, host_req, att_pts, host_pts, passed = check_member(member, totals)

    # no role in either dict = they dont have a quota
    if att_req is None and host_req is None:
        await interaction.response.send_message(
            f"{member.display_name} has no point requirement.", ephemeral=True
        )
        return

    # green if they met everything, red if not
    embed = discord.Embed(
        title=f"Weekly points: {member.display_name}",
        color=discord.Color.green() if passed else discord.Color.red(),
    )
    embed.add_field(name="Attending", value=fmt(att_pts, att_req))
    embed.add_field(name="Hosting", value=fmt(host_pts, host_req))
    embed.add_field(name="Status", value="Met" if passed else "Not met")
    embed.set_footer(text=f"Week started {start.strftime('%a %d %b %H:%M')}")
    await interaction.response.send_message(embed=embed)


@points_group.command(name="audit", description="Check everyone's points for this week")
async def points_audit(interaction: discord.Interaction):
    if not await hicom_only(interaction):
        return

    await interaction.response.defer()  # tells discord "working on it" so it doesnt time out

    start = week_start()
    totals = weekly_totals(start)  # worked out once, then reused for every member

    # sort everyone into met / not met
    missed = []
    met = []
    skipped = 0  # members with no quota at all
    for member in interaction.guild.members:
        if member.bot:
            continue
        att_req, host_req, att_pts, host_pts, passed = check_member(member, totals)
        if att_req is None and host_req is None:
            skipped += 1
            continue
        line = f"{member.mention} - attend {fmt(att_pts, att_req)}, host {fmt(host_pts, host_req)}"
        if passed:
            met.append(line)
        else:
            missed.append(line)

    # build the report then send it (split into multiple messages if its long)
    lines = [f"**Weekly audit** (week started {start.strftime('%a %d %b %H:%M')})"]
    lines.append(f"**Not met ({len(missed)})**")
    lines += missed or ["nobody, everyone met their requirement"]
    lines.append(f"**Met ({len(met)})**")
    lines += met or ["nobody"]
    lines.append(f"{skipped} members have no requirement")
    await send_chunks(interaction, lines)


# corrections (delete events / manual point changes) #
# all of this is hicom only. its one block so its easy to paste into your own copy

# the dropdown options for which kind of points you're adjusting
SOURCE_CHOICES = [
    app_commands.Choice(name="Attending", value="attend"),
    app_commands.Choice(name="Hosting", value="host"),
]


async def adjust_points(interaction, member, amount, source, reason):
    # shared by /points add and /points remove
    # amount is positive for add and negative for remove
    # saved as an award with no event_id, so the audit counts it like any other point
    data["awards"].append({
        "event_id": None, "user": member.id, "points": amount, "source": source,
        "timestamp": datetime.now(TZ).isoformat(),
        "adjusted_by": interaction.user.id, "reason": reason,  # so you can see who changed what
    })
    save_data()

    # show their totals for this week after the change
    t = weekly_totals(week_start()).get(member.id, {"attend": 0, "host": 0})
    sign = "+" if amount > 0 else ""
    await interaction.response.send_message(
        f"{sign}{amount} {source} points for {member.mention}"
        + (f" ({reason})" if reason else "")
        + f". This week: attend {t['attend']}, host {t['host']}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@points_group.command(name="add", description="Manually add points to someone this week")
@app_commands.describe(
    user="Who gets the points",
    amount="How many points to add",
    category="Do these count as attending or hosting points",
    reason="Optional note on why",
)
@app_commands.choices(category=SOURCE_CHOICES)
async def points_add(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: app_commands.Range[int, 1, 100],
    category: app_commands.Choice[str],
    reason: str = None,
):
    if not await hicom_only(interaction):
        return
    await adjust_points(interaction, user, amount, category.value, reason)


@points_group.command(name="remove", description="Manually remove points from someone this week")
@app_commands.describe(
    user="Who loses the points",
    amount="How many points to remove",
    category="Do these come off attending or hosting points",
    reason="Optional note on why",
)
@app_commands.choices(category=SOURCE_CHOICES)
async def points_remove(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: app_commands.Range[int, 1, 100],
    category: app_commands.Choice[str],
    reason: str = None,
):
    if not await hicom_only(interaction):
        return
    await adjust_points(interaction, user, -amount, category.value, reason)  # negative = removes points


@event_group.command(name="delete", description="Delete a logged event and remove its points")
@app_commands.describe(event_id="The event number (its in the footer of the log message)")
async def event_delete(interaction: discord.Interaction, event_id: int):
    if not await hicom_only(interaction):
        return

    # find the event, if it doesnt exist tell them
    event = next((e for e in data["events"] if e["id"] == event_id), None)
    if event is None:
        await interaction.response.send_message(f"No event with ID {event_id}.", ephemeral=True)
        return

    # remove the event and every award that came from it
    data["events"] = [e for e in data["events"] if e["id"] != event_id]
    before = len(data["awards"])
    data["awards"] = [a for a in data["awards"] if a.get("event_id") != event_id]
    removed = before - len(data["awards"])
    save_data()

    await interaction.response.send_message(
        f"Deleted event #{event_id} ({event['type']}, hosted by <@{event['host']}>), "
        f"removed {removed} point awards.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


bot.tree.add_command(points_group)

# ------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not found - check your .env file")
    bot.run(TOKEN)
