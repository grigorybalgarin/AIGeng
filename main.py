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

BACKLOG_TTL_DAYS = 7


def today_str() -> str:
    return date.today().isoformat()


def tomorrow_str() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def format_date_ru(value: str) -> str:
    if not value:
        return value
    try:
        if "T" in value:
            dt = datetime.fromisoformat(value)
            return dt.strftime("%d.%m.%Y")
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    except Exception:
        return value


def parse_date_input(value: str) -> Optional[str]:
    raw = value.strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


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
    display_day = format_date_ru(day)
    lines = [f"📌 <b>План на {display_day}</b>"]
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
        [
            ["📌 План на сегодня"],
            ["📅 План по дате"],
            ["➕ Добавить задачу"],
            ["🗑 Удалить задачу"],
            ["🌙 Итог дня"],
            ["📦 Бэклог"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def build_today_keyboard(day_obj: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows = []
    tasks = day_obj.get("tasks", [])
    todo_tasks = [t for t in tasks if t.get("status") != "done"]
    for t in todo_tasks[:10]:
        task_id = t.get("id")
        label = f"✅ {shorten_text(str(t.get('text', '')), 34)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"done:{task_id}")])
    if len(todo_tasks) > 10:
        rows.append([InlineKeyboardButton("…ещё", callback_data="noop")])
    rows.append([InlineKeyboardButton("🌙 Итог дня", callback_data="evening")])
    return InlineKeyboardMarkup(rows)


def build_add_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Сегодня", callback_data="add:today"),
                InlineKeyboardButton("Завтра", callback_data="add:tomorrow"),
            ],
            [
                InlineKeyboardButton("Выбрать дату", callback_data="add:date"),
                InlineKeyboardButton("В бэклог", callback_data="add:backlog"),
            ],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
        ]
    )


def build_add_today_closed_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Завтра", callback_data="add:tomorrow")],
            [InlineKeyboardButton("Открыть новый день", callback_data="add:reopen_today")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
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


def normalize_task_ids_backlog(backlog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    new_backlog = []
    for i, item in enumerate(backlog, start=1):
        item = dict(item)
        item["id"] = i
        new_backlog.append(item)
    return new_backlog


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


def get_backlog(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    backlog = state.setdefault("backlog", [])
    if backlog is None:
        backlog = []
        state["backlog"] = backlog
    return backlog


def add_task_to_day(state: Dict[str, Any], day: str, text: str) -> Dict[str, Any]:
    day_obj = get_day(state, day)
    tasks = day_obj.get("tasks", [])
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
    return day_obj


def parse_iso(dt_str: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


def backlog_sort_key(item: Dict[str, Any]) -> datetime:
    dt = parse_iso(str(item.get("created_at", "")))
    return dt if dt else datetime.max


def is_backlog_overdue(item: Dict[str, Any], now: datetime) -> bool:
    dt = parse_iso(str(item.get("created_at", "")))
    if not dt:
        return False
    return now - dt > timedelta(days=BACKLOG_TTL_DAYS)


def next_backlog_id(backlog: List[Dict[str, Any]]) -> int:
    max_id = 0
    for item in backlog:
        try:
            max_id = max(max_id, int(item.get("id", 0)))
        except Exception:
            continue
    return max_id + 1


def find_backlog_item(backlog: List[Dict[str, Any]], item_id: int) -> Optional[Dict[str, Any]]:
    for item in backlog:
        try:
            if int(item.get("id", 0)) == item_id:
                return item
        except Exception:
            continue
    return None


def find_backlog_by_text(backlog: List[Dict[str, Any]], text: str) -> Optional[Dict[str, Any]]:
    for item in backlog:
        if item.get("text") == text:
            return item
    return None


def maybe_pull_from_backlog(state: Dict[str, Any], day_obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    backlog = get_backlog(state)
    if not backlog:
        return None

    candidates = [item for item in backlog if item.get("source_day")]
    if not candidates:
        return None
    oldest = sorted(candidates, key=backlog_sort_key)[0]
    oldest["last_seen_at"] = now_iso()

    tasks = day_obj.get("tasks", [])
    task_texts = {t.get("text") for t in tasks}
    if oldest.get("text") in task_texts:
        return None

    backlog.remove(oldest)
    backlog[:] = normalize_task_ids_backlog(backlog)
    tasks.append(
        {
            "id": len(tasks) + 1,
            "text": oldest.get("text"),
            "status": "todo",
            "created_at": oldest.get("created_at", now_iso()),
            "done_at": None,
            "carried_from": oldest.get("source_day"),
            "carry_count": int(oldest.get("carry_count", 0) or 0),
        }
    )
    day_obj["tasks"] = normalize_task_ids(tasks)
    return oldest


def render_backlog(backlog: List[Dict[str, Any]]) -> str:
    if not backlog:
        return "📦 Бэклог пуст."

    lines = [f"📦 <b>Бэклог</b> ({len(backlog)} задач)"]
    lines.append("")
    for item in backlog[:20]:
        lines.append(f"{item.get('id')}) {item.get('text')}")
    return "\n".join(lines)


def render_backlog_tail(backlog: List[Dict[str, Any]], limit: int = 3) -> str:
    if not backlog:
        return "📦 Бэклог пуст."
    items = backlog[-limit:]
    lines = ["Последние задачи:"]
    for item in items:
        lines.append(f"{item.get('id')}) {item.get('text')}")
    return "\n".join(lines)


def shorten_text(text: str, max_len: int = 32) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def render_backlog_pick_list(backlog: List[Dict[str, Any]], limit: int = 10) -> str:
    if not backlog:
        return "Бэклог пуст."
    items = backlog[-limit:]
    lines = [f"📦 <b>Бэклог</b> ({len(backlog)} задач)"]
    lines.append("")
    for item in items:
        lines.append(f"{item.get('id')}) {item.get('text')}")
    return "\n".join(lines)


def build_backlog_pick_keyboard(backlog: List[Dict[str, Any]], limit: int = 10) -> InlineKeyboardMarkup:
    rows = []
    items = backlog[-limit:]
    for item in items:
        item_id = item.get("id")
        label = f"№{item_id} {shorten_text(str(item.get('text', '')), 28)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"pick:{item_id}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="pick:back")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def build_move_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Сегодня", callback_data=f"move:{item_id}:today")],
            [InlineKeyboardButton("Завтра", callback_data=f"move:{item_id}:tomorrow")],
            [InlineKeyboardButton("Выбрать дату", callback_data=f"move:{item_id}:date")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
        ]
    )


def build_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])


def build_date_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Ввести дату", callback_data="date:input")],
            [InlineKeyboardButton("Показать даты с задачами", callback_data="date:list")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
        ]
    )


def build_date_list_keyboard(dates: List[str]) -> InlineKeyboardMarkup:
    rows = []
    for iso in dates:
        rows.append([InlineKeyboardButton(format_date_ru(iso), callback_data=f"date:open:{iso}")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def reset_input_modes(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("del_mode", None)
    context.user_data.pop("awaiting_del_id", None)
    context.user_data.pop("add_mode", None)
    context.user_data.pop("add_date", None)
    context.user_data.pop("move_mode", None)
    context.user_data.pop("move_task_id", None)
    context.user_data.pop("view_date_mode", None)
    context.user_data.pop("pending_add_text", None)


def render_day_preview(
    day: str, day_obj: Dict[str, Any], limit: int = 5, include_text: Optional[str] = None
) -> str:
    tasks: List[Dict[str, Any]] = day_obj.get("tasks", [])
    if not tasks:
        return f"План на {format_date_ru(day)} пока пуст."

    preview = tasks[:limit]
    extra = None
    if include_text and all(t.get("text") != include_text for t in preview):
        extra = next((t for t in tasks if t.get("text") == include_text), None)

    lines = [f"Превью задач на {format_date_ru(day)} (всего: {len(tasks)}):"]
    for t in preview:
        lines.append(f"{t.get('id')}) {t.get('text')}")
    if extra:
        lines.append("…")
        lines.append(f"{extra.get('id')}) {extra.get('text')}")
    return "\n".join(lines)


def render_overdue_backlog(items: List[Dict[str, Any]]) -> str:
    lines = ["⚠️ <b>Просроченные задачи в бэклоге</b>"]
    for item in items:
        created_at = format_date_ru(str(item.get("created_at", "")))
        lines.append(f"<b>{item.get('id')})</b> {item.get('text')} ({created_at})")
    lines.append("\nВыбери действие:")
    return "\n".join(lines)


def build_overdue_keyboard(items: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for item in items:
        item_id = item.get("id")
        rows.append(
            [
                InlineKeyboardButton("✂️ Сжать формулировку", callback_data=f"backlog:shorten:{item_id}"),
                InlineKeyboardButton("🗑 Удалить из backlog", callback_data=f"backlog:delete:{item_id}"),
                InlineKeyboardButton("↩️ Вернуть сегодня", callback_data=f"backlog:return:{item_id}"),
            ]
        )
    return InlineKeyboardMarkup(rows)


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
    backlog = get_backlog(state)
    existing_backlog_texts = {b.get("text") for b in backlog}

    # Закрываем текущий день
    day_obj["closed"] = True
    day_obj["closed_at"] = now_iso()

    # Готовим завтра
    tmr = tomorrow_str()
    tomorrow_obj = get_day(state, tmr)
    if tomorrow_obj.get("closed"):
        tomorrow_obj["closed"] = False

    # Переносим невыполненные (в статус todo) или отправляем в бэклог
    carry = []
    carry_report = []
    backlog_items = []
    for t in todo_tasks:
        carry_count = int(t.get("carry_count", 0) or 0) + 1
        if carry_count >= 3:
            if t.get("text") in existing_backlog_texts:
                existing = find_backlog_by_text(backlog, t.get("text"))
                if existing:
                    existing["carry_count"] = max(int(existing.get("carry_count", 0) or 0), carry_count)
                    existing["last_seen_at"] = now_iso()
            else:
                item = {
                    "id": next_backlog_id(backlog),
                    "text": t.get("text"),
                    "created_at": now_iso(),
                    "source_day": day,
                    "last_seen_at": now_iso(),
                    "carry_count": carry_count,
                }
                backlog.append(item)
                existing_backlog_texts.add(t.get("text"))
                backlog_items.append(item)
            continue

        carry.append(
            {
                "id": 0,
                "text": t["text"],
                "status": "todo",
                "created_at": now_iso(),
                "done_at": None,
                "carried_from": day,
                "carry_count": carry_count,
            }
        )
        carry_report.append(t)

    state["backlog"] = normalize_task_ids_backlog(backlog)

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
        f"🌙 <b>Итог дня {format_date_ru(day)}</b>",
        f"Сделано: <b>{len(done_tasks)}</b> / <b>{len(tasks)}</b>",
        "",
        "<b>✅ Выполнено:</b>" if done_tasks else "<b>✅ Выполнено:</b> —",
    ]
    if done_tasks:
        for t in done_tasks:
            lines.append(f"✅ {t['id']}) {t['text']}")

    if carry_report:
        lines += ["", "<b>⬜ Не сделано (перенёс на завтра):</b>"]
        for t in carry_report:
            lines.append(f"⬜ {t['id']}) {t['text']}")
    elif backlog_items:
        lines += ["", "<b>⬜ Не сделано (перенёс на завтра):</b> —"]
    else:
        lines += ["", "<b>⬜ Не сделано:</b> —"]

    if backlog_items:
        lines += ["", "<b>🗂 В бэклог:</b>"]
        for t in backlog_items:
            lines.append(f"🗂 {t.get('text')}")

    lines += ["", f"📌 <b>Черновик на завтра ({format_date_ru(tmr)}):</b>"]
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


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Куда добавить задачу?", reply_markup=build_add_keyboard())


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = load_user_state(user_id)

    day = today_str()
    day_obj = get_day(state, day)
    context.user_data["view_scope"] = "day"
    context.user_data["view_day"] = day
    if not day_obj.get("closed") and not day_obj.get("tasks"):
        create_default_plan(day_obj)

    save_user_state(user_id, state)
    await update.message.reply_text(
        render_plan(day, day_obj),
        parse_mode=ParseMode.HTML,
        reply_markup=build_today_keyboard(day_obj),
    )

    backlog = get_backlog(state)
    now = datetime.now()
    overdue_items = [item for item in backlog if is_backlog_overdue(item, now)]
    if overdue_items:
        await update.message.reply_text(
            render_overdue_backlog(overdue_items),
            parse_mode=ParseMode.HTML,
            reply_markup=build_overdue_keyboard(overdue_items),
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


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    user_id = update.effective_user.id

    button_labels = {
        "📌 План на сегодня",
        "📅 План по дате",
        "➕ Добавить задачу",
        "🗑 Удалить задачу",
        "🌙 Итог дня",
        "📦 Бэклог",
        "🗂 Бэклог",
        "❌ Отмена",
    }

    if text in button_labels:
        reset_input_modes(context)
        state = load_user_state(user_id)
        if "backlog_edit" in state:
            state.pop("backlog_edit", None)
            save_user_state(user_id, state)

        if text == "❌ Отмена":
            await update.message.reply_text("Ок, отменил.", reply_markup=build_start_keyboard())
            return
        if text == "📌 План на сегодня":
            await cmd_today(update, context)
            return
        if text == "📅 План по дате":
            await update.message.reply_text("Выбери режим:", reply_markup=build_date_mode_keyboard())
            return
        if text == "➕ Добавить задачу":
            await cmd_add(update, context)
            return
        if text == "🗑 Удалить задачу":
            view_scope = context.user_data.get("view_scope", "day")
            view_day = context.user_data.get("view_day") or today_str()
            context.user_data["del_mode"] = view_scope
            context.user_data["awaiting_del_id"] = True
            if view_scope == "backlog":
                await update.message.reply_text(
                    "Введи номер задачи для удаления (Бэклог).",
                    reply_markup=build_cancel_keyboard(),
                )
            else:
                await update.message.reply_text(
                    f"Введи номер задачи для удаления (План: {format_date_ru(view_day)})",
                    reply_markup=build_cancel_keyboard(),
                )
            return
        if text == "🌙 Итог дня":
            await cmd_evening(update, context)
            return
        if text in {"📦 Бэклог", "🗂 Бэклог"}:
            state = load_user_state(user_id)
            backlog = get_backlog(state)
            context.user_data["view_scope"] = "backlog"
            if not backlog:
                await update.message.reply_text("Бэклог пуст.")
                return
            message = "Нажми на задачу, чтобы перенести её на день. Для удаления — 🗑 и номер."
            message += "\n\n" + render_backlog_pick_list(backlog)
            await update.message.reply_text(
                message,
                parse_mode=ParseMode.HTML,
                reply_markup=build_backlog_pick_keyboard(backlog),
            )
            return

    if context.user_data.get("view_date_mode"):
        iso_date = parse_date_input(text)
        if not iso_date:
            await update.message.reply_text(
                "Введите дату в формате ДД.ММ.ГГГГ, например 05.02.2026",
                reply_markup=build_cancel_keyboard(),
            )
            return

        state = load_user_state(user_id)
        day_obj = get_day(state, iso_date)
        save_user_state(user_id, state)
        context.user_data.pop("view_date_mode", None)
        context.user_data["view_scope"] = "day"
        context.user_data["view_day"] = iso_date
        await update.message.reply_text(render_plan(iso_date, day_obj), parse_mode=ParseMode.HTML)
        return

    if context.user_data.get("awaiting_del_id"):
        try:
            task_id = int(text)
        except ValueError:
            await update.message.reply_text(
                "Номер должен быть числом. Введи номер задачи.",
                reply_markup=build_cancel_keyboard(),
            )
            return

        state = load_user_state(user_id)
        view_scope = context.user_data.get("view_scope", "day")
        if view_scope == "backlog":
            backlog = get_backlog(state)
            item = find_backlog_item(backlog, task_id)
            if not item:
                await update.message.reply_text(
                    f"Не нашёл задачу с номером {task_id}. Введи другой номер.",
                    reply_markup=build_cancel_keyboard(),
                )
                return
            backlog.remove(item)
            backlog[:] = normalize_task_ids_backlog(backlog)
            state["backlog"] = backlog
            save_user_state(user_id, state)
            reset_input_modes(context)
            context.user_data["view_scope"] = "backlog"
            await update.message.reply_text(f"🗑 Удалил задачу {task_id}) {item.get('text')}")
            await update.message.reply_text(f"📦 Сейчас в бэклоге: {len(backlog)}")
            await update.message.reply_text(render_backlog(backlog), parse_mode=ParseMode.HTML)
            return

        day = context.user_data.get("view_day") or today_str()
        day_obj = get_day(state, day)
        if day_obj.get("closed"):
            reset_input_modes(context)
            await update.message.reply_text(
                "Этот день закрыт. Перейти к плану на сегодня? Нажми 📌 План на сегодня."
            )
            return

        task = find_task(day_obj, task_id)
        if not task:
            await update.message.reply_text(
                f"Не нашёл задачу с номером {task_id}. Введи другой номер.",
                reply_markup=build_cancel_keyboard(),
            )
            return

        tasks = [t for t in day_obj.get("tasks", []) if t.get("id") != task_id]
        day_obj["tasks"] = normalize_task_ids(tasks)
        save_user_state(user_id, state)
        reset_input_modes(context)
        context.user_data["view_scope"] = "day"
        context.user_data["view_day"] = day
        await update.message.reply_text(f"🗑 Удалил задачу {task_id}) {task.get('text')}")
        await update.message.reply_text(
            render_plan(day, day_obj),
            parse_mode=ParseMode.HTML,
            reply_markup=build_today_keyboard(day_obj),
        )
        return

    if context.user_data.get("move_mode") == "date":
        iso_date = parse_date_input(text)
        if not iso_date:
            await update.message.reply_text(
                "Введите дату в формате ДД.ММ.ГГГГ, например 08.02.2026",
                reply_markup=build_cancel_keyboard(),
            )
            return

        task_id = context.user_data.get("move_task_id")
        if not task_id:
            context.user_data.pop("move_mode", None)
            context.user_data.pop("move_task_id", None)
            await update.message.reply_text("Не нашёл задачу для переноса.")
            return

        state = load_user_state(user_id)
        backlog = get_backlog(state)
        item = find_backlog_item(backlog, int(task_id))
        if not item:
            context.user_data.pop("move_mode", None)
            context.user_data.pop("move_task_id", None)
            await update.message.reply_text("Не нашёл задачу в бэклоге.")
            return

        day = iso_date
        day_obj = add_task_to_day(state, day, item.get("text"))
        backlog.remove(item)
        backlog[:] = normalize_task_ids_backlog(backlog)
        state["backlog"] = backlog
        save_user_state(user_id, state)
        context.user_data.pop("move_mode", None)
        context.user_data.pop("move_task_id", None)
        context.user_data["view_scope"] = "day"
        context.user_data["view_day"] = day
        await update.message.reply_text(
            f"✅ Перенёс на {format_date_ru(day)}: {item.get('text')}"
        )
        await update.message.reply_text(render_plan(day, day_obj), parse_mode=ParseMode.HTML)
        return

    add_mode = context.user_data.get("add_mode")
    if add_mode:
        if add_mode == "date" and not context.user_data.get("add_date"):
            iso_date = parse_date_input(text)
            if not iso_date:
                await update.message.reply_text(
                    "Введите дату в формате ДД.ММ.ГГГГ, например 05.02.2026",
                    reply_markup=build_cancel_keyboard(),
                )
                return
            context.user_data["add_date"] = iso_date
            await update.message.reply_text(
                "Напиши текст задачи одним сообщением.",
                reply_markup=build_cancel_keyboard(),
            )
            return

        state = load_user_state(user_id)
        if add_mode == "today":
            day = today_str()
            day_obj = get_day(state, day)
            if day_obj.get("closed"):
                context.user_data["pending_add_text"] = text
                context.user_data.pop("add_mode", None)
                context.user_data.pop("add_date", None)
                await update.message.reply_text(
                    "Сегодня уже закрыто. Куда добавить задачу?",
                    reply_markup=build_add_today_closed_keyboard(),
                )
                return

            day_obj = add_task_to_day(state, day, text)
            save_user_state(user_id, state)
            context.user_data.pop("add_mode", None)
            context.user_data.pop("add_date", None)
            context.user_data.pop("pending_add_text", None)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            await update.message.reply_text(
                render_plan(day, day_obj),
                parse_mode=ParseMode.HTML,
                reply_markup=build_today_keyboard(day_obj),
            )
            return
        if add_mode == "reopen_today":
            day = today_str()
            day_obj = {"tasks": [], "closed": False, "created_at": now_iso()}
            state.setdefault("days", {})[day] = day_obj
            create_default_plan(day_obj)
            day_obj = add_task_to_day(state, day, text)
            save_user_state(user_id, state)
            context.user_data.pop("add_mode", None)
            context.user_data.pop("add_date", None)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            await update.message.reply_text(
                render_plan(day, day_obj),
                parse_mode=ParseMode.HTML,
                reply_markup=build_today_keyboard(day_obj),
            )
            return
        if add_mode == "tomorrow":
            day = tomorrow_str()
            day_obj = add_task_to_day(state, day, text)
            save_user_state(user_id, state)
            context.user_data.pop("add_mode", None)
            context.user_data.pop("add_date", None)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            await update.message.reply_text(
                f"✅ Добавил задачу на {format_date_ru(day)}: {text}"
            )
            await update.message.reply_text(render_day_preview(day, day_obj, include_text=text))
            return
        if add_mode == "date":
            day = context.user_data.get("add_date")
            if not day:
                await update.message.reply_text(
                    "Введите дату в формате ДД.ММ.ГГГГ, например 05.02.2026",
                    reply_markup=build_cancel_keyboard(),
                )
                return
            day_obj = add_task_to_day(state, day, text)
            save_user_state(user_id, state)
            context.user_data.pop("add_mode", None)
            context.user_data.pop("add_date", None)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            await update.message.reply_text(
                f"✅ Добавил задачу на {format_date_ru(day)}: {text}"
            )
            await update.message.reply_text(render_day_preview(day, day_obj, include_text=text))
            return
        if add_mode == "backlog":
            backlog = get_backlog(state)
            backlog.append(
                {
                    "id": len(backlog) + 1,
                    "text": text,
                    "status": "todo",
                    "created_at": now_iso(),
                    "done_at": None,
                    "source_day": None,
                    "last_seen_at": now_iso(),
                    "carry_count": 0,
                }
            )
            backlog = normalize_task_ids_backlog(backlog)
            state["backlog"] = backlog
            save_user_state(user_id, state)
            context.user_data.pop("add_mode", None)
            context.user_data.pop("add_date", None)
            context.user_data["view_scope"] = "backlog"
            await update.message.reply_text(f"✅ Добавил в бэклог: {text}")
            await update.message.reply_text(f"📦 Сейчас в бэклоге: {len(backlog)}")
            await update.message.reply_text(render_backlog_tail(backlog))
            return

    state = load_user_state(user_id)
    pending = state.get("backlog_edit")
    if isinstance(pending, dict) and pending.get("id") is not None:
        try:
            item_id = int(pending.get("id"))
        except Exception:
            item_id = None

        backlog = get_backlog(state)
        item = find_backlog_item(backlog, item_id) if item_id is not None else None
        state.pop("backlog_edit", None)
        if item:
            item["text"] = text
            item["last_seen_at"] = now_iso()
            save_user_state(user_id, state)
            await update.message.reply_text("✂️ Обновил формулировку.")
            await update.message.reply_text(render_backlog(backlog), parse_mode=ParseMode.HTML)
        else:
            save_user_state(user_id, state)
            await update.message.reply_text("Не нашёл задачу в бэклоге.")
        return


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    if data == "cancel":
        reset_input_modes(context)
        await query.answer()
        await query.message.reply_text("Ок, отменил.", reply_markup=build_start_keyboard())
        return
    if data == "noop":
        await query.answer("Показаны первые 10 задач.", show_alert=True)
        return

    if data.startswith("date:"):
        parts = data.split(":")
        if len(parts) == 2 and parts[1] == "input":
            reset_input_modes(context)
            context.user_data["view_date_mode"] = True
            await query.answer()
            await query.message.reply_text(
                "Введи дату в формате ДД.ММ.ГГГГ",
                reply_markup=build_cancel_keyboard(),
            )
            return
        if len(parts) == 2 and parts[1] == "list":
            user_id = query.from_user.id
            state = load_user_state(user_id)
            days = state.get("days", {})
            dates = []
            for day_key, day_obj in days.items():
                tasks = day_obj.get("tasks", [])
                if tasks:
                    dates.append(day_key)
            dates = sorted(dates)[:10]
            await query.answer()
            if not dates:
                await query.message.reply_text("Нет дат с задачами.", reply_markup=build_cancel_keyboard())
                return
            lines = ["Даты с задачами:"]
            for iso in dates:
                count = len(days.get(iso, {}).get("tasks", []))
                lines.append(f"{format_date_ru(iso)} — {count} задач")
            await query.message.reply_text(
                "\n".join(lines),
                reply_markup=build_date_list_keyboard(dates),
            )
            return
        if len(parts) == 3 and parts[1] == "open":
            iso = parts[2]
            user_id = query.from_user.id
            state = load_user_state(user_id)
            day_obj = get_day(state, iso)
            save_user_state(user_id, state)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = iso
            await query.answer()
            if iso == today_str() and not day_obj.get("closed"):
                await query.message.reply_text(
                    render_plan(iso, day_obj),
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_today_keyboard(day_obj),
                )
            else:
                await query.message.reply_text(render_plan(iso, day_obj), parse_mode=ParseMode.HTML)
            return


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
        context.user_data["view_scope"] = "day"
        context.user_data["view_day"] = day

        ok, message = apply_done(day_obj, task_id)
        if not ok:
            await query.answer(message, show_alert=True)
            return

        save_user_state(user_id, state)
        await query.edit_message_text(
            render_plan(day, day_obj),
            parse_mode=ParseMode.HTML,
            reply_markup=build_today_keyboard(day_obj),
        )
        await query.answer(message)
        return

    if data.startswith("add:"):
        mode = data.split(":", 1)[1]
        if mode not in {"today", "tomorrow", "date", "backlog", "reopen_today"}:
            await query.answer("Неверный режим.", show_alert=True)
            return

        pending_text = context.user_data.pop("pending_add_text", None)

        if mode in {"tomorrow", "reopen_today"} and pending_text:
            user_id = query.from_user.id
            state = load_user_state(user_id)
            if mode == "reopen_today":
                day = today_str()
                day_obj = {"tasks": [], "closed": False, "created_at": now_iso()}
                state.setdefault("days", {})[day] = day_obj
                create_default_plan(day_obj)
                day_obj = add_task_to_day(state, day, pending_text)
                save_user_state(user_id, state)
                context.user_data["view_scope"] = "day"
                context.user_data["view_day"] = day
                await query.answer()
                await query.message.reply_text(
                    render_plan(day, day_obj),
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_today_keyboard(day_obj),
                )
                return

            day = tomorrow_str()
            day_obj = add_task_to_day(state, day, pending_text)
            save_user_state(user_id, state)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            await query.answer()
            await query.message.reply_text(
                f"✅ Добавил задачу на {format_date_ru(day)}: {pending_text}"
            )
            await query.message.reply_text(render_day_preview(day, day_obj, include_text=pending_text))
            return

        if mode == "reopen_today":
            context.user_data["add_mode"] = "reopen_today"
            context.user_data.pop("add_date", None)
            await query.answer()
            await query.message.reply_text(
                "Напиши текст задачи одним сообщением.",
                reply_markup=build_cancel_keyboard(),
            )
            return

        context.user_data["add_mode"] = mode
        context.user_data.pop("add_date", None)
        context.user_data.pop("del_mode", None)
        context.user_data.pop("awaiting_del_id", None)
        context.user_data.pop("move_mode", None)
        context.user_data.pop("move_task_id", None)
        await query.answer()
        if mode == "date":
            await query.message.reply_text(
                "Введите дату в формате ДД.ММ.ГГГГ, например 05.02.2026",
                reply_markup=build_cancel_keyboard(),
            )
        else:
            await query.message.reply_text(
                "Напиши текст задачи одним сообщением.",
                reply_markup=build_cancel_keyboard(),
            )
        return

    if data.startswith("pick:"):
        if data == "pick:back":
            context.user_data.pop("move_mode", None)
            context.user_data.pop("move_task_id", None)
            context.user_data.pop("add_mode", None)
            context.user_data.pop("add_date", None)
            context.user_data.pop("del_mode", None)
            context.user_data.pop("awaiting_del_id", None)
            await query.answer()
            await query.message.reply_text("Ок.")
            return

        try:
            item_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Неверный номер задачи.", show_alert=True)
            return

        user_id = query.from_user.id
        state = load_user_state(user_id)
        backlog = get_backlog(state)
        item = find_backlog_item(backlog, item_id)
        if not item:
            await query.answer("Задача не найдена в бэклоге.", show_alert=True)
            return

        context.user_data["move_task_id"] = item_id
        context.user_data.pop("move_mode", None)
        context.user_data.pop("add_mode", None)
        context.user_data.pop("add_date", None)
        context.user_data.pop("del_mode", None)
        context.user_data.pop("awaiting_del_id", None)
        await query.answer()
        await query.message.reply_text("Куда перенести задачу?", reply_markup=build_move_keyboard(item_id))
        return

    if data.startswith("move:"):
        parts = data.split(":")
        if len(parts) == 2 and parts[1] == "cancel":
            reset_input_modes(context)
            await query.answer()
            await query.message.reply_text("Ок, отменил.", reply_markup=build_start_keyboard())
            return

        if len(parts) != 3:
            await query.answer("Неверные данные.", show_alert=True)
            return

        try:
            item_id = int(parts[1])
        except ValueError:
            await query.answer("Неверный номер задачи.", show_alert=True)
            return

        target = parts[2]
        if target == "date":
            context.user_data["move_mode"] = "date"
            context.user_data["move_task_id"] = item_id
            context.user_data.pop("add_mode", None)
            context.user_data.pop("add_date", None)
            context.user_data.pop("del_mode", None)
            context.user_data.pop("awaiting_del_id", None)
            await query.answer()
            await query.message.reply_text(
                "Введите дату в формате ДД.ММ.ГГГГ, например 08.02.2026",
                reply_markup=build_cancel_keyboard(),
            )
            return

        if target not in {"today", "tomorrow"}:
            await query.answer("Неверное направление.", show_alert=True)
            return

        user_id = query.from_user.id
        state = load_user_state(user_id)
        backlog = get_backlog(state)
        item = find_backlog_item(backlog, item_id)
        if not item:
            await query.answer("Задача не найдена в бэклоге.", show_alert=True)
            return

        day = today_str() if target == "today" else tomorrow_str()
        day_obj = get_day(state, day)
        if target == "today" and day_obj.get("closed"):
            await query.answer("День уже закрыт. Напиши /today чтобы открыть новый.", show_alert=True)
            return

        day_obj = add_task_to_day(state, day, item.get("text"))
        backlog.remove(item)
        backlog[:] = normalize_task_ids_backlog(backlog)
        state["backlog"] = backlog
        save_user_state(user_id, state)
        context.user_data.pop("move_mode", None)
        context.user_data.pop("move_task_id", None)
        context.user_data["view_scope"] = "day"
        context.user_data["view_day"] = day
        await query.answer()
        if target == "today":
            await query.message.reply_text(
                render_plan(day, day_obj),
                parse_mode=ParseMode.HTML,
                reply_markup=build_today_keyboard(day_obj),
            )
        else:
            await query.message.reply_text(
                f"✅ Перенёс на {format_date_ru(day)}: {item.get('text')}"
            )
            await query.message.reply_text(render_day_preview(day, day_obj, include_text=item.get("text")))
        return

    if data.startswith("backlog:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Неверные данные.", show_alert=True)
            return

        action = parts[1]
        try:
            item_id = int(parts[2])
        except ValueError:
            await query.answer("Неверный номер задачи.", show_alert=True)
            return

        user_id = query.from_user.id
        state = load_user_state(user_id)
        backlog = get_backlog(state)
        item = find_backlog_item(backlog, item_id)
        if not item:
            await query.answer("Задача не найдена в бэклоге.", show_alert=True)
            return

        if action == "shorten":
            state["backlog_edit"] = {"id": item_id, "action": "shorten"}
            save_user_state(user_id, state)
            await query.answer("Жду новую формулировку.")
            await query.message.reply_text("Напиши новую формулировку задачи.")
            return

        if action == "delete":
            backlog.remove(item)
            backlog[:] = normalize_task_ids_backlog(backlog)
            save_user_state(user_id, state)
            await query.answer("Удалил из бэклога.")
            await query.message.reply_text("🗑 Удалил из бэклога.")
            return

        if action == "return":
            day = today_str()
            day_obj = get_day(state, day)
            if day_obj.get("closed"):
                await query.answer("День уже закрыт. Напиши /today чтобы открыть новый.", show_alert=True)
                return

            tasks = day_obj.get("tasks", [])
            task_texts = {t.get("text") for t in tasks}
            if item.get("text") not in task_texts:
                tasks.append(
                    {
                        "id": len(tasks) + 1,
                        "text": item.get("text"),
                        "status": "todo",
                        "created_at": item.get("created_at", now_iso()),
                        "done_at": None,
                        "carried_from": item.get("source_day"),
                        "carry_count": int(item.get("carry_count", 0) or 0),
                    }
                )
                day_obj["tasks"] = normalize_task_ids(tasks)

            backlog.remove(item)
            backlog[:] = normalize_task_ids_backlog(backlog)
            save_user_state(user_id, state)
            await query.message.reply_text(
                render_plan(day, day_obj),
                parse_mode=ParseMode.HTML,
                reply_markup=build_today_keyboard(day_obj),
            )
            await query.answer("Вернул в план на сегодня.")
            return

        await query.answer("Неизвестное действие.", show_alert=True)
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    print("Bot is running... Press Ctrl+C to stop.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
