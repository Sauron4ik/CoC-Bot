import asyncio
import json
import logging
import os
import urllib.parse
import requests
import random
import re
import feedparser
import aiohttp
from aiogram.utils.keyboard import InlineKeyboardBuilder
from datetime import datetime, timezone
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated, FSInputFile
from aiogram.filters import Command, ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.enums import ParseMode

# === ЗАВАНТАЖЕННЯ ЗМІННИХ СЕРЕДОВИЩА ===
load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
COC_TOKEN = os.getenv("COC_TOKEN")
CLAN_TAG = os.getenv("CLAN_TAG", "#2PGVU889Q")
THREAD_ID = int(os.getenv("THREAD_ID", "14128"))
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
NEWS_THREAD_ID = 4026
YOUTUBE_CHANNEL_ID = "UCekeHLY2xpfjcGq_3gHFaTA"  # Канал SALOMON

if not TG_TOKEN or not COC_TOKEN:
    raise ValueError("⚠️ Помилка: TG_TOKEN або COC_TOKEN не знайдено у файлі .env!")

HEADERS = {"Authorization": f"Bearer {COC_TOKEN}"}
ENCODED_TAG = urllib.parse.quote(CLAN_TAG)

PLAYERS_FILE = "players.json"
STATE_FILE = "bot_state.json"
LEAGUES_FILE = "players_leagues.json"
HISTORY_FILE = "history.json"

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()

def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def record_player_stats(player_tag, player_name, event_type, attacks_done, attacks_max):
    history = load_json(HISTORY_FILE, {})
    month_key = datetime.now().strftime("%Y-%m")
    
    if month_key not in history:
        history[month_key] = {}
        
    if player_tag not in history[month_key]:
        history[month_key][player_tag] = {
            "name": player_name,
            "cw_done": 0, "cw_missed": 0,
            "cwl_done": 0, "cwl_missed": 0,
            "raid_done": 0, "raid_missed": 0
        }
    
    p = history[month_key][player_tag]
    p["name"] = player_name
    missed = max(0, attacks_max - attacks_done)
    
    if event_type == "cw":
        p["cw_done"] += attacks_done
        p["cw_missed"] += missed
    elif event_type == "cwl":
        p["cwl_done"] += attacks_done
        p["cwl_missed"] += missed
    elif event_type == "raid":
        p["raid_done"] += attacks_done
        p["raid_missed"] += missed
        
    save_json(HISTORY_FILE, history)

player_links = load_json(PLAYERS_FILE, {})
bot_state = load_json(STATE_FILE, {
    "last_war_state": "",
    "war_3h_reminded": False,
    "last_raid_state": "",
    "raid_24h_reminded": False,
    "clan_games_reminded": False
})

def format_mention(tag: str, name: str) -> str:
    tag_clean = tag.upper()
    if tag_clean in player_links:
        user_ref = player_links[tag_clean]
        if isinstance(user_ref, int) or (isinstance(user_ref, str) and user_ref.isdigit()):
            return f'<a href="tg://user?id={user_ref}">{name}</a>'
        elif isinstance(user_ref, str) and user_ref.startswith("@"):
            return user_ref
    return name

def get_clash_data(endpoint: str):
    url = f"https://api.clashofclans.com/v1/{endpoint}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            return res.json()
        elif res.status_code == 404:
            return None
        else:
            logging.error(f"Помилка API CoC [{res.status_code}]: {res.text}")
    except Exception as e:
        logging.error(f"Помилка з'єднання: {e}")
    return None

def parse_coc_time(time_str: str) -> datetime:
    clean_str = time_str.split(".")[0]
    return datetime.strptime(clean_str, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)

def load_previous_leagues():
    try:
        with open(LEAGUES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_current_leagues(data):
    with open(LEAGUES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

async def process_weekly_league_report():
    data = get_clash_data(f"clans/{ENCODED_TAG}")
    if not data or "memberList" not in data:
        return "❌ Не вдалося отримати дані клану."

    old_data = load_previous_leagues()
    new_data = {}
    player_rows = []

    for member in data["memberList"]:
        tag = member.get("tag")
        raw_name = member.get("name", "Гравець")
        clean_name = raw_name.replace("*", "").replace("_", "").replace("`", "")

        raw_league = member.get("league", {}).get("name", "Unranked")
        trophies = member.get("trophies", 0)

        if raw_league == "Legend League":
            if trophies >= 5400:
                league_name = "Legend League I"
            elif trophies >= 5200:
                league_name = "Legend League II"
            else:
                league_name = "Legend League III"
        else:
            league_name = raw_league

        new_data[tag] = {
            "name": clean_name,
            "league": league_name,
            "trophies": trophies,
        }

        if tag in old_data:
            prev_league = old_data[tag].get("league", "Unranked")
            prev_trophies = old_data[tag].get("trophies", 0)

            diff = trophies - prev_trophies
            sign = f"+{diff}" if diff > 0 else f"{diff}"

            if prev_league != league_name:
                league_str = f"{prev_league} ➔ {league_name}"
            else:
                league_str = f"{league_name}"

            player_rows.append(
                f"▫️ {clean_name:<16}: {league_str:<25} | {trophies:>4} 🏆 ({sign})"
            )
        else:
            player_rows.append(
                f"▫️ {clean_name:<16}: {league_name:<25} | {trophies:>4} 🏆 (новий гравець)"
            )

    save_current_leagues(new_data)

    header = "🏆 *За минулий бойовий тиждень ми маємо наступні зміни у клані:*\n\n```\n"
    footer = "\n```"
    return header + "\n".join(player_rows) + footer

# === КОМАНДИ ===

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    history = load_json(HISTORY_FILE, {})
    month_key = datetime.now().strftime("%Y-%m")
    
    if month_key not in history or not history[month_key]:
        await message.answer("📊 За цей місяць ще немає збереженої історії атак.")
        return

    lines = [f"📊 <b>Статистика атак за {month_key}:</b>\n"]
    
    for tag, d in history[month_key].items():
        lines.append(
            f"👤 <b>{d['name']}</b>\n"
            f" ├ ⚔️ <b>КВ:</b> зроблено {d['cw_done']} | ❌ пропущено {d['cw_missed']}\n"
            f" ├ 🏆 <b>ЛВК:</b> зроблено {d['cwl_done']} | ❌ пропущено {d['cwl_missed']}\n"
            f" └ 🛡️ <b>Рейди:</b> зроблено {d['raid_done']} | ❌ пропущено {d['raid_missed']}\n"
        )
        
    await message.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("start", "help"))
async def cmd_start(msg: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚔️ Стан КВ", callback_data="btn_war"),
            InlineKeyboardButton(text="🏹 Рейди", callback_data="btn_raid")
        ],
        [
            InlineKeyboardButton(text="🏆 CWL", callback_data="btn_cwl"),
            InlineKeyboardButton(text="📊 Тижневий звіт", callback_data="btn_weekly")
        ]
    ])

    photo = FSInputFile("23.jpg")
    await msg.answer_photo(
        photo=photo,
        caption=(
        "Привіт! Я помічник Саурона 🏰\n\n"
        "Ось що ти можеш вибрати:\n"
        "• `/link #ТЕГ_ГРАВЦЯ` — Прив'язати Telegram до акаунта (адмінам: реплай + `/link #ТЕГ`)\n"
        "• `/unlink` — Видалити прив'язку (через реплай або `/unlink #ТЕГ`)\n"
        "• `/listlinks` — Список усіх прив'язаних акаунтів (адмінам)\n"
        "• `/player (нік або тег)` — Картка гравця (ТН, кубки, донат)\n"
        "• `/war` — Стан поточної війни (КВ)\n"
        "• `/raid` — Звіт по рейд-вікенду\n"
        "• `/raidstats` — Підсумки останнього рейду (золото, боржники)\n"
        "• `/cwl` — Звіт по Війнах Ліги\n"
        "• `/stats` — Статистика гравців по усіх івентах за місяць\n"
        "• `/weekly_report` — Звіт за тиждень (кубки та ліги)\n"
        "Обирай потрібну дію кнопками нижче або вводь команди вручну!"
        ),
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.callback_query()
async def process_callback_buttons(callback: types.CallbackQuery):
    code = callback.data
    if code == "btn_war":
        await cmd_war(callback.message)
    elif code == "btn_raid":
        await cmd_raid(callback.message)
    elif code == "btn_cwl":
        await cmd_cwl(callback.message)
    elif code == "btn_weekly":
        report = await process_weekly_league_report()
        await callback.message.answer(report, parse_mode=ParseMode.MARKDOWN)
    
    await callback.answer()

async def is_admin(message: types.Message) -> bool:
    if message.chat.type == 'private':
        return True 
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in ['creator', 'administrator']
    except Exception:
        return False


@dp.message(Command("link"))
async def cmd_link(msg: types.Message):
    args = msg.text.split()
    target_user_id = msg.from_user.id
    target_user_name = msg.from_user.first_name
    
    admin_mode = await is_admin(msg)

    if admin_mode and msg.reply_to_message:
        target_user_id = msg.reply_to_message.from_user.id
        target_user_name = msg.reply_to_message.from_user.first_name
        player_tag_arg_index = 1
    else:
        player_tag_arg_index = 1

    if len(args) <= player_tag_arg_index:
        await msg.answer("Вкажіть ваш тег. Приклад: <code>/link #2ABC123</code>", parse_mode=ParseMode.HTML)
        return

    player_tag = args[player_tag_arg_index].upper().replace("%23", "#")
    if not player_tag.startswith("#"):
        player_tag = "#" + player_tag

    player_links[player_tag] = target_user_id
    save_json(PLAYERS_FILE, player_links)

    mention_html = f'<a href="tg://user?id={target_user_id}">{target_user_name}</a>'
    
    try:
        photo = FSInputFile("22.jpg")
        await msg.answer_photo(
            photo=photo,
            caption=f"Чудово! Тег <code>{player_tag}</code> прив'язано до {mention_html} ✨",
            parse_mode="HTML"
        )
    except Exception:
        await msg.answer(f"Чудово! Тег <code>{player_tag}</code> прив'язано до {mention_html} ✨", parse_mode="HTML")


@dp.message(Command("unlink"))
async def cmd_unlink(msg: types.Message):
    if not await is_admin(msg):
        await msg.answer("❌ Ця команда доступна лише адміністраторам.")
        return

    target_user_id = None
    if msg.reply_to_message:
        target_user_id = msg.reply_to_message.from_user.id
    else:
        args = msg.text.split()
        if len(args) > 1:
            target_tag = args[1].upper().replace("%23", "#")
            if not target_tag.startswith("#"):
                target_tag = "#" + target_tag
            found_tag = None
            for tag, uid in player_links.items():
                if tag.upper() == target_tag:
                    found_tag = tag
                    break
            if found_tag:
                del player_links[found_tag]
                save_json(PLAYERS_FILE, player_links)
                await msg.answer(f"🗑 Прив'язку тегу <code>{found_tag}</code> успішно видалено!", parse_mode="HTML")
                return

    if not target_user_id:
        await msg.answer("⚠️ Зробіть реплай (відповідь) на повідомлення користувача або вкажіть тег: <code>/unlink #TAG</code>", parse_mode="HTML")
        return

    found_tags = [tag for tag, uid in player_links.items() if uid == target_user_id]
    
    if not found_tags:
        await msg.answer("❌ У цього користувача немає прив'язаних тегів.")
        return

    for tag in found_tags:
        del player_links[tag]
    
    save_json(PLAYERS_FILE, player_links)
    await msg.answer("🗑 Усі прив'язки цього користувача успішно видалено!")


@dp.message(Command("listlinks"))
async def cmd_listlinks(msg: types.Message):
    if not await is_admin(msg):
        await msg.answer("❌ Ця команда доступна лише адміністраторам.")
        return

    try:
        with open(PLAYERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = player_links

    if not data:
        await msg.answer("📋 Список прив'язаних ігрових акаунтів порожній.")
        return

    user_tags = {}
    for tag, uid in data.items():
        user_tags.setdefault(str(uid), []).append(tag)

    text = "📋 <b>Список прив'язаних акаунтів:</b>\n\n"

    for uid_str, tags in user_tags.items():
        display_name = f"ID: {uid_str}"

        clean_id = uid_str.lstrip('-')
        if clean_id.isdigit():
            try:
                member = await msg.bot.get_chat_member(msg.chat.id, int(uid_str))
                u = member.user
                display_name = f"@{u.username}" if u.username else (u.first_name or display_name)
            except Exception:
                pass

        tags_str = ", ".join([f"<code>{t}</code>" for t in tags])
        text += f"👤 <b>{display_name}</b>:\n└ Теги: {tags_str}\n\n"

    if len(text) > 4096:
        text = text[:4090] + "..."

    await msg.answer(text, parse_mode="HTML")

@dp.message(Command("war"))
async def cmd_war(msg: types.Message):
    if msg.message_thread_id and msg.message_thread_id != THREAD_ID:
        return

    data = get_clash_data(f"clans/{ENCODED_TAG}/currentwar")
    if not data:
        await msg.answer("❌ Не вдалося отримати дані про війну.")
        return

    state = data.get("state")
    if state == "notInWar":
        await msg.answer("⚔️ Клан зараз не перебуває у війні.")
        return

    opponent_name = data.get("opponent", {}).get("name", "Невідомо")

    if state == "preparation":
        await msg.answer(f"⏳ Триває день підготовки до війни проти «<b>{opponent_name}</b>»!", parse_mode=ParseMode.HTML)
    elif state == "inWar":
        clan_stars = data.get("clan", {}).get("stars", 0)
        opp_stars = data.get("opponent", {}).get("stars", 0)
        
        clan_members = data.get("clan", {}).get("members", [])
        unattacked = []

        for m in clan_members:
            attacks = m.get("attacks", [])
            cnt = len(attacks)
            if cnt < 2:
                tag = m.get("tag", "")
                name = m.get("name", "Гравець")
                mention = format_mention(tag, name)
                unattacked.append(f"• {mention} — {cnt}/2 ⚔️")

        text = (
            f"⚔️ Ми воюємо з «<b>{opponent_name}</b>»!\n"
            f"⭐ Зірки: <b>{clan_stars}</b> — <b>{opp_stars}</b>\n\n"
        )

        if unattacked:
            text += "⚠️ <b>Ще не зробили всі атаки:</b>\n" + "\n".join(unattacked)
        else:
            text += "🎉 Усі учасники зробили свої 2 атаки!"

        await msg.answer(text, parse_mode=ParseMode.HTML)

    elif state == "warEnded":
        clan_stars = data.get("clan", {}).get("stars", 0)
        opp_stars = data.get("opponent", {}).get("stars", 0)
        clan_dest = data.get("clan", {}).get("destructionPercentage", 0)
        opp_dest = data.get("opponent", {}).get("destructionPercentage", 0)

        if clan_stars > opp_stars:
            res_str = "🎉 Перемога!"
        elif clan_stars < opp_stars:
            res_str = "💔 Поразка..."
        else:
            if clan_dest > opp_dest:
                res_str = "🎉 Перемога за відсотками!"
            elif clan_dest < opp_dest:
                res_str = "💔 Поразка за відсотками..."
            else:
                res_str = "🤝 Бойова нічия!"

        clan_members = data.get("clan", {}).get("members", [])
        not_full = []
        for m in clan_members:
            attacks = m.get("attacks", [])
            cnt = len(attacks)
            stars = sum(a.get("stars", 0) for a in attacks)
            if cnt < 2:
                mention = format_mention(m["tag"], m["name"])
                not_full.append(f"• {mention} — {cnt}/2 атак ⚔️ ({stars} ⭐)")

        text = (
            f"🏁 Війна проти «<b>{opponent_name}</b>» завершилася.\n"
            f"Результат: <b>{res_str}</b>\n"
            f"⭐ Зірки: <b>{clan_stars}</b> — <b>{opp_stars}</b>\n"
            f"💥 Руйнування: <b>{clan_dest:.1f}%</b> — <b>{opp_dest:.1f}%</b>\n\n"
        )

        if not_full:
            text += "⚠️ <b>Не зробили всі атаки:</b>\n" + "\n".join(not_full)
        else:
            text += "🎉 Усі зробили свої атаки! Молодці!"

        await msg.answer(text, parse_mode=ParseMode.HTML)

@dp.message(Command("test_league"))
async def cmd_test_league(msg: types.Message):
    data = get_clash_data(f"clans/{ENCODED_TAG}")
    if not data or "memberList" not in data:
        await msg.answer("❌ Не вдалося отримати дані.")
        return
    
    first_member = data["memberList"][0]
    league_info = first_member.get("league", {})
    await msg.answer(f"📊 Дані ліги для **{first_member.get('name')}**:\n`{league_info}`", parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("weekly_report"))
async def cmd_weekly_report(msg: types.Message):
    report = await process_weekly_league_report()
    await msg.answer(report, parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("raid"))
async def cmd_raid(msg: types.Message):
    data = get_clash_data(f"clans/{ENCODED_TAG}/capitalraidseasons")
    if not data or "items" not in data or not data["items"]:
        await msg.answer("⚠️ Не вдалося отримати дані про рейди від Supercell.")
        return
    
    current = data["items"][0]
    if current.get("state") != "ongoing":
        await msg.answer("⚔️ Наразі немає активного Рейд-вікенду.")
        return

    unfinished = []
    for m in current.get("members", []):
        tag = m.get("tag", "")
        name = m.get("name", "Гравець")
        used = m.get("attacks", 0)
        
        if isinstance(used, dict):
            used = used.get("count", 0)
            
        limit = m.get("attackLimit", 5) + m.get("bonusAttackLimit", 0)
        
        if used < limit:
            unfinished.append(f"• {format_mention(tag, name)} — {used}/{limit} ⚔️")
    
    text = "🏹 <b>Поточний стан рейду:</b>\n\n"
    if unfinished:
        text += "Гравці, які ще не зробили всі атаки:\n" + "\n".join(unfinished)
    else:
        text += "Усі зробили свої атаки! 🎉"
    
    await msg.answer(text, parse_mode=ParseMode.HTML)

@dp.message(Command("cwl"))
async def cmd_cwl(msg: types.Message):
    if msg.message_thread_id and msg.message_thread_id != THREAD_ID:
        return

    group_data = get_clash_data(f"clans/{ENCODED_TAG}/currentwar/leaguegroup")
    if not group_data or group_data.get("state") == "notInWar":
        await msg.answer("⚔️ Клан зараз не перебуває у Лізі Війн Кланів (CWL).")
        return

    rounds = group_data.get("rounds", [])
    current_war_tag = None

    for r in reversed(rounds):
        war_tags = r.get("warTags", [])
        for w_tag in war_tags:
            if w_tag != "#0":
                war_info = get_clash_data(f"clanwarleagues/wars/{urllib.parse.quote(w_tag)}")
                if war_info and (
                    war_info.get("clan", {}).get("tag") == CLAN_TAG
                    or war_info.get("opponent", {}).get("tag") == CLAN_TAG
                ):
                    if war_info.get("state") in ["inWar", "preparation"]:
                        current_war_tag = w_tag
                        break
        if current_war_tag:
            break

    if not current_war_tag:
        await msg.answer("📊 Активних раундів CWL наразі не знайдено.")
        return

    war_data = get_clash_data(f"clanwarleagues/wars/{urllib.parse.quote(current_war_tag)}")
    if not war_data:
        await msg.answer("❌ Не вдалося отримати дані раунду CWL.")
        return

    state = war_data.get("state")
    is_our_clan_first = war_data.get("clan", {}).get("tag") == CLAN_TAG
    our_clan = war_data.get("clan") if is_our_clan_first else war_data.get("opponent")
    opp_clan = war_data.get("opponent") if is_our_clan_first else war_data.get("clan")

    opp_name = opp_clan.get("name", "Суперник")

    if state == "preparation":
        await msg.answer(f"⏳ **CWL**: Триває підготовка до раунду проти **{opp_name}**!", parse_mode=ParseMode.MARKDOWN)
        return

    our_stars = our_clan.get("stars", 0)
    opp_stars = opp_clan.get("stars", 0)

    unattacked = []
    for member in our_clan.get("members", []):
        attacks = member.get("attacks", [])
        if not attacks:
            unattacked.append(format_mention(member.get("tag"), member.get("name")))

    text = (
        f"🏆 **Ліга Війн Кланів (CWL)**\n"
        f"⚔️ Проти: **{opp_name}**\n"
        f"⭐ Зірки: **{our_stars}** — **{opp_stars}**\n\n"
    )

    if unattacked:
        text += "⚠️ **Ще не зробили атаку:**\n" + "\n".join([f"• {m}" for m in unattacked])
    else:
        text += "🎉 Усі учасники зробили свої атаки!"

    await msg.answer(text, parse_mode=ParseMode.HTML)

@dp.message(Command("player"))
async def cmd_player_stats(msg: types.Message):
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        await msg.answer("❌ Вкажіть тег або ім'я гравця. Приклад:\n/player #QV2JL9G08 або /player Саурон")
        return

    query = args[1].strip()
    player_tag = None

    if query.startswith("#"):
        player_tag = query
    else:
        clan_data = get_clash_data(f"clans/{ENCODED_TAG}")
        if clan_data and "memberList" in clan_data:
            for m in clan_data["memberList"]:
                if query.lower() in m.get("name", "").lower():
                    player_tag = m.get("tag")
                    break

    if not player_tag:
        await msg.answer(f"❌ Гравця «{query}» не знайдено в клані.")
        return

    encoded_player_tag = player_tag.replace("#", "%23")
    p_data = get_clash_data(f"players/{encoded_player_tag}")

    if not p_data:
        await msg.answer("❌ Не вдалося отримати дані гравця з Supercell API.")
        return

    p_name = p_data.get("name", "Невідомо")
    th = p_data.get("townHallLevel", "?")
    trophies = p_data.get("trophies", 0)
    best_trophies = p_data.get("bestTrophies", 0)
    exp = p_data.get("expLevel", 0)
    role = p_data.get("role", "member").capitalize()
    donations = p_data.get("donations", 0)
    donations_rec = p_data.get("donationsReceived", 0)
    war_stars = p_data.get("warStars", 0)
    raw_league = p_data.get("league", {}).get("name", "Unranked")
    if raw_league == "Legend League":
        if trophies >= 5400:
            league_info = "Legend League I"
        elif trophies >= 5200:
            league_info = "Legend League II"
        else:
            league_info = "Legend League III"
    else:
        league_info = raw_league

    text = (
        f"👤 Інформація про гравця: {p_name}\n"
        f"🏷 Тег: {p_data.get('tag')}\n"
        f"🏰 Ратуша (TH): {th}\n"
        f"⭐ Рівень: {exp}\n"
        f"🛡 Посада в клані: {role}\n\n"
        f"🏆 Кубки: {trophies} (Рекорд: {best_trophies})\n"
        f"🏅 Ліга: {league_info}\n"
        f"⚔️ Зірки на війні: {war_stars}\n\n"
        f"🤲 Донат: {donations} / Отримано: {donations_rec}"
    )
    
    random_images = ["1.jpg", "2.jpg", "3.jpg","4.jpg","5.jpg","6.jpg","7.jpg","8.jpg","9.jpg","10.jpg","11.jpg","12.jpg"]
    chosen_image = random.choice(random_images)
    
    photo = FSInputFile(chosen_image)
    await msg.answer_photo(
        photo=photo,
        caption=text,
        parse_mode="HTML"
    )

# === ВІТАЛЬНИЙ ОБРОБНИК ===

@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def welcome_new_chat_member(event: ChatMemberUpdated):
    user_id = event.new_chat_member.user.id
    user_name = event.new_chat_member.user.first_name
    
    mention = f'<a href="tg://user?id={user_id}">{user_name}</a>'
    
    welcome_text = (
        f"Привіт, {mention}! 👋 Вітаємо у нашому чаті! 🏰\n\n"
        f"Будь ласка, прив'яжи свій Telegram до ігрового профілю в Clash of Clans.\n"
        f"Для цього напиши команду:\n"
        f"<code>/link #ТВІЙ_ТЕГ</code> (наприклад, <code>/link #2ABC123</code>)"
    )
    
    await send_to_topic(event.chat.id, welcome_text)

# === АВТО-СПОВІЩЕННЯ У ГІЛКУ ===

async def send_to_topic(chat_id: int, text: str, photo: str | None = None):
    if photo:
        try:
            photo_file = FSInputFile(photo)
            await bot.send_photo(
                chat_id=chat_id,
                message_thread_id=THREAD_ID,
                photo=photo_file,
                caption=text,
                parse_mode=ParseMode.HTML
            )
            return
        except Exception as e:
            logging.error(f"Не вдалося надіслати фото {photo}: {e}")

    await bot.send_message(
        chat_id=chat_id,
        message_thread_id=THREAD_ID,
        text=text,
        parse_mode=ParseMode.HTML
    )

async def check_war_events(chat_id: int):
    # ================= 1. ЗВИЧАЙНЕ КВ =================
    war = get_clash_data(f"clans/{ENCODED_TAG}/currentwar")
    if war and "state" in war:
        state = war["state"]

        # Початок війни
        if state == "inWar" and bot_state.get("last_war_state") == "preparation":
            opponent = war.get("opponent", {}).get("name", "ворога")
            await send_to_topic(
                chat_id,
                f"Почалася війна з <b>{opponent}</b> ⚔️\nНе забувайте зробити 2 атаки. Всім успіхів в атаках 💖\nІ нехай удача завжди буде з вами 🙋‍♂️ 💙",
                photo="24.jpg"
            )
            bot_state["war_3h_reminded"] = False
            save_json(STATE_FILE, bot_state)

        # Нагадування за 3 години
        if state == "inWar":
            end_time = parse_coc_time(war["endTime"])
            now = datetime.now(timezone.utc)
            hours_left = (end_time - now).total_seconds() / 3600

            if 0 < hours_left <= 3.5 and not bot_state.get("war_3h_reminded", False):
                clan_data = war.get("clan", {})
                unattacked = []
                for member in clan_data.get("members", []):
                    attacks = member.get("attacks", [])
                    cnt = len(attacks)
                    if cnt < 2:
                        mention = format_mention(member["tag"], member["name"])
                        unattacked.append(f"• {mention} ({cnt}/2)")

                if unattacked:
                    names_str = "\n".join(unattacked)
                    await send_to_topic(
                        chat_id,
                        f"⚠️ <b>Залишилося 3 години до кінця КВ!</b> 🕛\n\n"
                        f"Гравці, які ще не зробили всі атаки:\n\n{names_str}\n\n"
                        f"Зробіть, будь ласка, свої атаки! ⚔️"
                    )
                bot_state["war_3h_reminded"] = True
                save_json(STATE_FILE, bot_state)

        # Закінчення звичайного КВ
        if state == "warEnded" and bot_state.get("last_war_state") == "inWar":
            opponent_name = war.get("opponent", {}).get("name", "ворогом")
            my_stars = war.get("clan", {}).get("stars", 0)
            op_stars = war.get("opponent", {}).get("stars", 0)
            
            my_dest = war.get("clan", {}).get("destructionPercentage", 0)
            op_dest = war.get("opponent", {}).get("destructionPercentage", 0)

            if my_stars > op_stars:
                res_text = "перемогли 💪🏆"
            elif my_stars < op_stars:
                res_text = "програли 💔"
            else:
                if my_dest > op_dest:
                    res_text = f"перемогли за відсотками ({my_dest:.1f}% проти {op_dest:.1f}%) 💪🏆"
                elif my_dest < op_dest:
                    res_text = f"програли за відсотками ({my_dest:.1f}% проти {op_dest:.1f}%) 💔"
                else:
                    res_text = "зіграли в НІЧИЮ 🤝⚖️"
            
            clan_members = war.get("clan", {}).get("members", [])
            not_full = []
            for m in clan_members:
                attacks = m.get("attacks", [])
                cnt = len(attacks)
                stars = sum(a.get("stars", 0) for a in attacks)
                record_player_stats(m.get("tag"), m.get("name"), "cw", cnt, 2)
                if cnt < 2:
                    mention = format_mention(m["tag"], m["name"])
                    not_full.append(f"• {mention} - {cnt}/2 атак ⚔️, {stars} ⭐")
            
            msg_text = (
                f"🏁 Війна проти «<b>{opponent_name}</b>» закінчена.\n"
                f"Ми {res_text}!\n"
                f"⭐ Рахунок: <b>{my_stars}</b> — <b>{op_stars}</b>\n\n"
            )
            if not_full:
                msg_text += "⚠️ <b>Гравці, які не зробили всі атаки:</b>\n" + "\n".join(not_full)
            else:
                msg_text += "🎉 Усі зробили свої атаки! Молодці!"
                
            await send_to_topic(chat_id, msg_text, photo="25.jpg")

        # Зберігаємо стан звичайного КВ
        bot_state["last_war_state"] = state
        save_json(STATE_FILE, bot_state)

    # ================= 2. ЛІГА ВІЙН КЛАНІВ (CWL) =================
    cwl_group = get_clash_data(f"clans/{ENCODED_TAG}/currentwar/leaguegroup")
    if cwl_group and cwl_group.get("state") == "inWar":
        rounds = cwl_group.get("rounds", [])
        for r in rounds:
            war_tags = r.get("warTags", [])
            for w_tag in war_tags:
                if w_tag == "#0":
                    continue
                
                clean_wtag = urllib.parse.quote(w_tag)
                c_war = get_clash_data(f"clanwarleagues/wars/{clean_wtag}")
                
                if not c_war or c_war.get("clan", {}).get("tag") != CLAN_TAG:
                    continue

                state = c_war.get("state")
                round_id = c_war.get("endTime")

                if state == "inWar":
                    end_time = parse_coc_time(c_war.get("endTime"))
                    now = datetime.now(timezone.utc)
                    hours_left = (end_time - now).total_seconds() / 3600

                    cwl_start_key = f"cwl_announced_{round_id}"
                    if not bot_state.get(cwl_start_key, False):
                        opp_name = c_war.get("opponent", {}).get("name", "суперника")
                        await send_to_topic(
                            chat_id,
                            f"🏆 <b>Розпочався новий день ЛВК проти «{opp_name}»!</b> ⚔️\n\n"
                            f"Не забувайте зробити свою 1 вирішальну атаку! Успіхів та 3 зірок кожному! 🛡️✨"
                        )
                        bot_state[cwl_start_key] = True
                        save_json(STATE_FILE, bot_state)

                    if 0 < hours_left <= 3.5:
                        if bot_state.get("last_cwl_round") != round_id:
                            clan_data = c_war.get("clan", {})
                            unattacked = []
                            for member in clan_data.get("members", []):
                                attacks = member.get("attacks", [])
                                if len(attacks) < 1:
                                    mention = format_mention(member["tag"], member["name"])
                                    unattacked.append(f"• {mention} (0/1)")

                            if unattacked:
                                names_str = "\n".join(unattacked)
                                await send_to_topic(
                                    chat_id,
                                    f"🏆 <b>Залишилося 3 години до кінця раунду ЛВК!</b> 🕛\n\n"
                                    f"Гравці, які ще не зробили атаку:\n\n{names_str}\n\n"
                                    f"Зробіть, будь ласка, свій бій за клан! ⚔️"
                                )
                            bot_state["last_cwl_round"] = round_id
                            save_json(STATE_FILE, bot_state)

                elif state == "warEnded":
                    cwl_ended_key = f"cwl_ended_{round_id}"
                    if not bot_state.get(cwl_ended_key, False):
                        opp_name = c_war.get("opponent", {}).get("name", "суперника")
                        clan_stars = c_war.get("clan", {}).get("stars", 0)
                        opp_stars = c_war.get("opponent", {}).get("stars", 0)

                        if clan_stars > opp_stars:
                            result_text = "🎉 <b>Ми перемогли!</b> 🏆"
                        elif clan_stars < opp_stars:
                            result_text = "💔 <b>На жаль, ми програли...</b> ⚔️"
                        else:
                            result_text = "🤝 <b>Нічия!</b> ⚔️"

                        unattacked = []
                        for member in c_war.get("clan", {}).get("members", []):
                            p_tag = member.get("tag")
                            p_name = member.get("name")
                            attacks = member.get("attacks", [])
                            att_cnt = len(attacks)
                            record_player_stats(p_tag, p_name, "cwl", att_cnt, 1)
                            if len(attacks) < 1:
                                mention = format_mention(member["tag"], member["name"])
                                unattacked.append(f"• {mention}")

                        if unattacked:
                            missed_str = "⚠️ <b>Атаку не зробили:</b>\n" + "\n".join(unattacked)
                        else:
                            missed_str = "🌟 <b>Усі учасники зробили свої атаки! Чудова робота!</b>"

                        await send_to_topic(
                            chat_id,
                            f"🏁 <b>Раунд ЛВК проти «{opp_name}» завершено!</b>\n\n"
                            f"{result_text}\n"
                            f"⭐ Рахунок: <b>{clan_stars}</b> — <b>{opp_stars}</b>\n\n"
                            f"{missed_str}"
                        )
                        bot_state[cwl_ended_key] = True
                        save_json(STATE_FILE, bot_state)

    # ================= 3. ПІДСУМОК УСІЄЇ ЛІГИ (CWL) =================
    if cwl_group and cwl_group.get("state") == "ended":
        cwl_season = cwl_group.get("season")
        league_ended_key = f"cwl_season_summary_{cwl_season}"

        if not bot_state.get(league_ended_key, False):
            stats = {}

            for r in cwl_group.get("rounds", []):
                for w_tag in r.get("warTags", []):
                    if w_tag == "#0":
                        continue
                    clean_wtag = urllib.parse.quote(w_tag)
                    c_war = get_clash_data(f"clanwarleagues/wars/{clean_wtag}")
                    
                    if c_war and c_war.get("clan", {}).get("tag") == CLAN_TAG:
                        for m in c_war.get("clan", {}).get("members", []):
                            tag = m.get("tag")
                            name = m.get("name")
                            if tag not in stats:
                                stats[tag] = {"name": name, "stars": 0, "destruction": 0, "attacks": 0}
                            
                            for att in m.get("attacks", []):
                                stats[tag]["stars"] += att.get("stars", 0)
                                stats[tag]["destruction"] += att.get("destructionPercentage", 0)
                                stats[tag]["attacks"] += 1

            sorted_stats = sorted(
                stats.values(), 
                key=lambda x: (x["stars"], x["destruction"]), 
                reverse=True
            )

            lines = []
            for idx, p in enumerate(sorted_stats, start=1):
                avg_dest = round(p["destruction"] / p["attacks"]) if p["attacks"] > 0 else 0
                lines.append(
                    f"{idx}. <b>{p['name']}</b> — ⭐ <b>{p['stars']}</b> "
                    f"({p['attacks']}/7 атак, avg {avg_dest}%)"
                )

            summary_text = (
                f"🏆 <b>Підсумки Ліги Війн Кланів (Сезон {cwl_season})!</b> 🏁\n\n"
                f"📊 <b>Топи за весь сезон:</b>\n\n" + "\n".join(lines) + "\n\n"
                f"Дякуємо всім за активну участь у ЛВК! 💪🔥"
            )

            await send_to_topic(chat_id, summary_text)
            bot_state[league_ended_key] = True
            save_json(STATE_FILE, bot_state)

async def check_raid_events(chat_id: int):
    raids = get_clash_data(f"clans/{ENCODED_TAG}/capitalraidseasons")
    if not raids or "items" not in raids or not raids["items"]:
        return

    current = raids["items"][0]
    state = current.get("state")

    if state == "ongoing" and bot_state.get("last_raid_state") != "ongoing":
        await send_to_topic(
            chat_id,
            "Добрий ранок, любі друзі. ☺️ Почалися Рейди! 🏹🗡️\nАтакуйте, робіть 6 атак і не залишайте недобиті регіони іншим! 🐶"
        )
        bot_state["raid_24h_reminded"] = False

    if state == "ongoing":
        end_time = parse_coc_time(current["endTime"])
        now = datetime.now(timezone.utc)
        hours_left = (end_time - now).total_seconds() / 3600
        
        if 0 < hours_left <= 24.5 and not bot_state.get("raid_24h_reminded", False):
            unfinished = []
            for m in current.get("members", []):
                cnt = m.get("attacks", 0)
                if isinstance(cnt, dict):
                    cnt = cnt.get("count", 0)
                limit = m.get("attackLimit", 5) + m.get("bonusAttackLimit", 0)
                if cnt < limit:
                    unfinished.append(f"{format_mention(m['tag'], m['name'])} {cnt}/{limit} ⚔️")
            
            if unfinished:
                names = ", ".join(unfinished)
                await send_to_topic(
                    chat_id,
                    f"Шановні {names} зробіть, будь ласка, атаки в рейдах 😊🗡️"
                )
            bot_state["raid_24h_reminded"] = True

    if state == "ended" and bot_state.get("last_raid_state") == "ongoing":
        total_loot = current.get("capitalTotalLoot", 0)
        raids_completed = current.get("raidsCompleted", 0)
        raid_members = {m.get("tag"): m for m in current.get("members", [])}

        clan_data = get_clash_data(f"clans/{ENCODED_TAG}")
        all_clan_members = clan_data.get("memberList", []) if clan_data else []

        unfinished = []
        total_attacks = 0

        for clan_m in all_clan_members:
            tag = clan_m.get("tag", "")
            name = clan_m.get("name", "Гравець")

            if tag in raid_members:
                m = raid_members[tag]
                cnt = m.get("attacks", 0)
                if isinstance(cnt, dict):
                    cnt = cnt.get("count", 0)
                limit = m.get("attackLimit", 5) + m.get("bonusAttackLimit", 0)
            else:
                cnt = 0
                limit = 6

            total_attacks += cnt
            record_player_stats(tag, name, "raid", cnt, limit)

            if cnt < limit:
                unfinished.append(f"• {format_mention(tag, name)} — {cnt}/{limit} ⚔️")

        text = "🏹 <b>Рейди закінчилися! Підбиваємо підсумки:</b>\n\n"
        text += f"💰 <b>Всього добуто золота:</b> {total_loot:,}\n"
        text += f"⚔️ <b>Всього зроблено атак:</b> {total_attacks}\n"
        text += f"🏰 <b>Знищено ворожих районів:</b> {raids_completed}\n\n"

        if unfinished:
            text += f"⚠️ <b>Гравці, які не зробили всі атаки ({len(unfinished)}):</b>\n" + "\n".join(unfinished)
        else:
            text += "🎉 Усі учасники зробили максимум атак!"

        await send_to_topic(chat_id, text)

    bot_state["last_raid_state"] = state
    save_json(STATE_FILE, bot_state)

async def check_clan_games(chat_id: int):
    now = datetime.now(timezone.utc)
    if now.day == 22 and 8 <= now.hour <= 10 and not bot_state.get("clan_games_reminded", False):
        await send_to_topic(
            chat_id,
            "Доброго ранку, шановні 💚 Почалися Ігри Кланів! ⚽\nНабийте, будь ласка, 4к очок, аби отримати додаткову нагороду 🎁\nВсім успіхів 💜"
        )
        bot_state["clan_games_reminded"] = True
        save_json(STATE_FILE, bot_state)
    elif now.day != 22:
        bot_state["clan_games_reminded"] = False
        save_json(STATE_FILE, bot_state)

async def is_youtube_shorts(video_id: str) -> bool:
    async with aiohttp.ClientSession() as session:
        url = f"https://www.youtube.com/shorts/{video_id}"
        async with session.head(url, allow_redirects=False) as resp:
            return resp.status == 200

async def check_youtube_news():
    now = datetime.now()
    
    # Працюємо тільки з 8:00 до 20:00
    if not (8 <= now.hour < 20):
        return

    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
    try:
        feed = feedparser.parse(rss_url)
        if not feed.entries:
            return

        latest_video = feed.entries[0]
        video_id = latest_video.yt_videoid
        video_title = latest_video.title
        video_link = latest_video.link
        video_summary = latest_video.get("summary", "")

        if bot_state.get("last_youtube_video_id") != video_id:
            
            if await is_youtube_shorts(video_id):
                bot_state["last_youtube_video_id"] = video_id
                save_json(STATE_FILE, bot_state)
                return

            army_match = re.search(r'https://link\.clashofclans\.com/[^\s"]+action=CopyArmy[^\s"]*', video_summary)
            
            builder = InlineKeyboardBuilder()
            hint_text = ""
            
            if army_match:
                army_link = army_match.group(0)
                builder.button(text="⚔️ Скопіювати мікс у гру", url=army_link)
                hint_text = "\n\n💡 <i>У описі знайдено мікс! Натискай кнопку нижче, щоб завантажити його в гру.</i>"
            
            builder.button(text="📺 Дивитися відео", url=video_link)
            builder.adjust(1)

            text = (
                f"🎬 <b>Нове відео від SALOMON!</b>\n\n"
                f"📌 <b>{video_title}</b>"
                f"{hint_text}"
            )

            chat_id = CHAT_ID
            if chat_id:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    message_thread_id=NEWS_THREAD_ID,
                    parse_mode="HTML",
                    reply_markup=builder.as_markup()
                )
                bot_state["last_youtube_video_id"] = video_id
                save_json(STATE_FILE, bot_state)

    except Exception as e:
        logging.error(f"Помилка YouTube RSS: {e}")

async def background_checker():
    while True:
        try:
            target_id = CHAT_ID
            if target_id and target_id != 0:
                await check_war_events(target_id)
                await check_raid_events(target_id)
                await check_clan_games(target_id)
                await check_youtube_news()
                
                now = datetime.now()
                if now.weekday() == 0 and now.hour == 14 and now.minute < 5:
                    report_text = await process_weekly_league_report()
                    await bot.send_message(
                        target_id,
                        report_text,
                        message_thread_id=THREAD_ID,
                        parse_mode=ParseMode.MARKDOWN
                    )
        except Exception as e:
            logging.error(f"Помилка фонової перевірки: {e}")
        await asyncio.sleep(300)

@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    await msg.answer("📊 Збираю дані про пропущені атаки за місяць, зачекайте...")

    # 1. Склад клану
    clan_data = get_clash_data(f"clans/{ENCODED_TAG}/members")
    if not clan_data or "items" not in clan_data:
        await msg.answer("⚠️ Не вдалося отримати список учасників клану.")
        return

    # Ініціалізуємо лічильники для кожного гравця
    stats = {}
    for m in clan_data["items"]:
        stats[m["tag"]] = {
            "name": m["name"],
            "missed_cw": 0,
            "missed_cwl": 0,
            "missed_raid": 0
        }

    # 2. Перевірка звичайних КВ (War Log)
    warlog = get_clash_data(f"clans/{ENCODED_TAG}/warlog")
    if warlog and "items" in warlog:
        # Беремо останні КВ за місяць (наприклад, до 10 останніх війн)
        for entry in warlog.get("items", [])[:10]:
            # Якщо є детальні дані про війну
            if "clan" in entry and "members" in entry["clan"]:
                for m in entry["clan"]["members"]:
                    tag = m.get("tag")
                    if tag in stats:
                        attacks_done = len(m.get("attacks", []))
                        # У звичайній КВ дається 2 атаки
                        if attacks_done < 2:
                            stats[tag]["missed_cw"] += (2 - attacks_done)

    # 3. Перевірка ЛВК (CWL)
    cwl_group = get_clash_data(f"clans/{ENCODED_TAG}/currentwar/leaguegroup")
    if cwl_group and "rounds" in cwl_group:
        for r in cwl_group.get("rounds", []):
            for war_tag in r.get("warTags", []):
                if war_tag == "#0":
                    continue
                war_data = get_clash_data(f"clanwarleagues/wars/{ENCODED_TAG_URL(war_tag)}")
                if not war_data:
                    continue
                
                # Визначаємо, де наш клан (clan чи opponent)
                our_clan = None
                if war_data.get("clan", {}).get("tag") == f"#{ENCODED_TAG}":
                    our_clan = war_data["clan"]
                elif war_data.get("opponent", {}).get("tag") == f"#{ENCODED_TAG}":
                    our_clan = war_data["opponent"]

                if our_clan and "members" in our_clan:
                    for m in our_clan["members"]:
                        tag = m.get("tag")
                        if tag in stats:
                            # В ЛВК дається 1 атака на день
                            if len(m.get("attacks", [])) == 0:
                                stats[tag]["missed_cwl"] += 1

    # 4. Перевірка Рейд-вікендів (останні 4 тижні)
    raid_data = get_clash_data(f"clans/{ENCODED_TAG}/capitalraidseasons")
    if raid_data and "items" in raid_data:
        for raid in raid_data.get("items", [])[:4]:
            for m in raid.get("members", []):
                tag = m.get("tag")
                if tag in stats:
                    attacks_done = m.get("attacks", 0)
                    limit = m.get("attackLimit", 5) + m.get("bonusAttackLimit", 0)
                    if attacks_done < limit:
                        stats[tag]["missed_raid"] += (limit - attacks_done)

    # 5. Формування звіту
    debtors = []
    for tag, p in stats.items():
        total_missed = p["missed_cw"] + p["missed_cwl"] + p["missed_raid"]
        if total_missed > 0:
            debtors.append((total_missed, p))

    if not debtors:
        await msg.answer("🎉 **Ідеально!** За останній місяць жоден гравець не пропустив жодної атаки.")
        return

    # Сортуємо: зверху ті, у кого найбільше пропусків
    debtors.sort(key=lambda x: x[0], reverse=True)

    lines = ["📊 **Статистика пропущених атак за місяць:**\n"]
    for _, p in debtors:
        details = []
        if p["missed_cw"] > 0:
            details.append(f"⚔️ КВ: **{p['missed_cw']}**")
        if p["missed_cwl"] > 0:
            details.append(f"🏆 ЛВК: **{p['missed_cwl']}**")
        if p["missed_raid"] > 0:
            details.append(f"🏰 Рейди: **{p['missed_raid']}**")

        lines.append(f"• **{p['name']}**: " + ", ".join(details))

    await msg.answer("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("raidstats"))
async def cmd_raidstats(msg: types.Message):
    data = get_clash_data(f"clans/{ENCODED_TAG}/capitalraidseasons")
    if not data or "items" not in data or not data["items"]:
        await msg.answer("⚠️ Не вдалося отримати дані про рейди від Supercell.")
        return

    last_raid = data["items"][0]
    total_loot = last_raid.get("capitalTotalLoot", 0)
    raids_completed = last_raid.get("raidsCompleted", 0)
    raid_members = {m.get("tag"): m for m in last_raid.get("members", [])}

    clan_data = get_clash_data(f"clans/{ENCODED_TAG}")
    all_clan_members = clan_data.get("memberList", []) if clan_data else []

    unfinished = []
    total_attacks = 0

    for clan_m in all_clan_members:
        tag = clan_m.get("tag", "")
        name = clan_m.get("name", "Гравець")
        
        if tag in raid_members:
            m = raid_members[tag]
            used = m.get("attacks", 0)
            if isinstance(used, dict):
                used = used.get("count", 0)
            limit = m.get("attackLimit", 5) + m.get("bonusAttackLimit", 0)
        else:
            used = 0
            limit = 6

        total_attacks += used

        if used < limit:
            unfinished.append(f"• {format_mention(tag, name)} — {used}/{limit} ⚔️")

    text = f"🏹 <b>Підсумки останнього рейду:</b>\n\n"
    text += f"💰 <b>Всього добуто золота:</b> {total_loot:,}\n"
    text += f"⚔️ <b>Всього зроблено атак:</b> {total_attacks}\n"
    text += f"🏰 <b>Знищено ворожих районів:</b> {raids_completed}\n\n"

    if unfinished:
        text += f"⚠️ <b>Гравці, які не зробили всі атаки ({len(unfinished)}):</b>\n" + "\n".join(unfinished)
    else:
        text += "🎉 Усі учасники зробили максимум атак!"

    await msg.answer(text, parse_mode="HTML")

async def main():
    logging.basicConfig(level=logging.INFO)
    asyncio.create_task(background_checker())
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())