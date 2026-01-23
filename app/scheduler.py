from __future__ import annotations
from datetime import datetime, timedelta
import pytz
from sqlalchemy import select, and_
from telegram.ext import Application

from app.models import Appointment, AppointmentStatus
from app.logic import get_settings
from app.keyboards import reminder_kb
from app.config import Config

async def tick(application: Application) -> None:
    cfg: Config = application.bot_data["cfg"]
    session_factory = application.bot_data["session_factory"]

    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        now_utc = datetime.now(tz=pytz.UTC)

        # Expire HOLD
        holds = (await s.execute(
            select(Appointment).where(
                and_(
                    Appointment.status == AppointmentStatus.Hold,
                    Appointment.hold_expires_at.is_not(None),
                    Appointment.hold_expires_at < now_utc
                )
            )
        )).scalars().all()

        if holds:
            async with s.begin():
                for appt in holds:
                    appt.status = AppointmentStatus.Rejected
                    appt.updated_at = now_utc
                    appt.hold_expires_at = None
                    await application.bot.send_message(
                        chat_id=appt.client.tg_id,
                        text=(
                            f"⏳ Ваша заявка #{appt.id} не была подтверждена вовремя и автоматически отклонена.\n"
                            f"Попробуйте выбрать другое время."
                        )
                    )

        # Reminders for Booked (24h / 2h) with wide windows to avoid missing if server lags.
        booked = (await s.execute(
            select(Appointment).where(Appointment.status == AppointmentStatus.Booked)
        )).scalars().all()

        for appt in booked:
            start_utc = appt.start_dt

            # 24h window: from -24h to -23h
            if (not appt.reminder_24h_sent) and (start_utc - timedelta(hours=24) <= now_utc <= start_utc - timedelta(hours=23)):
                async with s.begin():
                    appt.reminder_24h_sent = True
                    appt.updated_at = now_utc
                await application.bot.send_message(
                    chat_id=appt.client.tg_id,
                    text=f"⏰ Напоминание: через 24 часа у вас запись #{appt.id} ({appt.service.name}).",
                    reply_markup=reminder_kb(appt.id)
                )

            # 2h window: from -2h to -1h
            if (not appt.reminder_2h_sent) and (start_utc - timedelta(hours=2) <= now_utc <= start_utc - timedelta(hours=1)):
                async with s.begin():
                    appt.reminder_2h_sent = True
                    appt.updated_at = now_utc
                await application.bot.send_message(
                    chat_id=appt.client.tg_id,
                    text=f"⏰ Напоминание: через 2 часа у вас запись #{appt.id} ({appt.service.name}).",
                    reply_markup=reminder_kb(appt.id)
                )
