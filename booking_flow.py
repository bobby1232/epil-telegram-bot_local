from __future__ import annotations

from datetime import datetime, timedelta, time, date
import pytz

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

from db import DB
from config import Defaults
import texts
import app.keyboards as keyboards

from app.db import DB
from app.config import Defaults
from app import texts, keyboards


# states
SVC, DAY, TIME, COMMENT, PHONE, FINAL = range(6)

def _tz(context: ContextTypes.DEFAULT_TYPE) -> pytz.BaseTzInfo:
    return pytz.timezone(context.bot_data["tz"])

def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return update.effective_user and update.effective_user.id == context.bot_data["admin_id"]

async def start_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: DB = context.bot_data["db"]
    user = update.effective_user
    await db.upsert_user(user.id, user.username, user.full_name)

    services = await db.get_services()
    if not services:
        await update.message.reply_text("Услуги не настроены. Обратитесь к мастеру.")
        return ConversationHandler.END

    buttons = []
    for s in services:
        buttons.append([InlineKeyboardButton(
            f"{s['name']} — {s['duration_min']} мин — {s['price']} ₽",
            callback_data=f"svc:{s['id']}"
        )])
    kb = InlineKeyboardMarkup(buttons + [[InlineKeyboardButton("↩️ В меню", callback_data="svc:cancel")]])
    await update.message.reply_text("Выберите услугу:", reply_markup=kb)
    return SVC

async def pick_service_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "svc:cancel":
        await query.edit_message_text(texts.MAIN_MENU)
        await query.message.reply_text(texts.ABOUT, reply_markup=keyboards.main_menu(_is_admin(update, context)))
        return ConversationHandler.END

    _, sid = data.split(":")
    context.user_data["service_id"] = int(sid)

    # show dates list (next 14 days)
    tz = _tz(context)
    today = datetime.now(tz).date()
    horizon = Defaults.BOOKING_HORIZON_DAYS
    rows = []
    for i in range(horizon):
        d = today + timedelta(days=i)
        rows.append([InlineKeyboardButton(d.strftime("%a %d.%m"), callback_data=f"day:{d.isoformat()}")])
    rows.append([InlineKeyboardButton("↩️ Назад", callback_data="day:back")])
    await query.edit_message_text("Выберите дату:", reply_markup=InlineKeyboardMarkup(rows))
    return DAY

def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))

async def _compute_free_slots(db: DB, tz, chosen_date: date, service_id: int) -> list[datetime]:
    """
    Генерируем слоты на день с учётом:
      - working_hours (берём дефолт 10-20 + weekday)
      - min_lead_time
      - buffer
      - blocked_intervals
      - existing Hold/Booked
    """
    service = await db.get_service(service_id)
    if not service:
        return []

    slot_step = await db.get_setting_int("slot_step_min", Defaults.SLOT_STEP_MIN)
    min_lead = await db.get_setting_int("min_lead_time_min", Defaults.MIN_LEAD_TIME_MIN)
    work_start = await db.get_setting_str("work_start", Defaults.WORK_START)
    work_end = await db.get_setting_str("work_end", Defaults.WORK_END)
    work_days = await db.get_setting_str("work_days", ",".join(map(str, Defaults.WORK_DAYS)))
    work_days_set = set(int(x) for x in work_days.split(",") if x.strip() != "")

    if chosen_date.weekday() not in work_days_set:
        return []

    start_t = _parse_hhmm(work_start)
    end_t = _parse_hhmm(work_end)

    day_start = tz.localize(datetime.combine(chosen_date, start_t))
    day_end = tz.localize(datetime.combine(chosen_date, end_t))

    now = datetime.now(tz)
    earliest = now + timedelta(minutes=min_lead)

    duration = int(service["duration_min"])
    buffer_min = int(service["buffer_min"])
    total = duration + buffer_min

    # load busy intervals
    blocked = await db.list_blocked(day_start, day_end)
    busy = await db.list_active_appointments(day_start, day_end)

    def overlaps(st: datetime, en: datetime) -> bool:
        for r in blocked:
            if st < r["end_dt"] and en > r["start_dt"]:
                return True
        for r in busy:
            if st < r["end_dt"] and en > r["start_dt"]:
                return True
        return False

    slots = []
    cursor = day_start
    step = timedelta(minutes=slot_step)
    while cursor + timedelta(minutes=duration) <= day_end:
        st = cursor
        en = cursor + timedelta(minutes=total)
        if st >= earliest and not overlaps(st, en):
            slots.append(st)
        cursor += step

    return slots

async def pick_day_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "day:back":
        # rebuild services list
        db: DB = context.bot_data["db"]
        services = await db.get_services()
        buttons = [[InlineKeyboardButton(
            f"{s['name']} — {s['duration_min']} мин — {s['price']} ₽",
            callback_data=f"svc:{s['id']}"
        )] for s in services]
        buttons.append([InlineKeyboardButton("↩️ В меню", callback_data="svc:cancel")])
        await query.edit_message_text("Выберите услугу:", reply_markup=InlineKeyboardMarkup(buttons))
        return SVC

    _, iso = data.split(":")
    chosen_date = date.fromisoformat(iso)
    context.user_data["date"] = chosen_date.isoformat()

    db: DB = context.bot_data["db"]
    tz = _tz(context)
    service_id = context.user_data["service_id"]

    slots = await _compute_free_slots(db, tz, chosen_date, service_id)
    if not slots:
        await query.edit_message_text("На эту дату свободных слотов нет. Выберите другую дату.")
        return DAY

    rows = []
    for st in slots[:40]:  # MVP-лимит чтобы не раздувать клавиатуру
        rows.append([InlineKeyboardButton(st.strftime("%H:%M"), callback_data=f"time:{st.isoformat()}")])
    rows.append([InlineKeyboardButton("↩️ Назад к дате", callback_data="time:back")])
    await query.edit_message_text("Выберите время:", reply_markup=InlineKeyboardMarkup(rows))
    return TIME

async def pick_time_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "time:back":
        #
