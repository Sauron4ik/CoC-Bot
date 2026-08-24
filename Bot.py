import asyncio
import html
import json
import logging
import os
import random
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import ChatMemberUpdatedFilter, Command, JOIN_TRANSITION
from aiogram.types import (
    ChatMemberUpdated,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
COC_TOKEN = os.getenv("COC_TOKEN")
CLAN_TAG = os.getenv("CLAN_TAG", "#2PGVU889Q").strip().upper()
if not CLAN_TAG.startswith("#"):
    CLAN_TAG = "#" + CLAN_TAG

CHAT_ID = int(os.getenv("CHAT_ID", "0"))
THREAD_ID = int(os.getenv("THREAD_ID", "14128"))
NEWS_THREAD_ID = int(os.getenv("NEWS_THREAD_ID", "4026"))
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "UCekeHLY2xpfjcGq_3gHFaTA")
BOT_TIMEZONE_NAME = os.getenv("BOT_TIMEZONE", "Europe/Kyiv")
CHECK_INTERVAL_SECONDS = max(60, int(os.getenv("CHECK_INTERVAL_SECONDS", "300")))
HTTP_TIMEOUT_SECONDS = max(5, int(os.getenv("HTTP_TIMEOUT_SECONDS", "12")))

if not TG_TOKEN or not COC_TOKEN:
    raise ValueError("⚠️ Помилка: TG_TOKEN або COC_TOKEN не знайдено у файлі .env!")

try:
    BOT_TIMEZONE = ZoneInfo(BOT_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    logging.warning("Невідомий часовий пояс %s, використовую UTC", BOT_TIMEZONE_NAME)
    BOT_TIMEZONE = timezone.utc

ENCODED_TAG = urllib.parse.quote(CLAN_TAG, safe="")

BASE_DIR = Path(__file__).resolve().parent
PLAYERS_FILE = BASE_DIR / "players.json"
STATE_FILE = BASE_DIR / "bot_state.json"
LEAGUES_FILE = BASE_DIR / "players_leagues.json"
HISTORY_FILE = BASE_DIR / "history.json"

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()


# ============================================================
# JSON / STATE HELPERS
# ============================================================


def load_json(filepath: Path, default: Any) -> Any:
    try:
        if filepath.exists():
            with filepath.open("r", encoding="utf-8") as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logging.error("Не вдалося прочитати %s: %s", filepath.name, exc)
    return default


def save_json(filepath: Path, data: Any) -> None:
    """Atomic-ish save: write temp file, then replace original."""
    tmp = filepath.with_suffix(filepath.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, filepath)
    except OSError as exc:
        logging.error("Не вдалося зберегти %s: %s", filepath.name, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


player_links: dict[str, int | str] = load_json(PLAYERS_FILE, {})
bot_state: dict[str, Any] = load_json(STATE_FILE, {})


def state_once(key: str) -> bool:
    return bool(bot_state.get(key, False))


def mark_state(key: str, value: Any = True) -> None:
    bot_state[key] = value
    save_json(STATE_FILE, bot_state)


def record_event_stats(
    event_type: str,
    rows: list[tuple[str, str, int, int]],
    event_time: Optional[datetime] = None,
) -> None:
    """Record one completed event in history.json with a single disk write."""
    history = load_json(HISTORY_FILE, {})
    dt = event_time or datetime.now(timezone.utc)
    month_key = dt.astimezone(BOT_TIMEZONE).strftime("%Y-%m")
    month = history.setdefault(month_key, {})

    for player_tag, player_name, attacks_done, attacks_max in rows:
        if not player_tag:
            continue

        p = month.setdefault(
            player_tag,
            {
                "name": player_name or "Гравець",
                "cw_done": 0,
                "cw_missed": 0,
                "cwl_done": 0,
                "cwl_missed": 0,
                "raid_done": 0,
                "raid_missed": 0,
            },
        )
        p["name"] = player_name or p.get("name", "Гравець")
        missed = max(0, attacks_max - attacks_done)

        if event_type == "cw":
            p["cw_done"] = p.get("cw_done", 0) + attacks_done
            p["cw_missed"] = p.get("cw_missed", 0) + missed
        elif event_type == "cwl":
            p["cwl_done"] = p.get("cwl_done", 0) + attacks_done
            p["cwl_missed"] = p.get("cwl_missed", 0) + missed
        elif event_type == "raid":
            p["raid_done"] = p.get("raid_done", 0) + attacks_done
            p["raid_missed"] = p.get("raid_missed", 0) + missed

    save_json(HISTORY_FILE, history)


# ============================================================
# CLASH OF CLANS API
# ============================================================


class ClashClient:
    def __init__(self, token: str):
        self.token = token
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def get(self, endpoint: str) -> Optional[dict[str, Any]]:
        await self.start()
        assert self.session is not None

        url = f"https://api.clashofclans.com/v1/{endpoint}"
        headers = {"Authorization": f"Bearer {self.token}"}

        for attempt in range(3):
            try:
                async with self.session.get(url, headers=headers) as res:
                    if res.status == 200:
                        return await res.json()
                    if res.status == 404:
                        return None

                    body = await res.text()
                    if res.status == 429 or 500 <= res.status < 600:
                        wait_seconds = 1 + attempt * 2
                        logging.warning(
                            "CoC API [%s], повтор через %sс: %s",
                            res.status,
                            wait_seconds,
                            body[:300],
                        )
                        await asyncio.sleep(wait_seconds)
                        continue

                    logging.error("CoC API [%s]: %s", res.status, body[:500])
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == 2:
                    logging.error("Помилка з'єднання з CoC API: %s", exc)
                    return None
                await asyncio.sleep(1 + attempt * 2)

        return None


clash = ClashClient(COC_TOKEN)


def encode_tag(tag: str) -> str:
    return urllib.parse.quote(tag.strip().upper(), safe="")


def normalize_tag(tag: str) -> str:
    tag = urllib.parse.unquote(tag.strip()).upper()
    if not tag.startswith("#"):
        tag = "#" + tag
    return tag


def parse_coc_time(time_str: Optional[str]) -> Optional[datetime]:
    if not time_str:
        return None
    clean = time_str.strip().rstrip("Z").split(".")[0]
    try:
        return datetime.strptime(clean, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        logging.warning("Невідомий формат часу CoC: %s", time_str)
        return None


def our_and_opponent(war: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    clan = war.get("clan", {}) or {}
    opponent = war.get("opponent", {}) or {}

    if clan.get("tag") == CLAN_TAG:
        return clan, opponent
    if opponent.get("tag") == CLAN_TAG:
        return opponent, clan
    return None, None


def war_result_text(our_clan: dict[str, Any], opp_clan: dict[str, Any]) -> str:
    our_stars = our_clan.get("stars", 0)
    opp_stars = opp_clan.get("stars", 0)
    our_dest = float(our_clan.get("destructionPercentage", 0) or 0)
    opp_dest = float(opp_clan.get("destructionPercentage", 0) or 0)

    if our_stars > opp_stars or (our_stars == opp_stars and our_dest > opp_dest):
        return "🎉 <b>Ми перемогли!</b> 🏆"
    if our_stars < opp_stars or (our_stars == opp_stars and our_dest < opp_dest):
        return "💔 <b>На жаль, ми програли...</b> ⚔️"
    return "🤝 <b>Нічия!</b> ⚔️"


# ============================================================
# TEXT / TELEGRAM HELPERS
# ============================================================


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def format_mention(tag: str, name: str) -> str:
    tag_clean = (tag or "").upper()
    safe_name = esc(name or "Гравець")
    user_ref = player_links.get(tag_clean)

    if isinstance(user_ref, int) or (isinstance(user_ref, str) and user_ref.isdigit()):
        return f'<a href="tg://user?id={user_ref}">{safe_name}</a>'
    if isinstance(user_ref, str) and user_ref.startswith("@"):
        return esc(user_ref)
    return safe_name


def topic_kwargs(thread_id: int) -> dict[str, int]:
    return {"message_thread_id": thread_id} if thread_id else {}


async def send_to_topic(chat_id: int, text: str, photo: Optional[str] = None) -> None:
    kwargs = topic_kwargs(THREAD_ID)

    if photo:
        photo_path = BASE_DIR / photo
        if photo_path.exists():
            try:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=FSInputFile(photo_path),
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    **kwargs,
                )
                return
            except Exception as exc:
                logging.error("Не вдалося надіслати фото %s: %s", photo, exc)

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        **kwargs,
    )


async def answer_html_lines(msg: types.Message, lines: list[str], limit: int = 3900) -> None:
    """Send long HTML safely by splitting only between complete lines."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    for chunk in chunks:
        await msg.answer(chunk, parse_mode=ParseMode.HTML)


async def is_admin(message: types.Message, user_id: Optional[int] = None) -> bool:
    uid = user_id or message.from_user.id
    chat_id = message.chat.id if message.chat.type in {"group", "supergroup"} else CHAT_ID
    if not chat_id:
        return False

    try:
        member = await message.bot.get_chat_member(chat_id, uid)
        return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
    except Exception:
        return False


def recent_enough(event_time: Optional[datetime], hours: float) -> bool:
    if event_time is None:
        return True
    age = (datetime.now(timezone.utc) - event_time).total_seconds() / 3600
    return -0.25 <= age <= hours


# ============================================================
# WEEKLY LEAGUE REPORT
# ============================================================


def load_previous_leagues() -> dict[str, Any]:
    return load_json(LEAGUES_FILE, {})


def save_current_leagues(data: dict[str, Any]) -> None:
    save_json(LEAGUES_FILE, data)


async def process_weekly_league_report() -> str:
    data = await clash.get(f"clans/{ENCODED_TAG}")
    if not data or "memberList" not in data:
        return "❌ Не вдалося отримати дані клану."

    old_data = load_previous_leagues()
    new_data: dict[str, Any] = {}
    player_rows: list[str] = []

    for member in data["memberList"]:
        tag = member.get("tag", "")
        name = str(member.get("name", "Гравець"))
        raw_league = member.get("league", {}).get("name", "Unranked")
        trophies = int(member.get("trophies", 0) or 0)

        if raw_league == "Legend League":
            if trophies >= 5400:
                league_name = "Legend League I"
            elif trophies >= 5200:
                league_name = "Legend League II"
            else:
                league_name = "Legend League III"
        else:
            league_name = raw_league

        new_data[tag] = {"name": name, "league": league_name, "trophies": trophies}

        if tag in old_data:
            prev_league = old_data[tag].get("league", "Unranked")
            prev_trophies = int(old_data[tag].get("trophies", 0) or 0)
            diff = trophies - prev_trophies
            sign = f"+{diff}" if diff > 0 else str(diff)
            league_str = f"{prev_league} → {league_name}" if prev_league != league_name else league_name
            player_rows.append(f"{name}: {league_str} | {trophies} 🏆 ({sign})")
        else:
            player_rows.append(f"{name}: {league_name} | {trophies} 🏆 (новий гравець)")

    save_current_leagues(new_data)
    body = "\n".join(esc(row) for row in player_rows)
    return f"🏆 <b>Зміни у клані за бойовий тиждень:</b>\n\n<pre>{body}</pre>"


# ============================================================
# COMMANDS
# ============================================================


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    history = load_json(HISTORY_FILE, {})
    month_key = datetime.now(BOT_TIMEZONE).strftime("%Y-%m")
    month = history.get(month_key, {})

    if not month:
        await message.answer("📊 За цей місяць ще немає збереженої історії атак.")
        return

    lines = [f"📊 <b>Статистика атак за {month_key}:</b>", ""]
    for _, d in sorted(month.items(), key=lambda item: str(item[1].get("name", "")).lower()):
        lines.extend(
            [
                f"👤 <b>{esc(d.get('name', 'Гравець'))}</b>",
                f" ├ ⚔️ <b>КВ:</b> зроблено {d.get('cw_done', 0)} | ❌ пропущено {d.get('cw_missed', 0)}",
                f" ├ 🏆 <b>ЛВК:</b> зроблено {d.get('cwl_done', 0)} | ❌ пропущено {d.get('cwl_missed', 0)}",
                f" └ 🛡️ <b>Рейди:</b> зроблено {d.get('raid_done', 0)} | ❌ пропущено {d.get('raid_missed', 0)}",
                "",
            ]
        )
    await answer_html_lines(message, lines)


@dp.message(Command("missedstats"))
async def cmd_missed_stats(msg: types.Message):
    """Ranking of missed attacks using reliable bot-recorded history.

    The Clash war log does not expose old member attack details, so reconstructing
    missed normal-war attacks from the API after the fact would be inaccurate.
    """
    history = load_json(HISTORY_FILE, {})
    month_key = datetime.now(BOT_TIMEZONE).strftime("%Y-%m")
    month = history.get(month_key, {})

    debtors: list[tuple[int, dict[str, Any]]] = []
    for d in month.values():
        total = int(d.get("cw_missed", 0)) + int(d.get("cwl_missed", 0)) + int(d.get("raid_missed", 0))
        if total:
            debtors.append((total, d))

    if not debtors:
        await msg.answer("🎉 За цей місяць у збереженій історії немає пропущених атак.")
        return

    debtors.sort(key=lambda item: (item[0], str(item[1].get("name", ""))), reverse=True)
    lines = [f"📊 <b>Пропущені атаки за {month_key}:</b>", ""]
    for total, p in debtors:
        details = []
        if p.get("cw_missed", 0):
            details.append(f"⚔️ КВ: <b>{p['cw_missed']}</b>")
        if p.get("cwl_missed", 0):
            details.append(f"🏆 ЛВК: <b>{p['cwl_missed']}</b>")
        if p.get("raid_missed", 0):
            details.append(f"🏰 Рейди: <b>{p['raid_missed']}</b>")
        lines.append(f"• <b>{esc(p.get('name', 'Гравець'))}</b> ({total}): " + ", ".join(details))

    await answer_html_lines(msg, lines)


@dp.message(Command("start", "help"))
async def cmd_start(msg: types.Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚔️ Стан КВ", callback_data="btn_war"),
                InlineKeyboardButton(text="🏹 Рейди", callback_data="btn_raid"),
            ],
            [
                InlineKeyboardButton(text="🏆 CWL", callback_data="btn_cwl"),
                InlineKeyboardButton(text="📊 Тижневий звіт", callback_data="btn_weekly"),
            ],
        ]
    )

    text = (
        "Привіт! Я помічник Саурона 🏰\n\n"
        "Ось що ти можеш вибрати:\n"
        "• /link #ТЕГ_ГРАВЦЯ — прив'язати Telegram до акаунта\n"
        "• /unlink — видалити свої прив'язки\n"
        "• /listlinks — список прив'язок (адмінам)\n"
        "• /player нік_або_тег — картка гравця\n"
        "• /war — стан поточної КВ\n"
        "• /raid — стан рейд-вікенду\n"
        "• /raidstats — підсумки останнього рейду\n"
        "• /cwl — стан ЛВК\n"
        "• /stats — накопичена статистика за місяць\n"
        "• /missedstats — рейтинг пропущених атак\n"
        "• /weekly_report — звіт по кубках та лігах\n\n"
        "Обирай кнопками нижче або вводь команди вручну!"
    )

    photo = BASE_DIR / "23.jpg"
    if photo.exists():
        try:
            await msg.answer_photo(
                photo=FSInputFile(photo),
                caption=text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception as exc:
            logging.warning("Не вдалося відправити 23.jpg: %s", exc)

    await msg.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.in_({"btn_war", "btn_raid", "btn_cwl", "btn_weekly"}))
async def process_callback_buttons(callback: types.CallbackQuery):
    if callback.message is None:
        await callback.answer()
        return

    if callback.data == "btn_war":
        await cmd_war(callback.message)
    elif callback.data == "btn_raid":
        await cmd_raid(callback.message)
    elif callback.data == "btn_cwl":
        await cmd_cwl(callback.message)
    elif callback.data == "btn_weekly":
        report = await process_weekly_league_report()
        await callback.message.answer(report, parse_mode=ParseMode.HTML)

    await callback.answer()


@dp.message(Command("link"))
async def cmd_link(msg: types.Message):
    args = (msg.text or "").split()
    if len(args) < 2:
        await msg.answer("Вкажіть тег. Приклад: <code>/link #2ABC123</code>", parse_mode=ParseMode.HTML)
        return

    target_user = msg.from_user
    if msg.reply_to_message:
        if not await is_admin(msg):
            await msg.answer("❌ Прив'язувати акаунт іншому користувачу може лише адміністратор.")
            return
        target_user = msg.reply_to_message.from_user

    player_tag = normalize_tag(args[1])
    player_links[player_tag] = target_user.id
    save_json(PLAYERS_FILE, player_links)

    mention = f'<a href="tg://user?id={target_user.id}">{esc(target_user.first_name or "користувач")}</a>'
    caption = f"Чудово! Тег <code>{esc(player_tag)}</code> прив'язано до {mention} ✨"

    photo = BASE_DIR / "22.jpg"
    if photo.exists():
        try:
            await msg.answer_photo(FSInputFile(photo), caption=caption, parse_mode=ParseMode.HTML)
            return
        except Exception as exc:
            logging.warning("Не вдалося відправити 22.jpg: %s", exc)

    await msg.answer(caption, parse_mode=ParseMode.HTML)


@dp.message(Command("unlink"))
async def cmd_unlink(msg: types.Message):
    args = (msg.text or "").split()

    # Admin can unlink the user replied to.
    if msg.reply_to_message:
        if not await is_admin(msg):
            await msg.answer("❌ Видаляти прив'язки іншого користувача може лише адміністратор.")
            return
        target_uid = msg.reply_to_message.from_user.id
        found = [tag for tag, uid in player_links.items() if str(uid) == str(target_uid)]
        for tag in found:
            player_links.pop(tag, None)
        save_json(PLAYERS_FILE, player_links)
        await msg.answer("🗑 Усі прив'язки цього користувача видалено." if found else "❌ Прив'язок не знайдено.")
        return

    # Explicit tag: owner can unlink own tag; admin can unlink any tag.
    if len(args) > 1:
        tag = normalize_tag(args[1])
        owner = player_links.get(tag)
        if owner is None:
            await msg.answer("❌ Такої прив'язки не знайдено.")
            return
        if str(owner) != str(msg.from_user.id) and not await is_admin(msg):
            await msg.answer("❌ Це не ваша прив'язка.")
            return
        player_links.pop(tag, None)
        save_json(PLAYERS_FILE, player_links)
        await msg.answer(f"🗑 Прив'язку <code>{esc(tag)}</code> видалено.", parse_mode=ParseMode.HTML)
        return

    # No args: unlink all own tags.
    own_tags = [tag for tag, uid in player_links.items() if str(uid) == str(msg.from_user.id)]
    if not own_tags:
        await msg.answer("❌ У вас немає прив'язаних тегів.")
        return
    for tag in own_tags:
        player_links.pop(tag, None)
    save_json(PLAYERS_FILE, player_links)
    await msg.answer("🗑 Усі ваші прив'язки видалено.")


@dp.message(Command("listlinks"))
async def cmd_listlinks(msg: types.Message):
    if not await is_admin(msg):
        await msg.answer("❌ Ця команда доступна лише адміністраторам.")
        return

    if not player_links:
        await msg.answer("📋 Список прив'язаних ігрових акаунтів порожній.")
        return

    user_tags: dict[str, list[str]] = {}
    for tag, uid in player_links.items():
        user_tags.setdefault(str(uid), []).append(tag)

    lines = ["📋 <b>Список прив'язаних акаунтів:</b>", ""]
    for uid_str, tags in user_tags.items():
        display_name = f"ID: {uid_str}"
        try:
            uid = int(uid_str)
            lookup_chat = msg.chat.id if msg.chat.type in {"group", "supergroup"} else CHAT_ID
            if lookup_chat:
                member = await msg.bot.get_chat_member(lookup_chat, uid)
                user = member.user
                display_name = f"@{user.username}" if user.username else (user.first_name or display_name)
        except Exception:
            pass

        tags_str = ", ".join(f"<code>{esc(tag)}</code>" for tag in sorted(tags))
        lines.extend([f"👤 <b>{esc(display_name)}</b>:", f"└ Теги: {tags_str}", ""])

    await answer_html_lines(msg, lines)


@dp.message(Command("war"))
async def cmd_war(msg: types.Message):
    if msg.message_thread_id and THREAD_ID and msg.message_thread_id != THREAD_ID:
        return

    war = await clash.get(f"clans/{ENCODED_TAG}/currentwar")
    if not war:
        await msg.answer("❌ Не вдалося отримати дані про війну.")
        return

    state = war.get("state")
    if state == "notInWar":
        await msg.answer("⚔️ Клан зараз не перебуває у війні.")
        return

    our_clan, opp_clan = our_and_opponent(war)
    if not our_clan or not opp_clan:
        await msg.answer("❌ API повернув війну, але наш клан у ній не знайдено.")
        return

    opponent_name = esc(opp_clan.get("name", "Невідомо"))
    attack_limit = int(war.get("attacksPerMember", 2) or 2)

    if state == "preparation":
        await msg.answer(
            f"⏳ Триває день підготовки до війни проти «<b>{opponent_name}</b>»!",
            parse_mode=ParseMode.HTML,
        )
        return

    our_stars = our_clan.get("stars", 0)
    opp_stars = opp_clan.get("stars", 0)

    if state == "inWar":
        unattacked = []
        for member in our_clan.get("members", []):
            count = len(member.get("attacks", []))
            if count < attack_limit:
                unattacked.append(
                    f"• {format_mention(member.get('tag', ''), member.get('name', 'Гравець'))} — {count}/{attack_limit} ⚔️"
                )

        text = (
            f"⚔️ Ми воюємо з «<b>{opponent_name}</b>»!\n"
            f"⭐ Зірки: <b>{our_stars}</b> — <b>{opp_stars}</b>\n\n"
        )
        text += (
            "⚠️ <b>Ще не зробили всі атаки:</b>\n" + "\n".join(unattacked)
            if unattacked
            else f"🎉 Усі учасники зробили свої {attack_limit} атаки!"
        )
        await msg.answer(text, parse_mode=ParseMode.HTML)
        return

    if state == "warEnded":
        our_dest = float(our_clan.get("destructionPercentage", 0) or 0)
        opp_dest = float(opp_clan.get("destructionPercentage", 0) or 0)
        not_full = []
        for member in our_clan.get("members", []):
            attacks = member.get("attacks", [])
            count = len(attacks)
            stars = sum(int(a.get("stars", 0) or 0) for a in attacks)
            if count < attack_limit:
                not_full.append(
                    f"• {format_mention(member.get('tag', ''), member.get('name', 'Гравець'))} — {count}/{attack_limit} атак ⚔️ ({stars} ⭐)"
                )

        text = (
            f"🏁 Війна проти «<b>{opponent_name}</b>» завершилася.\n"
            f"{war_result_text(our_clan, opp_clan)}\n"
            f"⭐ Зірки: <b>{our_stars}</b> — <b>{opp_stars}</b>\n"
            f"💥 Руйнування: <b>{our_dest:.1f}%</b> — <b>{opp_dest:.1f}%</b>\n\n"
        )
        text += (
            "⚠️ <b>Не зробили всі атаки:</b>\n" + "\n".join(not_full)
            if not_full
            else "🎉 Усі зробили свої атаки! Молодці!"
        )
        await msg.answer(text, parse_mode=ParseMode.HTML)


@dp.message(Command("test_league"))
async def cmd_test_league(msg: types.Message):
    data = await clash.get(f"clans/{ENCODED_TAG}")
    if not data or not data.get("memberList"):
        await msg.answer("❌ Не вдалося отримати дані.")
        return
    first_member = data["memberList"][0]
    await msg.answer(
        f"📊 Дані ліги для <b>{esc(first_member.get('name', 'Гравець'))}</b>:\n"
        f"<code>{esc(first_member.get('league', {}))}</code>",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("weekly_report"))
async def cmd_weekly_report(msg: types.Message):
    report = await process_weekly_league_report()
    await msg.answer(report, parse_mode=ParseMode.HTML)


@dp.message(Command("raid"))
async def cmd_raid(msg: types.Message):
    data = await clash.get(f"clans/{ENCODED_TAG}/capitalraidseasons?limit=1")
    if not data or not data.get("items"):
        await msg.answer("⚠️ Не вдалося отримати дані про рейди від Supercell.")
        return

    current = data["items"][0]
    if current.get("state") != "ongoing":
        await msg.answer("⚔️ Наразі немає активного Рейд-вікенду.")
        return

    unfinished = []
    for member in current.get("members", []):
        used = member.get("attacks", 0)
        if isinstance(used, dict):
            used = used.get("count", 0)
        limit = int(member.get("attackLimit", 5) or 5) + int(member.get("bonusAttackLimit", 0) or 0)
        if used < limit:
            unfinished.append(
                f"• {format_mention(member.get('tag', ''), member.get('name', 'Гравець'))} — {used}/{limit} ⚔️"
            )

    text = "🏹 <b>Поточний стан рейду:</b>\n\n"
    text += (
        "Гравці, які ще не зробили всі атаки:\n" + "\n".join(unfinished)
        if unfinished
        else "Усі учасники рейду зробили свої атаки! 🎉"
    )
    await msg.answer(text, parse_mode=ParseMode.HTML)


async def find_current_cwl_war(group_data: dict[str, Any]) -> Optional[dict[str, Any]]:
    for round_data in reversed(group_data.get("rounds", [])):
        for war_tag in round_data.get("warTags", []):
            if war_tag == "#0":
                continue
            war = await clash.get(f"clanwarleagues/wars/{encode_tag(war_tag)}")
            if not war:
                continue
            our_clan, _ = our_and_opponent(war)
            if our_clan and war.get("state") in {"preparation", "inWar"}:
                return war
    return None


@dp.message(Command("cwl"))
async def cmd_cwl(msg: types.Message):
    if msg.message_thread_id and THREAD_ID and msg.message_thread_id != THREAD_ID:
        return

    group_data = await clash.get(f"clans/{ENCODED_TAG}/currentwar/leaguegroup")
    if not group_data or group_data.get("state") == "notInWar":
        await msg.answer("⚔️ Клан зараз не перебуває у Лізі Війн Кланів (CWL).")
        return

    war = await find_current_cwl_war(group_data)
    if not war:
        await msg.answer("📊 Активних раундів CWL наразі не знайдено.")
        return

    our_clan, opp_clan = our_and_opponent(war)
    if not our_clan or not opp_clan:
        await msg.answer("❌ Не вдалося визначити сторони поточного раунду CWL.")
        return

    opponent_name = esc(opp_clan.get("name", "Суперник"))
    if war.get("state") == "preparation":
        await msg.answer(
            f"⏳ <b>CWL:</b> триває підготовка до раунду проти <b>{opponent_name}</b>!",
            parse_mode=ParseMode.HTML,
        )
        return

    unattacked = []
    for member in our_clan.get("members", []):
        if not member.get("attacks", []):
            unattacked.append(format_mention(member.get("tag", ""), member.get("name", "Гравець")))

    text = (
        "🏆 <b>Ліга Війн Кланів (CWL)</b>\n"
        f"⚔️ Проти: <b>{opponent_name}</b>\n"
        f"⭐ Зірки: <b>{our_clan.get('stars', 0)}</b> — <b>{opp_clan.get('stars', 0)}</b>\n\n"
    )
    text += (
        "⚠️ <b>Ще не зробили атаку:</b>\n" + "\n".join(f"• {name}" for name in unattacked)
        if unattacked
        else "🎉 Усі учасники зробили свої атаки!"
    )
    await msg.answer(text, parse_mode=ParseMode.HTML)


@dp.message(Command("player"))
async def cmd_player_stats(msg: types.Message):
    args = (msg.text or "").split(maxsplit=1)
    if len(args) < 2:
        await msg.answer("❌ Вкажіть тег або ім'я гравця. Приклад:\n/player #QV2JL9G08 або /player Саурон")
        return

    query = args[1].strip()
    player_tag: Optional[str] = None

    if query.startswith("#") or query.upper().startswith("%23"):
        player_tag = normalize_tag(query)
    else:
        clan_data = await clash.get(f"clans/{ENCODED_TAG}")
        members = clan_data.get("memberList", []) if clan_data else []
        exact = [m for m in members if str(m.get("name", "")).lower() == query.lower()]
        partial = [m for m in members if query.lower() in str(m.get("name", "")).lower()]
        match = exact[0] if exact else (partial[0] if partial else None)
        if match:
            player_tag = match.get("tag")

    if not player_tag:
        await msg.answer(f"❌ Гравця «{esc(query)}» не знайдено в клані.", parse_mode=ParseMode.HTML)
        return

    p_data = await clash.get(f"players/{encode_tag(player_tag)}")
    if not p_data:
        await msg.answer("❌ Не вдалося отримати дані гравця з Supercell API.")
        return

    name = esc(p_data.get("name", "Невідомо"))
    trophies = int(p_data.get("trophies", 0) or 0)
    raw_league = p_data.get("league", {}).get("name", "Unranked")
    if raw_league == "Legend League":
        if trophies >= 5400:
            league_name = "Legend League I"
        elif trophies >= 5200:
            league_name = "Legend League II"
        else:
            league_name = "Legend League III"
    else:
        league_name = raw_league

    text = (
        f"👤 Інформація про гравця: <b>{name}</b>\n"
        f"🏷 Тег: <code>{esc(p_data.get('tag', player_tag))}</code>\n"
        f"🏰 Ратуша (TH): {p_data.get('townHallLevel', '?')}\n"
        f"⭐ Рівень: {p_data.get('expLevel', 0)}\n"
        f"🛡 Посада в клані: {esc(str(p_data.get('role', 'member')).capitalize())}\n\n"
        f"🏆 Кубки: {trophies} (Рекорд: {p_data.get('bestTrophies', 0)})\n"
        f"🏅 Ліга: {esc(league_name)}\n"
        f"⚔️ Зірки на війні: {p_data.get('warStars', 0)}\n\n"
        f"🤲 Донат: {p_data.get('donations', 0)} / Отримано: {p_data.get('donationsReceived', 0)}"
    )

    images = [BASE_DIR / f"{i}.jpg" for i in range(1, 13)]
    available = [p for p in images if p.exists()]
    if available:
        try:
            await msg.answer_photo(FSInputFile(random.choice(available)), caption=text, parse_mode=ParseMode.HTML)
            return
        except Exception as exc:
            logging.warning("Не вдалося відправити картинку гравця: %s", exc)

    await msg.answer(text, parse_mode=ParseMode.HTML)


@dp.message(Command("raidstats"))
async def cmd_raidstats(msg: types.Message):
    data = await clash.get(f"clans/{ENCODED_TAG}/capitalraidseasons?limit=1")
    if not data or not data.get("items"):
        await msg.answer("⚠️ Не вдалося отримати дані про рейди від Supercell.")
        return

    raid = data["items"][0]
    raid_members = {m.get("tag"): m for m in raid.get("members", [])}
    clan_data = await clash.get(f"clans/{ENCODED_TAG}")
    all_clan_members = clan_data.get("memberList", []) if clan_data else []

    unfinished = []
    total_attacks = 0
    for clan_member in all_clan_members:
        tag = clan_member.get("tag", "")
        name = clan_member.get("name", "Гравець")
        if tag in raid_members:
            member = raid_members[tag]
            used = member.get("attacks", 0)
            if isinstance(used, dict):
                used = used.get("count", 0)
            limit = int(member.get("attackLimit", 5) or 5) + int(member.get("bonusAttackLimit", 0) or 0)
        else:
            used = 0
            limit = 6

        total_attacks += int(used or 0)
        if used < limit:
            unfinished.append(f"• {format_mention(tag, name)} — {used}/{limit} ⚔️")

    text = (
        "🏹 <b>Підсумки останнього рейду:</b>\n\n"
        f"💰 <b>Всього добуто золота:</b> {raid.get('capitalTotalLoot', 0):,}\n"
        f"⚔️ <b>Всього зроблено атак:</b> {total_attacks}\n"
        f"🏰 <b>Знищено ворожих районів:</b> {raid.get('raidsCompleted', 0)}\n\n"
    )
    text += (
        f"⚠️ <b>Гравці, які не зробили всі атаки ({len(unfinished)}):</b>\n" + "\n".join(unfinished)
        if unfinished
        else "🎉 Усі учасники зробили максимум атак!"
    )
    await msg.answer(text, parse_mode=ParseMode.HTML)


# ============================================================
# WELCOME
# ============================================================


@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def welcome_new_chat_member(event: ChatMemberUpdated):
    user = event.new_chat_member.user
    mention = f'<a href="tg://user?id={user.id}">{esc(user.first_name or "друже")}</a>'
    text = (
        f"Привіт, {mention}! 👋 Вітаємо у нашому чаті! 🏰\n\n"
        "Будь ласка, прив'яжи свій Telegram до ігрового профілю в Clash of Clans.\n"
        "Для цього напиши команду:\n"
        "<code>/link #ТВІЙ_ТЕГ</code> (наприклад, <code>/link #2ABC123</code>)"
    )
    await send_to_topic(event.chat.id, text)


# ============================================================
# BACKGROUND NOTIFICATIONS
# ============================================================


async def check_war_events(chat_id: int) -> None:
    war = await clash.get(f"clans/{ENCODED_TAG}/currentwar")
    if not war or war.get("state") == "notInWar":
        return

    our_clan, opp_clan = our_and_opponent(war)
    if not our_clan or not opp_clan:
        return

    state = war.get("state")
    war_id = war.get("endTime") or war.get("startTime") or opp_clan.get("tag", "unknown")
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(war_id))
    start_time = parse_coc_time(war.get("startTime"))
    end_time = parse_coc_time(war.get("endTime"))
    attack_limit = int(war.get("attacksPerMember", 2) or 2)

    if state == "inWar":
        start_key = f"war_started_{safe_id}"
        if not state_once(start_key):
            if recent_enough(start_time, 1.0):
                await send_to_topic(
                    chat_id,
                    f"Почалася війна з <b>{esc(opp_clan.get('name', 'ворогом'))}</b> ⚔️\n"
                    f"Не забувайте зробити {attack_limit} атаки. Всім успіхів! 💙",
                    photo="24.jpg",
                )
            mark_state(start_key)

        reminder_key = f"war_3h_{safe_id}"
        if end_time and not state_once(reminder_key):
            hours_left = (end_time - datetime.now(timezone.utc)).total_seconds() / 3600
            if 0 < hours_left <= 3.5:
                unattacked = []
                for member in our_clan.get("members", []):
                    count = len(member.get("attacks", []))
                    if count < attack_limit:
                        unattacked.append(
                            f"• {format_mention(member.get('tag', ''), member.get('name', 'Гравець'))} ({count}/{attack_limit})"
                        )
                if unattacked:
                    await send_to_topic(
                        chat_id,
                        "⚠️ <b>Залишилося близько 3 годин до кінця КВ!</b> 🕛\n\n"
                        "Гравці, які ще не зробили всі атаки:\n\n"
                        + "\n".join(unattacked)
                        + "\n\nЗробіть, будь ласка, свої атаки! ⚔️",
                    )
                mark_state(reminder_key)

    if state == "warEnded":
        ended_key = f"war_ended_{safe_id}"
        recorded_key = f"war_recorded_{safe_id}"

        if not state_once(recorded_key):
            rows = []
            for member in our_clan.get("members", []):
                rows.append(
                    (
                        member.get("tag", ""),
                        member.get("name", "Гравець"),
                        len(member.get("attacks", [])),
                        attack_limit,
                    )
                )
            record_event_stats("cw", rows, end_time)
            mark_state(recorded_key)

        if not state_once(ended_key):
            if recent_enough(end_time, 12.0):
                not_full = []
                for member in our_clan.get("members", []):
                    attacks = member.get("attacks", [])
                    count = len(attacks)
                    stars = sum(int(a.get("stars", 0) or 0) for a in attacks)
                    if count < attack_limit:
                        not_full.append(
                            f"• {format_mention(member.get('tag', ''), member.get('name', 'Гравець'))} — "
                            f"{count}/{attack_limit} атак ⚔️, {stars} ⭐"
                        )

                text = (
                    f"🏁 Війна проти «<b>{esc(opp_clan.get('name', 'ворогом'))}</b>» закінчена.\n"
                    f"{war_result_text(our_clan, opp_clan)}\n"
                    f"⭐ Рахунок: <b>{our_clan.get('stars', 0)}</b> — <b>{opp_clan.get('stars', 0)}</b>\n\n"
                )
                text += (
                    "⚠️ <b>Гравці, які не зробили всі атаки:</b>\n" + "\n".join(not_full)
                    if not_full
                    else "🎉 Усі зробили свої атаки! Молодці!"
                )
                await send_to_topic(chat_id, text, photo="25.jpg")
            mark_state(ended_key)


async def check_cwl_events(chat_id: int) -> None:
    group = await clash.get(f"clans/{ENCODED_TAG}/currentwar/leaguegroup")
    if not group or group.get("state") == "notInWar":
        return

    war_cache: dict[str, dict[str, Any]] = {}

    for round_data in group.get("rounds", []):
        for war_tag in round_data.get("warTags", []):
            if war_tag == "#0":
                continue

            key_tag = war_tag.replace("#", "")
            summary_done = state_once(f"cwl_ended_{key_tag}")
            stats_done = state_once(f"cwl_recorded_{key_tag}")
            if group.get("state") != "ended" and summary_done and stats_done:
                continue

            war = await clash.get(f"clanwarleagues/wars/{encode_tag(war_tag)}")
            if not war:
                continue
            war_cache[war_tag] = war

            our_clan, opp_clan = our_and_opponent(war)
            if not our_clan or not opp_clan:
                continue

            state = war.get("state")
            start_time = parse_coc_time(war.get("startTime"))
            end_time = parse_coc_time(war.get("endTime"))

            if state == "inWar":
                start_key = f"cwl_started_{key_tag}"
                if not state_once(start_key):
                    if recent_enough(start_time, 1.0):
                        await send_to_topic(
                            chat_id,
                            f"🏆 <b>Розпочався новий день ЛВК проти «{esc(opp_clan.get('name', 'суперника'))}»!</b> ⚔️\n\n"
                            "Не забувайте зробити свою 1 вирішальну атаку! Успіхів та 3 зірок кожному! 🛡️✨",
                        )
                    mark_state(start_key)

                reminder_key = f"cwl_3h_{key_tag}"
                if end_time and not state_once(reminder_key):
                    hours_left = (end_time - datetime.now(timezone.utc)).total_seconds() / 3600
                    if 0 < hours_left <= 3.5:
                        unattacked = []
                        for member in our_clan.get("members", []):
                            if not member.get("attacks", []):
                                unattacked.append(
                                    f"• {format_mention(member.get('tag', ''), member.get('name', 'Гравець'))} (0/1)"
                                )
                        if unattacked:
                            await send_to_topic(
                                chat_id,
                                "🏆 <b>Залишилося близько 3 годин до кінця раунду ЛВК!</b> 🕛\n\n"
                                "Гравці, які ще не зробили атаку:\n\n"
                                + "\n".join(unattacked)
                                + "\n\nЗробіть, будь ласка, свій бій за клан! ⚔️",
                            )
                        mark_state(reminder_key)

            if state == "warEnded":
                recorded_key = f"cwl_recorded_{key_tag}"
                ended_key = f"cwl_ended_{key_tag}"

                if not state_once(recorded_key):
                    rows = [
                        (
                            member.get("tag", ""),
                            member.get("name", "Гравець"),
                            len(member.get("attacks", [])),
                            1,
                        )
                        for member in our_clan.get("members", [])
                    ]
                    record_event_stats("cwl", rows, end_time)
                    mark_state(recorded_key)

                if not state_once(ended_key):
                    if recent_enough(end_time, 12.0):
                        unattacked = [
                            f"• {format_mention(member.get('tag', ''), member.get('name', 'Гравець'))}"
                            for member in our_clan.get("members", [])
                            if not member.get("attacks", [])
                        ]
                        missed = (
                            "⚠️ <b>Атаку не зробили:</b>\n" + "\n".join(unattacked)
                            if unattacked
                            else "🌟 <b>Усі учасники зробили свої атаки! Чудова робота!</b>"
                        )
                        await send_to_topic(
                            chat_id,
                            f"🏁 <b>Раунд ЛВК проти «{esc(opp_clan.get('name', 'суперника'))}» завершено!</b>\n\n"
                            f"{war_result_text(our_clan, opp_clan)}\n"
                            f"⭐ Рахунок: <b>{our_clan.get('stars', 0)}</b> — <b>{opp_clan.get('stars', 0)}</b>\n\n"
                            f"{missed}",
                        )
                    mark_state(ended_key)

    if group.get("state") == "ended":
        season = group.get("season", "невідомий")
        season_key = f"cwl_season_summary_{season}"
        if state_once(season_key):
            return

        stats: dict[str, dict[str, Any]] = {}
        for round_data in group.get("rounds", []):
            for war_tag in round_data.get("warTags", []):
                if war_tag == "#0":
                    continue
                war = war_cache.get(war_tag)
                if war is None:
                    war = await clash.get(f"clanwarleagues/wars/{encode_tag(war_tag)}")
                if not war:
                    continue
                our_clan, _ = our_and_opponent(war)
                if not our_clan:
                    continue

                for member in our_clan.get("members", []):
                    tag = member.get("tag", "")
                    p = stats.setdefault(
                        tag,
                        {
                            "name": member.get("name", "Гравець"),
                            "stars": 0,
                            "destruction": 0.0,
                            "attacks": 0,
                            "possible": 0,
                        },
                    )
                    p["possible"] += 1
                    for attack in member.get("attacks", []):
                        p["stars"] += int(attack.get("stars", 0) or 0)
                        p["destruction"] += float(attack.get("destructionPercentage", 0) or 0)
                        p["attacks"] += 1

        ranked = sorted(stats.values(), key=lambda p: (p["stars"], p["destruction"]), reverse=True)
        lines = []
        for idx, p in enumerate(ranked, 1):
            avg = round(p["destruction"] / p["attacks"]) if p["attacks"] else 0
            lines.append(
                f"{idx}. <b>{esc(p['name'])}</b> — ⭐ <b>{p['stars']}</b> "
                f"({p['attacks']}/{p['possible']} атак, avg {avg}%)"
            )

        text = (
            f"🏆 <b>Підсумки Ліги Війн Кланів (сезон {esc(season)})!</b> 🏁\n\n"
            "📊 <b>Топи за весь сезон:</b>\n\n"
            + ("\n".join(lines) if lines else "Немає даних про атаки.")
            + "\n\nДякуємо всім за активну участь у ЛВК! 💪🔥"
        )
        await send_to_topic(chat_id, text)
        mark_state(season_key)


async def check_raid_events(chat_id: int) -> None:
    data = await clash.get(f"clans/{ENCODED_TAG}/capitalraidseasons?limit=1")
    if not data or not data.get("items"):
        return

    raid = data["items"][0]
    state = raid.get("state")
    start_time = parse_coc_time(raid.get("startTime"))
    end_time = parse_coc_time(raid.get("endTime"))
    raid_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(raid.get("startTime") or raid.get("endTime") or "raid"))

    if state == "ongoing":
        start_key = f"raid_started_{raid_id}"
        if not state_once(start_key):
            if recent_enough(start_time, 2.0):
                await send_to_topic(
                    chat_id,
                    "Добрий ранок, любі друзі. ☺️ Почалися Рейди! 🏹🗡️\n"
                    "Атакуйте, робіть усі доступні атаки й не залишайте недобиті регіони іншим! 🐶",
                )
            mark_state(start_key)

        reminder_key = f"raid_24h_{raid_id}"
        if end_time and not state_once(reminder_key):
            hours_left = (end_time - datetime.now(timezone.utc)).total_seconds() / 3600
            if 0 < hours_left <= 24.5:
                unfinished = []
                for member in raid.get("members", []):
                    count = member.get("attacks", 0)
                    if isinstance(count, dict):
                        count = count.get("count", 0)
                    limit = int(member.get("attackLimit", 5) or 5) + int(member.get("bonusAttackLimit", 0) or 0)
                    if count < limit:
                        unfinished.append(
                            f"{format_mention(member.get('tag', ''), member.get('name', 'Гравець'))} {count}/{limit} ⚔️"
                        )
                if unfinished:
                    await send_to_topic(
                        chat_id,
                        "Шановні " + ", ".join(unfinished) + ", зробіть, будь ласка, атаки в рейдах 😊🗡️",
                    )
                mark_state(reminder_key)

    if state == "ended":
        ended_key = f"raid_ended_{raid_id}"
        recorded_key = f"raid_recorded_{raid_id}"

        clan_data = await clash.get(f"clans/{ENCODED_TAG}")
        all_clan_members = clan_data.get("memberList", []) if clan_data else []
        raid_members = {member.get("tag"): member for member in raid.get("members", [])}

        rows = []
        unfinished = []
        total_attacks = 0
        for clan_member in all_clan_members:
            tag = clan_member.get("tag", "")
            name = clan_member.get("name", "Гравець")
            if tag in raid_members:
                member = raid_members[tag]
                count = member.get("attacks", 0)
                if isinstance(count, dict):
                    count = count.get("count", 0)
                limit = int(member.get("attackLimit", 5) or 5) + int(member.get("bonusAttackLimit", 0) or 0)
            else:
                count = 0
                limit = 6

            count = int(count or 0)
            total_attacks += count
            rows.append((tag, name, count, limit))
            if count < limit:
                unfinished.append(f"• {format_mention(tag, name)} — {count}/{limit} ⚔️")

        if not state_once(recorded_key):
            record_event_stats("raid", rows, end_time)
            mark_state(recorded_key)

        if not state_once(ended_key):
            if recent_enough(end_time, 48.0):
                text = (
                    "🏹 <b>Рейди закінчилися! Підбиваємо підсумки:</b>\n\n"
                    f"💰 <b>Всього добуто золота:</b> {raid.get('capitalTotalLoot', 0):,}\n"
                    f"⚔️ <b>Всього зроблено атак:</b> {total_attacks}\n"
                    f"🏰 <b>Знищено ворожих районів:</b> {raid.get('raidsCompleted', 0)}\n\n"
                )
                text += (
                    f"⚠️ <b>Гравці, які не зробили всі атаки ({len(unfinished)}):</b>\n" + "\n".join(unfinished)
                    if unfinished
                    else "🎉 Усі учасники зробили максимум атак!"
                )
                await send_to_topic(chat_id, text)
            mark_state(ended_key)


async def check_clan_games(chat_id: int) -> None:
    now = datetime.now(BOT_TIMEZONE)
    key = f"clan_games_{now:%Y-%m}"
    if now.day == 22 and 8 <= now.hour < 11 and not state_once(key):
        await send_to_topic(
            chat_id,
            "Доброго ранку, шановні 💚 Почалися Ігри Кланів! ⚽\n"
            "Набийте, будь ласка, 4к очок, аби отримати додаткову нагороду 🎁\n"
            "Всім успіхів 💜",
        )
        mark_state(key)


async def is_youtube_shorts(video_id: str) -> bool:
    await clash.start()
    assert clash.session is not None
    url = f"https://www.youtube.com/shorts/{video_id}"
    try:
        async with clash.session.head(url, allow_redirects=False) as response:
            return response.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


async def check_youtube_news() -> None:
    now = datetime.now(BOT_TIMEZONE)
    if not (8 <= now.hour < 20) or not CHAT_ID:
        return

    await clash.start()
    assert clash.session is not None
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"

    try:
        async with clash.session.get(rss_url) as response:
            if response.status != 200:
                logging.warning("YouTube RSS HTTP %s", response.status)
                return
            raw_feed = await response.read()

        feed = feedparser.parse(raw_feed)
        if not feed.entries:
            return

        latest = feed.entries[0]
        video_id = getattr(latest, "yt_videoid", None)
        if not video_id or bot_state.get("last_youtube_video_id") == video_id:
            return

        if await is_youtube_shorts(video_id):
            mark_state("last_youtube_video_id", video_id)
            return

        title = esc(getattr(latest, "title", "Нове відео"))
        video_link = getattr(latest, "link", f"https://www.youtube.com/watch?v={video_id}")
        summary = latest.get("summary", "")
        army_match = re.search(r'https://link\.clashofclans\.com/[^\s"<>]+action=CopyArmy[^\s"<>]*', summary)

        builder = InlineKeyboardBuilder()
        hint = ""
        if army_match:
            builder.button(text="⚔️ Скопіювати мікс у гру", url=army_match.group(0))
            hint = "\n\n💡 <i>У описі знайдено мікс. Натискай кнопку нижче, щоб завантажити його в гру.</i>"
        builder.button(text="📺 Дивитися відео", url=video_link)
        builder.adjust(1)

        text = f"🎬 <b>Нове відео від SALOMON!</b>\n\n📌 <b>{title}</b>{hint}"
        kwargs = topic_kwargs(NEWS_THREAD_ID)
        await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=builder.as_markup(),
            **kwargs,
        )
        mark_state("last_youtube_video_id", video_id)
    except Exception as exc:
        logging.error("Помилка YouTube RSS: %s", exc)


async def maybe_send_weekly_report(chat_id: int) -> None:
    now = datetime.now(BOT_TIMEZONE)
    iso_year, iso_week, _ = now.isocalendar()
    key = f"weekly_report_{iso_year}_{iso_week}"

    # Any check during Monday 14:00-14:59 can deliver it, so a 5-minute loop
    # cannot accidentally miss the tiny first-five-minutes window.
    if now.weekday() == 0 and now.hour == 14 and not state_once(key):
        report = await process_weekly_league_report()
        await bot.send_message(
            chat_id=chat_id,
            text=report,
            parse_mode=ParseMode.HTML,
            **topic_kwargs(THREAD_ID),
        )
        if not report.startswith("❌"):
            mark_state(key)


async def background_checker() -> None:
    while True:
        try:
            if CHAT_ID:
                await check_war_events(CHAT_ID)
                await check_cwl_events(CHAT_ID)
                await check_raid_events(CHAT_ID)
                await check_clan_games(CHAT_ID)
                await check_youtube_news()
                await maybe_send_weekly_report(CHAT_ID)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Помилка фонової перевірки")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


# ============================================================
# MAIN
# ============================================================


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    logging.info("Запуск CoC Bot для клану %s", CLAN_TAG)
    logging.info("Часовий пояс планувальника: %s", BOT_TIMEZONE_NAME)

    await clash.start()
    background_task = asyncio.create_task(background_checker(), name="background_checker")

    try:
        await dp.start_polling(bot)
    finally:
        background_task.cancel()
        try:
            await background_task
        except asyncio.CancelledError:
            pass
        await clash.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())