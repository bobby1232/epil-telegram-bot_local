from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Tuple, Optional

import pytz
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from telegram.ext import Application

from app.models import Appointment, AppointmentStatus
from app.logic import get_settings
from app.keyboards import reminder_kb
from app.config import Config


async def tick(application: Application) -> None:
    """Periodic maintenance:
    - expire HOLD appointments
    - send 24h / 2h reminders for BOOKED appointments

    IMPORTANT: SQLAlchemy AsyncSession uses autobegin. Do NOT nest `session.begin()`
    inside a session after any DB call, or you'll get:
    InvalidRequestError: A transaction is already begun on this Session.
    """
    cfg: Config = application.bot_data["cfg"]
    session_factory = application.bot_data["session_factory"]

    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        now_utc = datetime.now(tz=pytz.UTC)

        to_send: List[Tuple[int, str, Optional[object]]] = []

        # --- Expire HOLD ---
        holds = (await s.execute(
            select(Appointment)
            .options(
                selectinload(Appointment.client),
                selectinload(Appointment.service),
            )
            .where(
                and_(
                    Appointment.status == AppointmentStatus.Hold,
                    Appointment.hold_expires_at.is_not(None),
                    Appointment.hold_expires_at < now_utc,
                )
            )
        )).scalars().all()

        for appt in holds:
            appt.status = AppointmentStatus.Rejected
            appt.updated_at = now_utc
            appt.hold_expires_at = None
            # notify client after commit
            to_send.append((
                appt.client.tg_id,
                (
                    f"⏳ Ваша заявка #{appt.id} не была подтверждена вовремя и автоматически отклонена.\n"
                    f"Попробуйте выбрать другое время."
                ),
                None
            ))

        # --- Reminders for BOOKED (wide windows to avoid missing) ---
        booked = (await s.execute(
            select(Appointment)
            .options(
                selectinload(Appointment.client),
                selectinload(Appointment.service),
            )
            .where(Appointment.status == AppointmentStatus.Booked)
        )).scalars().all()

        for appt in booked:
            start_utc = appt.start_dt

            # 24h window: from -24h to -23h
            if (not appt.reminder_24h_sent) and (start_utc - timedelta(hours=24) <= now_utc <= start_utc - timedelta(hours=23)):
                appt.reminder_24h_sent = True
                appt.updated_at = now_utc
                to_send.append((
                    appt.client.tg_id,
                    f"⏰ Напоминание: через 24 часа у вас запись #{appt.id} ({appt.service.name}).",
                    reminder_kb(appt.id),
                ))

            # 2h window: from -2h to -1h
            if (not appt.reminder_2h_sent) and (start_utc - timedelta(hours=2) <= now_utc <= start_utc - timedelta(hours=1)):
                appt.reminder_2h_sent = True
                appt.updated_at = now_utc
                to_send.append((
                    appt.client.tg_id,
                    f"⏰ Напоминание: через 2 часа у вас запись #{appt.id} ({appt.service.name}).",
                    reminder_kb(appt.id),
                ))

        # Commit DB changes once (if any)
        if holds or any(a.reminder_24h_sent or a.reminder_2h_sent for a in booked):
            # commit will also end the implicit transaction
            await s.commit()
        else:
            # release implicit transaction created by autobegin
            await s.rollback()

        # Send messages after DB is consistent
        for chat_id, text, markup in to_send:
            if markup is None:
                await application.bot.send_message(chat_id=chat_id, text=text)
            else:
                await application.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
