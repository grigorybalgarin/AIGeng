import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest



DATA_DIR = Path("data")


DEFAULT_TASKS = [
    "Python: 30 мин теория (Notion)",
    "Python: 30 мин практика (PyCharm)",
    "5 мин итог: что понял/что повторить",
]


def today_str() -> str:
    return date.today().isoformat()


def tomorrow_str() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def user_file(user_id: int) -> Path:
    ensure_data_dir()
    return DATA_DIR / f"{user_id}.json"


def load_user_state(user_id: int) -> Dict[str, Any]:
    path = user_file(user_id)
    if not path.exists():
        return {"user_id": user_id, "created_at": now_iso(), "days": {}}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_user_state(user_id: int, state: Dict[str, Any]) -> None:
    path = user_file(user_id)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_day(state: Dict[str, Any], day: str) -> Dict[str, Any]:
    days = state.setdefault("days", {})
    if day not in days:
        days[day] = {"tasks": [], "closed": False, "created_at": now_iso()}
    return days[day]


def create_default_plan(day_obj: Dict[str, Any]) -> None:
    if day_obj["tasks"]:
        return
    tasks = []
    for i, text in enumerate(DEFAULT_TASKS, start=1):
        tasks.append(
            {
                "id": i,
                "text": text,
                "status": "todo",
                "created_at": now_iso(),
                "done_at": None,
            }
        )
    day_obj["tasks"] = tasks


def render_plan(day: str, day_obj: Dict[str, Any]) -> str:
    lines = [f"📌 <b>План на {day}</b>"]
    if day_obj.get("closed"):
        lines.append("⚠️ День закрыт (история).")
        return "\n".join(lines)

    tasks: List[Dict[str, Any]] = day_obj.get("tasks", [])
    if not tasks:
        lines.append("Пока задач нет.")
        return "\n".join(lines)

    for t in tasks:
        mark = "✅" if t["status"] == "done" else "⬜"
        lines.append(f"{mark} <b>{t['id']})</b> {t['text']}")
    lines.append("\nОтмечай выполненное кнопками ниже.")
    return "\n".join(lines)


def build_start_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["📌 План на сегодня"], ["🌙 Итог дня"]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def build_today_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Сделал 1", callback_data="done:1")],
            [InlineKeyboardButton("✅ Сделал 2", callback_data="done:2")],
            [InlineKeyboardButton("✅ Сделал 3", callback_data="done:3")],
            [InlineKeyboardButton("🌙 Итог дня", callback_data="evening")],
        ]
    )


def find_task(day_obj: Dict[str, Any], task_id: int) -> Optional[Dict[str, Any]]:
    for t in day_obj.get("tasks", []):
        if t.get("id") == task_id:
            return t
    return None


def normalize_task_ids(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Перенумеруем id 1..N, чтобы всё было аккуратно
    new_tasks = []
    for i, t in enumerate(tasks, start=1):
        t = dict(t)
        t["id"] = i
        new_tasks.append(t)
    return new_tasks


def ensure_min_tasks(day_obj: Dict[str, Any], min_count: int = 3) -> None:
    tasks = day_obj.get("tasks", [])
    existing_texts = {t["text"] for t in tasks}
    add_texts = [t for t in DEFAULT_TASKS if t not in existing_texts]
    while len(tasks) < min_count and add_texts:
        text = add_texts.pop(0)
        tasks.append(
            {
                "id": len(tasks) + 1,
                "text": text,
                "status": "todo",
                "created_at": now_iso(),
                "done_at": None,
            }
        )
    day_obj["tasks"] = normalize_task_ids(tasks)


def apply_done(day_obj: Dict[str, Any], task_id: int) -> tuple[bool, str]:
    if day_obj.get("closed"):
        return False, "Сегодняшний день уже закрыт. Напиши /today чтобы начать новый план."

    task = find_task(day_obj, task_id)
    if not task:
        return False, f"Не нашёл задачу с номером {task_id}. Сначала посмотри /today"

    if task["status"] == "done":
        return False, f"Задача {task_id} уже была отмечена ✅"

    task["status"] = "done"
    task["done_at"] = now_iso()
    return True, f"✅ Отметил: {task_id}) {task['text']}"


def build_evening_report(state: Dict[str, Any], day: str, day_obj: Dict[str, Any]) -> str:
    tasks: List[Dict[str, Any]] = day_obj.get("tasks", [])
    if not tasks:
        create_default_plan(day_obj)
        tasks = day_obj["tasks"]

    done_tasks = [t for t in tasks if t["status"] == "done"]
    todo_tasks = [t for t in tasks if t["status"] != "done"]

    # Закрываем текущий день
    day_obj["closed"] = True
    day_obj["closed_at"] = now_iso()

    # Готовим завтра
    tmr = tomorrow_str()
    tomorrow_obj = get_day(state, tmr)
    if tomorrow_obj.get("closed"):
        tomorrow_obj["closed"] = False

    # Переносим невыполненные (в статус todo)
    carry = []
    for t in todo_tasks:
        carry.append(
            {
                "id": 0,
                "text": t["text"],
                "status": "todo",
                "created_at": now_iso(),
                "done_at": None,
                "carried_from": day,
            }
        )

    # Добавляем перенесённые в начало завтрашних задач (без дублей по тексту)
    existing_texts = {t["text"] for t in tomorrow_obj.get("tasks", [])}
    new_tasks = []
    for t in carry:
        if t["text"] not in existing_texts:
            new_tasks.append(t)

    tomorrow_obj["tasks"] = new_tasks + tomorrow_obj.get("tasks", [])
    tomorrow_obj["tasks"] = normalize_task_ids(tomorrow_obj["tasks"])
    ensure_min_tasks(tomorrow_obj, min_count=3)

    # Ответ
    lines = [
        f"🌙 <b>Итог дня {day}</b>",
        f"Сделано: <b>{len(done_tasks)}</b> / <b>{len(tasks)}</b>",
        "",
        "<b>✅ Выполнено:</b>" if done_tasks else "<b>✅ Выполнено:</b> —",
    ]
    if done_tasks:
        for t in done_tasks:
            lines.append(f"✅ {t['id']}) {t['text']}")

    lines += [
        "",
        "<b>⬜ Не сделано (перенёс на завтра):</b>" if todo_tasks else "<b>⬜ Не сделано:</b> —",
    ]
    if todo_tasks:
        for t in todo_tasks:
            lines.append(f"⬜ {t['id']}) {t['text']}")

    lines += ["", f"📌 <b>Черновик на завтра ({tmr}):</b>"]
    for t in tomorrow_obj["tasks"]:
        lines.append(f"⬜ <b>{t['id']})</b> {t['text']}")

    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я твой PM-бот (MVP v0.1).\n\n"
        "Как пользуемся:\n"
        "1) С утра собери план на день\n"
        "2) В течение дня отмечай выполненное\n"
        "3) Вечером подведём итог и перенесём остаток\n\n"
        "Жми кнопки ниже — это самый быстрый путь.",
        reply_markup=build_start_keyboard(),
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = load_user_state(user_id)

    day = today_str()
    day_obj = get_day(state, day)

    if day_obj.get("closed"):
        # если вдруг закрыто — создадим новый день заново (редко)
        day_obj = {"tasks": [], "closed": False, "created_at": now_iso()}
        state["days"][day] = day_obj

    if not day_obj.get("tasks"):
        create_default_plan(day_obj)

    save_user_state(user_id, state)
    await update.message.reply_text(
        render_plan(day, day_obj),
        parse_mode=ParseMode.HTML,
        reply_markup=build_today_keyboard(),
    )


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = load_user_state(user_id)

    day = today_str()
    day_obj = get_day(state, day)

    if day_obj.get("closed"):
        await update.message.reply_text("Сегодняшний день уже закрыт. Напиши /today чтобы начать новый план.")
        return

    if not context.args:
        await update.message.reply_text("Нужно указать номер задачи. Пример: /done 2")
        return

    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Номер должен быть числом. Пример: /done 2")
        return

    ok, message = apply_done(day_obj, task_id)
    if not ok:
        await update.message.reply_text(message)
        return

    save_user_state(user_id, state)
    await update.message.reply_text(message)


async def cmd_evening(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = load_user_state(user_id)

    day = today_str()
    day_obj = get_day(state, day)

    if day_obj.get("closed"):
        await update.message.reply_text("День уже закрыт. Напиши /today чтобы увидеть план на сегодня.")
        return

    report = build_evening_report(state, day, day_obj)
    save_user_state(user_id, state)
    await update.message.reply_text(report, parse_mode=ParseMode.HTML)


async def handle_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = (update.message.text or "").strip()
    if text == "📌 План на сегодня":
        await cmd_today(update, context)
    elif text == "🌙 Итог дня":
        await cmd_evening(update, context)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    if data.startswith("done:"):
        try:
            task_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Неверный номер задачи.", show_alert=True)
            return

        user_id = query.from_user.id
        state = load_user_state(user_id)
        day = today_str()
        day_obj = get_day(state, day)

        ok, message = apply_done(day_obj, task_id)
        if not ok:
            await query.answer(message, show_alert=True)
            return

        save_user_state(user_id, state)
        await query.edit_message_text(
            render_plan(day, day_obj),
            parse_mode=ParseMode.HTML,
            reply_markup=build_today_keyboard(),
        )
        await query.answer(message)
        return

    if data == "evening":
        user_id = query.from_user.id
        state = load_user_state(user_id)
        day = today_str()
        day_obj = get_day(state, day)

        if day_obj.get("closed"):
            await query.answer("День уже закрыт. Напиши /today чтобы увидеть план на сегодня.", show_alert=True)
            await query.message.reply_text("День уже закрыт. Напиши /today чтобы увидеть план на сегодня.")
            return

        report = build_evening_report(state, day, day_obj)
        save_user_state(user_id, state)
        await query.message.reply_text(report, parse_mode=ParseMode.HTML)
        await query.answer()


def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")

    if not token or token.strip() == "" or "PASTE" in token:
        raise SystemExit(
            "BOT_TOKEN не найден. Создай файл .env в корне проекта и добавь строку:\n"
            "BOT_TOKEN=твой_токен_из_BotFather\n"
        )

    ensure_data_dir()

    request = HTTPXRequest(
        connect_timeout=20,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=30,
    )
    app = Application.builder().token(token).request(request).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("evening", cmd_evening))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_buttons))

    print("Bot is running... Press Ctrl+C to stop.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
