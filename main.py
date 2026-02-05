import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time
import html
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

HABITS_DEFAULT_CONFIG = [
    {"key": "study", "title": "Учёба"},
    {"key": "music", "title": "Музыка"},
    {"key": "sport", "title": "Спорт"},
    {"key": "reading", "title": "Чтение"},
    {"key": "vitamin_d", "title": "Витамин D"},
    {"key": "breathing", "title": "Дыхание"},
    {"key": "automation", "title": "Автоматизация"},
]

BACKLOG_TTL_DAYS = 7


def today_str() -> str:
    return date.today().isoformat()


def tomorrow_str() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

DATE_INPUT_ERROR = "Введите дату в формате ДД.ММ.ГГГГ, например 05.02.2026"


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


def parse_date_input_ru(value: str) -> Optional[str]:
    raw = value.strip()
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    day_s, month_s, year_s = parts
    if len(day_s) != 2 or len(month_s) != 2 or len(year_s) != 4:
        return None
    if not (day_s.isdigit() and month_s.isdigit() and year_s.isdigit()):
        return None
    try:
        return date(int(year_s), int(month_s), int(day_s)).isoformat()
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


def render_plan(day: str, day_obj: Dict[str, Any], show_hint: bool = False) -> str:
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
    if show_hint:
        lines.append("\nОтмечай выполненное кнопками ниже.")
    return "\n".join(lines)


def build_start_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["📌 План на сегодня", "➕ Добавить задачу"],
            ["📦 Бэклог", "📅 План по дате"],
            ["🗑 Удалить задачу", "✅ Привычки"],
            ["🔔 Уведомления", "🌙 Итог дня"],
            ["🏠 Главная"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def build_today_keyboard(day_obj: Dict[str, Any]) -> InlineKeyboardMarkup:
    if day_obj.get("closed"):
        return InlineKeyboardMarkup([])
    rows = []
    tasks = day_obj.get("tasks", [])
    todo_tasks = [t for t in tasks if t.get("status") == "todo"]
    for t in todo_tasks[:10]:
        task_id = t.get("id")
        label = f"⬜ {task_id}. {shorten_text(str(t.get('text', '')), 34)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"done:{task_id}")])
    if len(todo_tasks) > 10:
        rows.append([InlineKeyboardButton("...ещё", callback_data="noop")])
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


def build_today_closed_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🆕 Открыть новый день", callback_data="day:reopen_today")],
            [InlineKeyboardButton("📅 Показать завтра", callback_data="day:show_tomorrow")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
        ]
    )


def build_backlog_take_keyboard(backlog: List[Dict[str, Any]], limit: int = 5) -> InlineKeyboardMarkup:
    rows = []
    items = sorted(backlog, key=backlog_sort_key)[:limit]
    for item in items:
        label = shorten_text(str(item.get("text", "")), 36)
        rows.append([InlineKeyboardButton(label, callback_data=f"pick:{item.get('id')}")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def build_pick_to_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Завтра", callback_data=f"pick_to:tomorrow:{item_id}")],
            [InlineKeyboardButton("Открыть новый день", callback_data=f"pick_to:reopen_today:{item_id}")],
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


def parse_task_ids_input(text: str) -> Optional[List[int]]:
    parts = re.split(r"[,\s]+", text.strip())
    parts = [p for p in parts if p]
    if not parts:
        return None
    try:
        return [int(p) for p in parts]
    except ValueError:
        return None


def build_delete_day_keyboard(state: Dict[str, Any], active_day: Optional[str]) -> InlineKeyboardMarkup:
    days = state.get("days", {})
    days_with_tasks = [d for d, obj in days.items() if obj.get("tasks")]
    if not days_with_tasks:
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])

    rows = []
    days_sorted = sorted(days_with_tasks, reverse=True)
    if active_day and active_day in days_sorted:
        rows.append(
            [InlineKeyboardButton(f"Текущая: {format_date_ru(active_day)}", callback_data=f"del_pick_day:{active_day}")]
        )
        days_sorted.remove(active_day)

    today = today_str()
    tomorrow = tomorrow_str()
    if today in days_sorted:
        rows.append([InlineKeyboardButton("Сегодня", callback_data=f"del_pick_day:{today}")])
        days_sorted.remove(today)
    if tomorrow in days_sorted:
        rows.append([InlineKeyboardButton("Завтра", callback_data=f"del_pick_day:{tomorrow}")])
        days_sorted.remove(tomorrow)

    for day in days_sorted[:7]:
        rows.append([InlineKeyboardButton(format_date_ru(day), callback_data=f"del_pick_day:{day}")])

    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def build_delete_tasks_keyboard(day: str, day_obj: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows = []
    for task in day_obj.get("tasks", []):
        label = f"🗑 {task.get('id')}) {shorten_text(str(task.get('text', '')), 32)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"del_one:{day}:{task.get('id')}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="del_back")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def get_triage_items(backlog: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    return sorted(backlog, key=backlog_sort_key)[:limit]


def render_triage_list(backlog: List[Dict[str, Any]], limit: int = 3) -> str:
    if not backlog:
        return "📦 Бэклог пуст."
    items = get_triage_items(backlog, limit)
    lines = ["🧹 Разобрать бэклог:"]
    for item in items:
        lines.append(f"{item.get('id')}) {item.get('text')}")
    return "\n".join(lines)


def build_triage_keyboard(backlog: List[Dict[str, Any]], limit: int = 3) -> InlineKeyboardMarkup:
    rows = []
    items = get_triage_items(backlog, limit)
    for item in items:
        label = shorten_text(str(item.get("text", "")), 34)
        rows.append([InlineKeyboardButton(label, callback_data=f"triage:{item.get('id')}")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="triage:cancel")])
    return InlineKeyboardMarkup(rows)


def build_triage_to_keyboard(item_id: int, include_today: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if include_today:
        rows.append([InlineKeyboardButton("Сегодня", callback_data=f"triage_to:today:{item_id}")])
    rows.append([InlineKeyboardButton("Завтра", callback_data=f"triage_to:tomorrow:{item_id}")])
    rows.append([InlineKeyboardButton("Выбрать дату", callback_data=f"triage_to:date:{item_id}")])
    rows.append([InlineKeyboardButton("Удалить", callback_data=f"triage_to:delete:{item_id}")])
    rows.append([InlineKeyboardButton("Назад", callback_data="triage:back")])
    return InlineKeyboardMarkup(rows)


def backlog_item_date_label(item: Dict[str, Any]) -> str:
    day = item.get("source_day") or item.get("carried_from")
    if day:
        try:
            return date.fromisoformat(str(day)).strftime("%d.%m")
        except Exception:
            return format_date_ru(str(day))
    created_at = item.get("created_at")
    dt = parse_iso(str(created_at)) if created_at else None
    if dt:
        return dt.strftime("%d.%m")
    return "--.--"


def render_backlog_pick_list(backlog: List[Dict[str, Any]], limit: int = 10) -> str:
    if not backlog:
        return "📦 Бэклог пуст."
    items = backlog[-limit:]
    lines = [f"📦 <b>Бэклог</b> ({len(backlog)} задач)"]
    lines.append("")
    for item in items:
        date_tag = backlog_item_date_label(item)
        lines.append(f"{item.get('id')}) {item.get('text')} [{date_tag}]")
    return "\n".join(lines)


def build_backlog_pick_keyboard(backlog: List[Dict[str, Any]], limit: int = 10) -> InlineKeyboardMarkup:
    rows = []
    items = backlog[-limit:]
    for item in items:
        item_id = item.get("id")
        date_tag = backlog_item_date_label(item)
        label = f"{shorten_text(str(item.get('text', '')), 24)} [{date_tag}]"
        rows.append([InlineKeyboardButton(label, callback_data=f"pick:{item_id}")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def build_move_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Сегодня", callback_data=f"move:{item_id}:today")],
            [InlineKeyboardButton("Завтра", callback_data=f"move:{item_id}:tomorrow")],
            [InlineKeyboardButton("Выбрать дату", callback_data=f"move:{item_id}:date")],
            [InlineKeyboardButton("Удалить", callback_data=f"move:{item_id}:delete")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="move:back")],
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
    context.user_data.pop("del_day", None)
    context.user_data.pop("add_mode", None)
    context.user_data.pop("add_date", None)
    context.user_data.pop("awaiting_task_text", None)
    context.user_data.pop("move_mode", None)
    context.user_data.pop("move_task_id", None)
    context.user_data.pop("view_date_mode", None)
    context.user_data.pop("pending_add_text", None)
    context.user_data.pop("take_mode", None)
    context.user_data.pop("triage_mode", None)
    context.user_data.pop("triage_date_mode", None)
    context.user_data.pop("triage_task_id", None)
    context.user_data.pop("view_scope", None)
    context.user_data.pop("view_day", None)
    context.user_data.pop("active_day", None)
    context.user_data.pop("awaiting_habit_title", None)
    context.user_data.pop("habits_selected_date", None)
    context.user_data.pop("habits_selected_day", None)
    context.user_data.pop("habits_week_start", None)


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


def get_habits_config(state: Dict[str, Any]) -> List[Dict[str, str]]:
    config = state.get("habits_config")
    if not isinstance(config, list) or not config:
        state["habits_config"] = [dict(item) for item in HABITS_DEFAULT_CONFIG]
        config = state["habits_config"]
    return config


def get_habits_log(state: Dict[str, Any]) -> Dict[str, Dict[str, bool]]:
    log = state.get("habits_log")
    if not isinstance(log, dict):
        log = {}
        state["habits_log"] = log
    return log


def week_start_for(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_dates_for(start: date) -> List[date]:
    return [start + timedelta(days=i) for i in range(7)]


def render_habits_week(state: Dict[str, Any], week_start: date, selected_date: date) -> str:
    config = get_habits_config(state)
    log = get_habits_log(state)
    week_dates = week_dates_for(week_start)
    start_iso = week_dates[0].isoformat()
    end_iso = week_dates[-1].isoformat()

    titles = [str(h.get("title", "")) for h in config]
    max_title = max([len(t) for t in titles] + [5])
    day_labels = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
    selected_idx = None
    if week_dates[0] <= selected_date <= week_dates[-1]:
        selected_idx = (selected_date - week_dates[0]).days
    header_cells = []
    for idx, label in enumerate(day_labels):
        if selected_idx == idx:
            header_cells.append(f"【{label}】")
        else:
            header_cells.append(f" {label} ")
    header = " " * (max_title + 1) + "".join(f"{cell:<4}" for cell in header_cells)

    lines = [header]
    for habit in config:
        title = str(habit.get("title", ""))
        key = str(habit.get("key", ""))
        row = [title.ljust(max_title)]
        for d in week_dates:
            iso = d.isoformat()
            day_log = log.get(iso, {}) if isinstance(log.get(iso, {}), dict) else {}
            if key not in day_log:
                mark = "⬜"
            else:
                mark = "🟩" if day_log.get(key, False) else "🟥"
            row.append(f"{mark} ")
        line = f"{row[0]} " + "".join(f"{cell:<4}" for cell in row[1:])
        lines.append(line)

    header_line = f"✅ Привычки — неделя {format_date_ru(start_iso)}–{format_date_ru(end_iso)}"
    edit_line = f"Выбран день: {format_date_ru(selected_date.isoformat())}"
    table = "\n".join(lines)
    hint = "Нажми на привычку, чтобы отметить за выбранный день."
    return f"{html.escape(header_line)}\n{html.escape(edit_line)}\n<pre>{html.escape(table)}</pre>\n{html.escape(hint)}"


def build_habits_keyboard(state: Dict[str, Any], selected_date: date) -> InlineKeyboardMarkup:
    config = get_habits_config(state)
    log = get_habits_log(state)
    selected_iso = selected_date.isoformat()
    rows = []
    for habit in config:
        key = str(habit.get("key", ""))
        title = str(habit.get("title", ""))
        day_log = log.get(selected_iso, {}) if isinstance(log.get(selected_iso, {}), dict) else {}
        if key not in day_log:
            mark = "⬜"
        else:
            mark = "🟩" if day_log.get(key, False) else "🟥"
        label = f"{mark} {title}"
        rows.append([InlineKeyboardButton(label, callback_data=f"hab:toggle:{key}")])
    rows.append(
        [
            InlineKeyboardButton("📅 Выбрать день", callback_data="hab:pick_day"),
            InlineKeyboardButton("⚙️ Настроить", callback_data="hab:settings"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_habits_settings_keyboard(week_start: date, selected_date: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 Выбрать день", callback_data="hab:pick_day")],
            [
                InlineKeyboardButton("⬅️ Неделя -1", callback_data="hab:week_prev"),
                InlineKeyboardButton("➡️ Неделя +1", callback_data="hab:week_next"),
            ],
            [InlineKeyboardButton("➕ Добавить привычку", callback_data="hab:add")],
            [InlineKeyboardButton("🗑 Удалить привычку", callback_data="hab:del")],
            [InlineKeyboardButton("↩️ Назад", callback_data="hab:back")],
        ]
    )


def build_habits_delete_keyboard(config: List[Dict[str, str]]) -> InlineKeyboardMarkup:
    rows = []
    for habit in config:
        key = str(habit.get("key", ""))
        title = str(habit.get("title", ""))
        rows.append([InlineKeyboardButton(title, callback_data=f"hab:del:{key}")])
    rows.append([InlineKeyboardButton("↩️ Назад", callback_data="hab:back")])
    return InlineKeyboardMarkup(rows)


def build_habits_day_picker_keyboard(week_start: date) -> InlineKeyboardMarkup:
    week_dates = week_dates_for(week_start)
    day_labels = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    day_codes = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    buttons = []
    for label, code in zip(day_labels, day_codes):
        buttons.append(InlineKeyboardButton(label, callback_data=f"habits_day:{code}"))
    rows = [buttons[:4], buttons[4:]]
    rows.append([InlineKeyboardButton("Сегодня", callback_data="habits_day:today")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="habits_day:cancel")])
    return InlineKeyboardMarkup(rows)


def render_habits_settings_text(week_start: date, selected_date: date) -> str:
    start_iso = week_start.isoformat()
    end_iso = (week_start + timedelta(days=6)).isoformat()
    return (
        "Настройки привычек\n"
        f"Неделя: {format_date_ru(start_iso)}–{format_date_ru(end_iso)}\n"
        f"Выбран день: {format_date_ru(selected_date.isoformat())}"
    )


def normalize_habit_key(title: str, existing: set[str]) -> str:
    base = title.strip().lower()
    base = re.sub(r"\s+", "_", base)
    base = re.sub(r"[^a-z0-9_]", "", base)
    if not base:
        base = f"custom_{int(datetime.now().timestamp())}"
    key = base
    idx = 2
    while key in existing:
        key = f"{base}_{idx}"
        idx += 1
    return key


def get_notifications(state: Dict[str, Any]) -> Dict[str, Any]:
    cfg = state.get("notifications")
    if not isinstance(cfg, dict):
        cfg = {"enabled": False, "morning": "09:00", "evening": "21:30"}
        state["notifications"] = cfg
    cfg.setdefault("enabled", False)
    cfg.setdefault("morning", "09:00")
    cfg.setdefault("evening", "21:30")
    return cfg


def build_notifications_keyboard(state: Dict[str, Any]) -> InlineKeyboardMarkup:
    cfg = get_notifications(state)
    enabled = bool(cfg.get("enabled"))
    toggle_label = "❌ Выключить" if enabled else "✅ Включить"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(toggle_label, callback_data="notif:toggle")],
            [InlineKeyboardButton("⏰ Утро 09:00", callback_data="notif:morning")],
            [InlineKeyboardButton("🌙 Вечер 21:30", callback_data="notif:evening")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="notif:back")],
        ]
    )


def parse_time_hhmm(value: str) -> Optional[time]:
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except Exception:
        return None


def remove_jobs(job_queue, name: str) -> None:
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()


def schedule_notifications(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int, state: Dict[str, Any]
) -> None:
    job_queue = context.application.job_queue
    if not job_queue:
        return
    remove_jobs(job_queue, f"notify_morning_{user_id}")
    remove_jobs(job_queue, f"notify_evening_{user_id}")

    cfg = get_notifications(state)
    if not cfg.get("enabled"):
        return

    morning_time = parse_time_hhmm(str(cfg.get("morning", "09:00")))
    evening_time = parse_time_hhmm(str(cfg.get("evening", "21:30")))
    if morning_time:
        job_queue.run_daily(
            notify_morning,
            time=morning_time,
            name=f"notify_morning_{user_id}",
            data={"user_id": user_id, "chat_id": chat_id},
        )
    if evening_time:
        job_queue.run_daily(
            notify_evening,
            time=evening_time,
            name=f"notify_evening_{user_id}",
            data={"user_id": user_id, "chat_id": chat_id},
        )


def has_any_habit_done(state: Dict[str, Any], date_iso: str) -> bool:
    log = get_habits_log(state)
    day_log = log.get(date_iso)
    if not isinstance(day_log, dict) or not day_log:
        return False
    return any(bool(v) for v in day_log.values())


async def notify_morning(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    if not chat_id:
        return
    reply_kb = ReplyKeyboardMarkup([["📌 План на сегодня"]], resize_keyboard=True, one_time_keyboard=False)
    await context.bot.send_message(
        chat_id=chat_id,
        text="Доброе утро! Готов собрать план на сегодня?",
        reply_markup=reply_kb,
    )


async def notify_evening(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    user_id = data.get("user_id")
    chat_id = data.get("chat_id")
    if not chat_id or not user_id:
        return
    state = load_user_state(int(user_id))
    today_iso = today_str()
    if has_any_habit_done(state, today_iso):
        return
    reply_kb = ReplyKeyboardMarkup([["✅ Привычки"]], resize_keyboard=True, one_time_keyboard=False)
    await context.bot.send_message(
        chat_id=chat_id,
        text="Вечерний чек‑ин: отметь привычки за сегодня.",
        reply_markup=reply_kb,
    )


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

    # Привычки за сегодня
    habits_config = get_habits_config(state)
    habits_log = get_habits_log(state)
    day_log = habits_log.get(day, {}) if isinstance(habits_log.get(day, {}), dict) else {}
    done_count = 0
    lines += ["", "✅ <b>Привычки за сегодня:</b>"]
    for habit in habits_config:
        key = str(habit.get("key", ""))
        title = str(habit.get("title", ""))
        if key not in day_log:
            mark = "⬜"
        else:
            mark = "🟩" if day_log.get(key, False) else "🟥"
        if day_log.get(key, False):
            done_count += 1
        lines.append(f"{mark} {title}")
    lines.append(f"Итого: <b>{done_count}</b>/<b>{len(habits_config)}</b> выполнено")

    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(get_start_message(), reply_markup=build_start_keyboard())


def get_start_message() -> str:
    return (
        "Привет! Я твой PM-бот (MVP v0.1).\n\n"
        "Как пользуемся:\n"
        "1) С утра собери план на день\n"
        "2) В течение дня отмечай выполненное\n"
        "3) Вечером подведём итог и перенесём остаток\n\n"
        "Жми кнопки ниже — это самый быстрый путь."
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Куда добавить задачу?", reply_markup=build_add_keyboard())


async def cmd_habits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = load_user_state(user_id)
    today = date.today()
    week_start = week_start_for(today)
    log = get_habits_log(state)
    log.setdefault(today.isoformat(), {})
    save_user_state(user_id, state)
    context.user_data["view_scope"] = "habits"
    context.user_data["habits_selected_day"] = today.isoformat()
    context.user_data["habits_week_start"] = week_start.isoformat()
    await update.message.reply_text(
        render_habits_week(state, week_start, today),
        parse_mode=ParseMode.HTML,
        reply_markup=build_habits_keyboard(state, today),
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = load_user_state(user_id)

    day = today_str()
    day_obj = get_day(state, day)
    context.user_data["view_scope"] = "day"
    context.user_data["view_day"] = day
    context.user_data["active_day"] = day

    if day_obj.get("closed"):
        save_user_state(user_id, state)
        await update.message.reply_text(
            f"📌 План на {format_date_ru(day)} (день закрыт)",
            reply_markup=build_today_closed_keyboard(),
        )
        return

    if not day_obj.get("tasks"):
        create_default_plan(day_obj)

    save_user_state(user_id, state)
    await update.message.reply_text(
        render_plan(day, day_obj, show_hint=True),
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
        "🏠 Главная",
        "📌 План на сегодня",
        "📅 План по дате",
        "➕ Добавить задачу",
        "🗑 Удалить задачу",
        "🌙 Итог дня",
        "📦 Бэклог",
        "✅ Привычки",
        "🔔 Уведомления",
        "🗂 Бэклог",
        "❌ Отмена",
    }

    if text == "🏠 Главная":
        reset_input_modes(context)
        state = load_user_state(user_id)
        if "backlog_edit" in state:
            state.pop("backlog_edit", None)
            save_user_state(user_id, state)
        await cmd_start(update, context)
        return

    if text == "❌ Отмена":
        reset_input_modes(context)
        await update.message.reply_text("Ок, отменил.", reply_markup=build_start_keyboard())
        return

    if context.user_data.get("awaiting_task_text") and text in button_labels:
        await update.message.reply_text(
            "Сейчас добавление задачи. Пришли текст одним сообщением или нажми ❌ Отмена.",
            reply_markup=build_cancel_keyboard(),
        )
        return

    if text in button_labels:
        reset_input_modes(context)
        state = load_user_state(user_id)
        if "backlog_edit" in state:
            state.pop("backlog_edit", None)
            save_user_state(user_id, state)

        if text == "📌 План на сегодня":
            await cmd_today(update, context)
            return
        if text == "📅 План по дате":
            await update.message.reply_text("Выбери режим:", reply_markup=build_date_mode_keyboard())
            return
        if text == "✅ Привычки":
            await cmd_habits(update, context)
            return
        if text == "🔔 Уведомления":
            state = load_user_state(user_id)
            cfg = get_notifications(state)
            save_user_state(user_id, state)
            text_msg = (
                f"🔔 Уведомления: {'включены' if cfg.get('enabled') else 'выключены'}\n"
                f"Утро: {cfg.get('morning')}\n"
                f"Вечер: {cfg.get('evening')}"
            )
            await update.message.reply_text(
                text_msg,
                reply_markup=build_notifications_keyboard(state),
            )
            return
        if text == "➕ Добавить задачу":
            await cmd_add(update, context)
            return
        if text == "🗑 Удалить задачу":
            active_day = context.user_data.get("active_day")
            state = load_user_state(user_id)
            kb = build_delete_day_keyboard(state, active_day)
            has_tasks = any(obj.get("tasks") for obj in state.get("days", {}).values())
            if not has_tasks:
                await update.message.reply_text("Нет задач для удаления.")
                return
            context.user_data["del_mode"] = "day"
            context.user_data.pop("awaiting_del_id", None)
            context.user_data.pop("del_day", None)
            await update.message.reply_text(
                "Выбери день, где удалить задачи",
                reply_markup=kb,
            )
            return
        if text == "🌙 Итог дня":
            await cmd_evening(update, context)
            return
        if text in {"📦 Бэклог", "🗂 Бэклог"}:
            state = load_user_state(user_id)
            backlog = get_backlog(state)
            context.user_data["view_scope"] = "backlog"
            context.user_data["take_mode"] = False
            if not backlog:
                await update.message.reply_text("Бэклог пуст.")
                return
            message = "Нажми на задачу, чтобы перенести в план или удалить."
            message += "\n\n" + render_backlog_pick_list(backlog)
            await update.message.reply_text(
                message,
                parse_mode=ParseMode.HTML,
                reply_markup=build_backlog_pick_keyboard(backlog),
            )
            return
        # управление бэклогом доступно внутри экрана "📦 Бэклог"

    if context.user_data.get("view_date_mode"):
        iso_date = parse_date_input_ru(text)
        if not iso_date:
            await update.message.reply_text(DATE_INPUT_ERROR, reply_markup=build_cancel_keyboard())
            return

        state = load_user_state(user_id)
        day_obj = get_day(state, iso_date)
        save_user_state(user_id, state)
        context.user_data.pop("view_date_mode", None)
        context.user_data["view_scope"] = "day"
        context.user_data["view_day"] = iso_date
        context.user_data["active_day"] = iso_date
        await update.message.reply_text(render_plan(iso_date, day_obj), parse_mode=ParseMode.HTML)
        return

    if context.user_data.get("awaiting_del_id"):
        ids = parse_task_ids_input(text)
        if not ids:
            await update.message.reply_text(
                "Введите номер(а) задачи: 2 или 2,4,5",
                reply_markup=build_cancel_keyboard(),
            )
            return

        state = load_user_state(user_id)
        day = context.user_data.get("del_day") or context.user_data.get("active_day") or today_str()
        day_obj = get_day(state, day)
        if day_obj.get("closed"):
            reset_input_modes(context)
            await update.message.reply_text("День закрыт (история). Удалять нельзя.")
            return
        tasks = day_obj.get("tasks", [])
        removed = [t for t in tasks if t.get("id") in ids]
        if not removed:
            await update.message.reply_text(
                "Не нашёл задачи с такими номерами. Введи другие номера.",
                reply_markup=build_cancel_keyboard(),
            )
            return

        tasks = [t for t in tasks if t.get("id") not in ids]
        day_obj["tasks"] = normalize_task_ids(tasks)
        save_user_state(user_id, state)
        reset_input_modes(context)
        context.user_data["view_scope"] = "day"
        context.user_data["view_day"] = day
        context.user_data["active_day"] = day
        removed_text = ", ".join(f"{t.get('id')}) {t.get('text')}" for t in removed)
        await update.message.reply_text(f"🗑 Удалил задачи: {removed_text}")
        reply_markup = build_today_keyboard(day_obj) if day == today_str() and not day_obj.get("closed") else None
        await update.message.reply_text(
            render_plan(day, day_obj, show_hint=bool(reply_markup)),
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
        return

    if context.user_data.get("awaiting_habit_title"):
        if text in button_labels:
            await update.message.reply_text(
                "Сейчас добавление привычки. Пришли текст одним сообщением или нажми ❌ Отмена.",
                reply_markup=build_cancel_keyboard(),
            )
            return
        title = text.strip()
        if not title:
            await update.message.reply_text(
                "Название не должно быть пустым. Пришли текст одним сообщением.",
                reply_markup=build_cancel_keyboard(),
            )
            return
        state = load_user_state(user_id)
        config = get_habits_config(state)
        existing = {str(h.get("key", "")) for h in config}
        key = normalize_habit_key(title, existing)
        config.append({"key": key, "title": title})
        state["habits_config"] = config
        save_user_state(user_id, state)
        context.user_data.pop("awaiting_habit_title", None)
        today = date.today()
        week_start_iso = context.user_data.get("habits_week_start")
        active_iso = context.user_data.get("habits_selected_day") or context.user_data.get("habits_selected_date")
        try:
            week_start = date.fromisoformat(week_start_iso) if week_start_iso else week_start_for(today)
        except Exception:
            week_start = week_start_for(today)
        try:
            active_date = date.fromisoformat(active_iso) if active_iso else today
        except Exception:
            active_date = today
        context.user_data["habits_week_start"] = week_start.isoformat()
        context.user_data["habits_selected_day"] = active_date.isoformat()
        await update.message.reply_text(f"✅ Добавил привычку: {title}")
        await update.message.reply_text(
            render_habits_week(state, week_start_for(active_date), active_date),
            parse_mode=ParseMode.HTML,
            reply_markup=build_habits_keyboard(state, active_date),
        )
        return

    if context.user_data.get("triage_date_mode"):
        iso_date = parse_date_input_ru(text)
        if not iso_date:
            await update.message.reply_text(DATE_INPUT_ERROR, reply_markup=build_cancel_keyboard())
            return
        task_id = context.user_data.get("triage_task_id")
        if not task_id:
            context.user_data.pop("triage_date_mode", None)
            context.user_data.pop("triage_task_id", None)
            await update.message.reply_text("Не нашёл задачу для переноса.")
            return

        state = load_user_state(user_id)
        backlog = get_backlog(state)
        item = find_backlog_item(backlog, int(task_id))
        if not item:
            context.user_data.pop("triage_date_mode", None)
            context.user_data.pop("triage_task_id", None)
            await update.message.reply_text("Не нашёл задачу в бэклоге.")
            return

        day_obj = add_task_to_day(state, iso_date, item.get("text"))
        backlog.remove(item)
        backlog[:] = normalize_task_ids_backlog(backlog)
        state["backlog"] = backlog
        save_user_state(user_id, state)
        context.user_data.pop("triage_date_mode", None)
        context.user_data.pop("triage_task_id", None)
        await update.message.reply_text(
            f"✅ Перенёс на {format_date_ru(iso_date)}: {item.get('text')}"
        )
        if backlog:
            await update.message.reply_text(
                render_triage_list(backlog),
                reply_markup=build_triage_keyboard(backlog),
            )
        else:
            await update.message.reply_text("📦 Бэклог пуст.")
        return

    if context.user_data.get("move_mode") == "date":
        iso_date = parse_date_input_ru(text)
        if not iso_date:
            await update.message.reply_text(DATE_INPUT_ERROR, reply_markup=build_cancel_keyboard())
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
        context.user_data["active_day"] = day
        await update.message.reply_text(
            f"✅ Перенёс на {format_date_ru(day)}: {item.get('text')}"
        )
        await update.message.reply_text(render_plan(day, day_obj), parse_mode=ParseMode.HTML)
        return

    add_mode = context.user_data.get("add_mode")
    if add_mode:
        if add_mode == "date" and not context.user_data.get("add_date"):
            iso_date = parse_date_input_ru(text)
            if not iso_date:
                await update.message.reply_text(DATE_INPUT_ERROR, reply_markup=build_cancel_keyboard())
                return
            context.user_data["add_date"] = iso_date
            context.user_data["awaiting_task_text"] = True
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
                context.user_data.pop("awaiting_task_text", None)
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
            context.user_data.pop("awaiting_task_text", None)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            context.user_data["active_day"] = day
            await update.message.reply_text(
                render_plan(day, day_obj, show_hint=True),
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
            context.user_data.pop("awaiting_task_text", None)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            context.user_data["active_day"] = day
            await update.message.reply_text(
                render_plan(day, day_obj, show_hint=True),
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
            context.user_data.pop("awaiting_task_text", None)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            context.user_data["active_day"] = day
            await update.message.reply_text(
                f"✅ Добавил задачу на {format_date_ru(day)}: {text}"
            )
            await update.message.reply_text(render_day_preview(day, day_obj, include_text=text))
            return
        if add_mode == "date":
            day = context.user_data.get("add_date")
            if not day:
                await update.message.reply_text(DATE_INPUT_ERROR, reply_markup=build_cancel_keyboard())
                return
            day_obj = add_task_to_day(state, day, text)
            save_user_state(user_id, state)
            context.user_data.pop("add_mode", None)
            context.user_data.pop("add_date", None)
            context.user_data.pop("awaiting_task_text", None)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            context.user_data["active_day"] = day
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
            context.user_data.pop("awaiting_task_text", None)
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
    if data.startswith("habit:"):
        data = "hab:" + data.split("habit:", 1)[1]

    if data.startswith("notif:"):
        user_id = query.from_user.id
        state = load_user_state(user_id)
        cfg = get_notifications(state)
        chat_id = query.message.chat_id if query.message else query.from_user.id
        if data == "notif:back":
            reset_input_modes(context)
            await query.answer()
            await query.message.reply_text(get_start_message(), reply_markup=build_start_keyboard())
            return
        if data == "notif:toggle":
            cfg["enabled"] = not bool(cfg.get("enabled"))
            state["notifications"] = cfg
            save_user_state(user_id, state)
            schedule_notifications(context, user_id, chat_id, state)
            await query.answer()
        if data == "notif:morning":
            cfg["morning"] = "09:00"
            state["notifications"] = cfg
            save_user_state(user_id, state)
            schedule_notifications(context, user_id, chat_id, state)
            await query.answer("Утреннее уведомление: 09:00")
        if data == "notif:evening":
            cfg["evening"] = "21:30"
            state["notifications"] = cfg
            save_user_state(user_id, state)
            schedule_notifications(context, user_id, chat_id, state)
            await query.answer("Вечернее уведомление: 21:30")

        text_msg = (
            f"🔔 Уведомления: {'включены' if cfg.get('enabled') else 'выключены'}\n"
            f"Утро: {cfg.get('morning')}\n"
            f"Вечер: {cfg.get('evening')}"
        )
        await query.message.edit_text(text_msg, reply_markup=build_notifications_keyboard(state))
        return

    if data.startswith("del_pick_day:"):
        day = data.split(":", 1)[1]
        user_id = query.from_user.id
        state = load_user_state(user_id)
        day_obj = get_day(state, day)
        if not day_obj.get("tasks"):
            await query.answer()
            await query.message.edit_text(
                "В этом дне нет задач.",
                reply_markup=build_delete_day_keyboard(state, context.user_data.get("active_day")),
            )
            return
        context.user_data["del_day"] = day
        context.user_data["awaiting_del_id"] = True
        await query.answer()
        await query.message.edit_text(
            f"Задачи на {format_date_ru(day)}:",
            reply_markup=build_delete_tasks_keyboard(day, day_obj),
        )
        return

    if data == "del_back":
        user_id = query.from_user.id
        state = load_user_state(user_id)
        await query.answer()
        await query.message.edit_text(
            "Выбери день, где удалить задачи",
            reply_markup=build_delete_day_keyboard(state, context.user_data.get("active_day")),
        )
        return

    if data.startswith("del_one:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Неверные данные.", show_alert=True)
            return
        day = parts[1]
        try:
            task_id = int(parts[2])
        except ValueError:
            await query.answer("Неверный номер задачи.", show_alert=True)
            return
        user_id = query.from_user.id
        state = load_user_state(user_id)
        day_obj = get_day(state, day)
        task = find_task(day_obj, task_id)
        if not task:
            await query.answer("Задача не найдена.", show_alert=True)
            return
        tasks = [t for t in day_obj.get("tasks", []) if t.get("id") != task_id]
        day_obj["tasks"] = normalize_task_ids(tasks)
        save_user_state(user_id, state)
        await query.answer("Удалил.")
        if not day_obj.get("tasks"):
            await query.message.edit_text(
                f"Задачи на {format_date_ru(day)} отсутствуют.",
                reply_markup=build_delete_day_keyboard(state, context.user_data.get("active_day")),
            )
            return
        await query.message.edit_text(
            f"Задачи на {format_date_ru(day)}:",
            reply_markup=build_delete_tasks_keyboard(day, day_obj),
        )
        return

    if data.startswith("triage:"):
        user_id = query.from_user.id
        state = load_user_state(user_id)
        backlog = get_backlog(state)
        if data == "triage:cancel":
            reset_input_modes(context)
            await query.answer()
            await query.message.reply_text("Ок, отменил.", reply_markup=build_start_keyboard())
            return
        if data == "triage:back":
            await query.answer()
            if not backlog:
                await query.message.reply_text("📦 Бэклог пуст.")
            else:
                await query.message.reply_text(
                    render_triage_list(backlog),
                    reply_markup=build_triage_keyboard(backlog),
                )
            return
        try:
            item_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Неверный номер задачи.", show_alert=True)
            return
        item = find_backlog_item(backlog, item_id)
        if not item:
            await query.answer("Задача не найдена в бэклоге.", show_alert=True)
            return
        await query.answer()
        await query.message.reply_text(
            f"Задача: {item.get('text')}\nКуда её поставить?",
            reply_markup=build_triage_to_keyboard(item_id, include_today=True),
        )
        return

    if data.startswith("triage_to:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Неверные данные.", show_alert=True)
            return
        target = parts[1]
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

        if target == "date":
            context.user_data["triage_date_mode"] = True
            context.user_data["triage_task_id"] = item_id
            await query.answer()
            await query.message.reply_text(DATE_INPUT_ERROR, reply_markup=build_cancel_keyboard())
            return

        if target == "delete":
            backlog.remove(item)
            backlog[:] = normalize_task_ids_backlog(backlog)
            state["backlog"] = backlog
            save_user_state(user_id, state)
            await query.answer("Удалил.")
            await query.message.reply_text(f"🗑 Удалил задачу: {item.get('text')}")
        else:
            day = today_str() if target == "today" else tomorrow_str()
            day_obj = get_day(state, day)
            if target == "today" and day_obj.get("closed"):
                await query.answer("Сегодня закрыт. Выбери другую дату.", show_alert=True)
                await query.message.reply_text(
                    "Сегодня закрыт. Выбери другую дату.",
                    reply_markup=build_triage_to_keyboard(item_id, include_today=False),
                )
                return
            add_task_to_day(state, day, item.get("text"))
            backlog.remove(item)
            backlog[:] = normalize_task_ids_backlog(backlog)
            state["backlog"] = backlog
            save_user_state(user_id, state)
            await query.answer()
            await query.message.reply_text(
                f"✅ Перенёс на {format_date_ru(day)}: {item.get('text')}"
            )

        if backlog:
            await query.message.reply_text(
                render_triage_list(backlog),
                reply_markup=build_triage_keyboard(backlog),
            )
        else:
            await query.message.reply_text("📦 Бэклог пуст.")
        return

    if data.startswith("hab:") or data.startswith("habits_day:"):
        user_id = query.from_user.id
        state = load_user_state(user_id)
        today = date.today()
        week_start_iso = context.user_data.get("habits_week_start")
        selected_iso = context.user_data.get("habits_selected_day") or context.user_data.get("habits_selected_date")
        try:
            week_start = date.fromisoformat(week_start_iso) if week_start_iso else None
        except Exception:
            week_start = None
        try:
            selected_date = date.fromisoformat(selected_iso) if selected_iso else today
        except Exception:
            selected_date = today
        if not week_start:
            week_start = week_start_for(selected_date)

        if data == "hab:back":
            await query.answer()
            await query.message.edit_text(
                render_habits_week(state, week_start, selected_date),
                parse_mode=ParseMode.HTML,
                reply_markup=build_habits_keyboard(state, selected_date),
            )
            return
        if data == "hab:settings":
            context.user_data.pop("awaiting_habit_title", None)
            await query.answer()
            await query.message.edit_text(
                render_habits_week(state, week_start, selected_date),
                parse_mode=ParseMode.HTML,
                reply_markup=build_habits_settings_keyboard(week_start, selected_date),
            )
            return
        if data == "hab:add":
            context.user_data["awaiting_habit_title"] = True
            await query.answer()
            await query.message.reply_text(
                "Напиши название привычки одним сообщением.",
                reply_markup=build_cancel_keyboard(),
            )
            return
        if data == "hab:del":
            config = get_habits_config(state)
            save_user_state(user_id, state)
            await query.answer()
            await query.message.edit_text(
                "Выбери привычку для удаления:",
                reply_markup=build_habits_delete_keyboard(config),
            )
            return
        if data.startswith("hab:del:"):
            key = data.split(":", 2)[2]
            config = get_habits_config(state)
            config = [h for h in config if str(h.get("key", "")) != key]
            state["habits_config"] = config
            save_user_state(user_id, state)
            await query.answer("Удалил привычку.")
            await query.message.edit_text(
                render_habits_week(state, week_start, selected_date),
                parse_mode=ParseMode.HTML,
                reply_markup=build_habits_keyboard(state, selected_date),
            )
            return
        if data == "hab:pick_day":
            await query.answer()
            await query.message.edit_text(
                render_habits_week(state, week_start, selected_date),
                parse_mode=ParseMode.HTML,
                reply_markup=build_habits_day_picker_keyboard(week_start),
            )
            return
        if data == "hab:week_prev":
            week_start = week_start - timedelta(days=7)
            context.user_data["habits_week_start"] = week_start.isoformat()
            selected_date = week_start + timedelta(days=selected_date.weekday())
            context.user_data["habits_selected_day"] = selected_date.isoformat()
            await query.answer()
            await query.message.edit_text(
                render_habits_week(state, week_start, selected_date),
                parse_mode=ParseMode.HTML,
                reply_markup=build_habits_settings_keyboard(week_start, selected_date),
            )
            return
        if data == "hab:week_next":
            week_start = week_start + timedelta(days=7)
            context.user_data["habits_week_start"] = week_start.isoformat()
            selected_date = week_start + timedelta(days=selected_date.weekday())
            context.user_data["habits_selected_day"] = selected_date.isoformat()
            await query.answer()
            await query.message.edit_text(
                render_habits_week(state, week_start, selected_date),
                parse_mode=ParseMode.HTML,
                reply_markup=build_habits_settings_keyboard(week_start, selected_date),
            )
            return
        if data.startswith("habits_day:"):
            code = data.split(":", 1)[1]
            if code == "cancel":
                await query.answer()
                await query.message.edit_text(
                    render_habits_week(state, week_start, selected_date),
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_habits_keyboard(state, selected_date),
                )
                return
            if code == "today":
                selected_date = today
            else:
                week_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
                idx = week_map.get(code)
                if idx is None:
                    selected_date = today
                else:
                    selected_date = week_start + timedelta(days=idx)
            context.user_data["habits_selected_day"] = selected_date.isoformat()
            context.user_data["habits_week_start"] = week_start_for(selected_date).isoformat()
            await query.answer()
            await query.message.edit_text(
                render_habits_week(state, week_start_for(selected_date), selected_date),
                parse_mode=ParseMode.HTML,
                reply_markup=build_habits_keyboard(state, selected_date),
            )
            return
        if data.startswith("hab:toggle:"):
            key = data.split(":", 2)[2]
            log = get_habits_log(state)
            selected_iso = selected_date.isoformat()
            log.setdefault(selected_iso, {})
            log[selected_iso][key] = not bool(log[selected_iso].get(key, False))
            state["habits_log"] = log
            save_user_state(user_id, state)
            await query.answer("Готово.")
            await query.message.edit_text(
                render_habits_week(state, week_start, selected_date),
                parse_mode=ParseMode.HTML,
                reply_markup=build_habits_keyboard(state, selected_date),
            )
            return

    if data.startswith("day:") or data.startswith("today:"):
        action = data.split(":", 1)[1]
        user_id = query.from_user.id
        state = load_user_state(user_id)
        day = today_str()
        day_obj = get_day(state, day)
        if action in {"reopen", "reopen_today"}:
            day_obj["closed"] = False
            day_obj.pop("closed_at", None)
            if not day_obj.get("tasks"):
                create_default_plan(day_obj)
            save_user_state(user_id, state)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            context.user_data["active_day"] = day
            await query.answer()
            await query.message.reply_text(
                render_plan(day, day_obj, show_hint=True),
                parse_mode=ParseMode.HTML,
                reply_markup=build_today_keyboard(day_obj),
            )
            return
        if action in {"tomorrow_preview", "show_tomorrow"}:
            tmr = tomorrow_str()
            tmr_obj = get_day(state, tmr)
            save_user_state(user_id, state)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = tmr
            context.user_data["active_day"] = tmr
            await query.answer()
            await query.message.reply_text(render_plan(tmr, tmr_obj), parse_mode=ParseMode.HTML)
            return

    if data.startswith("date:"):
        parts = data.split(":")
        if len(parts) == 2 and parts[1] == "input":
            reset_input_modes(context)
            context.user_data["view_date_mode"] = True
            await query.answer()
            await query.message.reply_text(DATE_INPUT_ERROR, reply_markup=build_cancel_keyboard())
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
            context.user_data["active_day"] = iso
            await query.answer()
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
        active_day = context.user_data.get("active_day")
        if active_day and active_day != day:
            await query.answer("Отмечать можно только в плане на сегодня.", show_alert=True)
            return
        if day_obj.get("closed"):
            await query.answer("Отмечать можно только в плане на сегодня.", show_alert=True)
            return

        context.user_data["view_scope"] = "day"
        context.user_data["view_day"] = day
        context.user_data["active_day"] = day

        ok, message = apply_done(day_obj, task_id)
        if not ok:
            await query.answer(message, show_alert=True)
            return

        save_user_state(user_id, state)
        await query.edit_message_text(
            render_plan(day, day_obj, show_hint=True),
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
                context.user_data["active_day"] = day
                context.user_data.pop("awaiting_task_text", None)
                await query.answer()
                await query.message.reply_text(
                    render_plan(day, day_obj, show_hint=True),
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_today_keyboard(day_obj),
                )
                return

            day = tomorrow_str()
            day_obj = add_task_to_day(state, day, pending_text)
            save_user_state(user_id, state)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            context.user_data["active_day"] = day
            context.user_data.pop("awaiting_task_text", None)
            await query.answer()
            await query.message.reply_text(
                f"✅ Добавил задачу на {format_date_ru(day)}: {pending_text}"
            )
            await query.message.reply_text(render_day_preview(day, day_obj, include_text=pending_text))
            return

        if mode == "reopen_today":
            context.user_data["add_mode"] = "reopen_today"
            context.user_data.pop("add_date", None)
            context.user_data["awaiting_task_text"] = True
            await query.answer()
            await query.message.reply_text(
                "Напиши текст задачи одним сообщением.",
                reply_markup=build_cancel_keyboard(),
            )
            return

        context.user_data["add_mode"] = mode
        context.user_data.pop("add_date", None)
        context.user_data.pop("awaiting_task_text", None)
        context.user_data.pop("del_mode", None)
        context.user_data.pop("awaiting_del_id", None)
        context.user_data.pop("move_mode", None)
        context.user_data.pop("move_task_id", None)
        await query.answer()
        if mode == "date":
            await query.message.reply_text(DATE_INPUT_ERROR, reply_markup=build_cancel_keyboard())
        else:
            context.user_data["awaiting_task_text"] = True
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
            context.user_data.pop("take_mode", None)
            user_id = query.from_user.id
            state = load_user_state(user_id)
            backlog = get_backlog(state)
            await query.answer()
            if not backlog:
                await query.message.reply_text("📦 Бэклог пуст.")
            else:
                message = "Нажми на задачу, чтобы перенести в план или удалить."
                message += "\n\n" + render_backlog_pick_list(backlog)
                await query.message.reply_text(
                    message,
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_backlog_pick_keyboard(backlog),
                )
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

        if context.user_data.get("take_mode"):
            day = today_str()
            day_obj = get_day(state, day)
            if day_obj.get("closed"):
                context.user_data.pop("take_mode", None)
                await query.answer()
                await query.message.reply_text(
                    "Сегодня уже закрыт. Куда добавить задачу?",
                    reply_markup=build_pick_to_keyboard(item_id),
                )
                return

            day_obj = add_task_to_day(state, day, item.get("text"))
            backlog.remove(item)
            backlog[:] = normalize_task_ids_backlog(backlog)
            state["backlog"] = backlog
            save_user_state(user_id, state)
            context.user_data.pop("take_mode", None)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            context.user_data["active_day"] = day
            await query.answer("✅ Добавил в план на сегодня")
            await query.message.reply_text(
                render_plan(day, day_obj, show_hint=True),
                parse_mode=ParseMode.HTML,
                reply_markup=build_today_keyboard(day_obj),
            )
            return

        context.user_data["move_task_id"] = item_id
        context.user_data.pop("move_mode", None)
        context.user_data.pop("add_mode", None)
        context.user_data.pop("add_date", None)
        context.user_data.pop("del_mode", None)
        context.user_data.pop("awaiting_del_id", None)
        context.user_data.pop("take_mode", None)
        await query.answer()
        await query.message.reply_text("Куда перенести задачу?", reply_markup=build_move_keyboard(item_id))
        return

    if data.startswith("pick_to:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Неверные данные.", show_alert=True)
            return

        try:
            item_id = int(parts[2])
        except ValueError:
            await query.answer("Неверный номер задачи.", show_alert=True)
            return

        target = parts[1]
        user_id = query.from_user.id
        state = load_user_state(user_id)
        backlog = get_backlog(state)
        item = find_backlog_item(backlog, item_id)
        if not item:
            await query.answer("Задача не найдена в бэклоге.", show_alert=True)
            return

        if target == "tomorrow":
            day = tomorrow_str()
            day_obj = add_task_to_day(state, day, item.get("text"))
            backlog.remove(item)
            backlog[:] = normalize_task_ids_backlog(backlog)
            state["backlog"] = backlog
            save_user_state(user_id, state)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            context.user_data["active_day"] = day
            await query.answer()
            await query.message.reply_text(
                f"✅ Добавил на {format_date_ru(day)}: {item.get('text')}"
            )
            await query.message.reply_text(render_day_preview(day, day_obj, include_text=item.get("text")))
            return

        if target == "reopen_today":
            day = today_str()
            day_obj = get_day(state, day)
            day_obj["closed"] = False
            day_obj.pop("closed_at", None)
            if not day_obj.get("tasks"):
                create_default_plan(day_obj)
            day_obj = add_task_to_day(state, day, item.get("text"))
            backlog.remove(item)
            backlog[:] = normalize_task_ids_backlog(backlog)
            state["backlog"] = backlog
            save_user_state(user_id, state)
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            context.user_data["active_day"] = day
            await query.answer("✅ Добавил в план на сегодня")
            await query.message.reply_text(
                render_plan(day, day_obj, show_hint=True),
                parse_mode=ParseMode.HTML,
                reply_markup=build_today_keyboard(day_obj),
            )
            return

    if data.startswith("move:"):
        parts = data.split(":")
        if len(parts) == 2 and parts[1] == "cancel":
            reset_input_modes(context)
            await query.answer()
            await query.message.reply_text("Ок, отменил.", reply_markup=build_start_keyboard())
            return
        if len(parts) == 2 and parts[1] == "back":
            user_id = query.from_user.id
            state = load_user_state(user_id)
            backlog = get_backlog(state)
            await query.answer()
            if not backlog:
                await query.message.reply_text("📦 Бэклог пуст.")
            else:
                message = "Нажми на задачу, чтобы перенести в план или удалить."
                message += "\n\n" + render_backlog_pick_list(backlog)
                await query.message.reply_text(
                    message,
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_backlog_pick_keyboard(backlog),
                )
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
        user_id = query.from_user.id
        state = load_user_state(user_id)

        if target == "delete":
            backlog = get_backlog(state)
            item = find_backlog_item(backlog, item_id)
            if not item:
                await query.answer("Задача не найдена в бэклоге.", show_alert=True)
                return
            backlog.remove(item)
            backlog[:] = normalize_task_ids_backlog(backlog)
            state["backlog"] = backlog
            save_user_state(user_id, state)
            await query.answer("Удалил.")
            if backlog:
                message = "Нажми на задачу, чтобы перенести в план или удалить."
                message += "\n\n" + render_backlog_pick_list(backlog)
                await query.message.reply_text(
                    message,
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_backlog_pick_keyboard(backlog),
                )
            else:
                await query.message.reply_text("📦 Бэклог пуст.")
            return

        if target == "date":
            context.user_data["move_mode"] = "date"
            context.user_data["move_task_id"] = item_id
            context.user_data.pop("add_mode", None)
            context.user_data.pop("add_date", None)
            context.user_data.pop("del_mode", None)
            context.user_data.pop("awaiting_del_id", None)
            await query.answer()
            await query.message.reply_text(DATE_INPUT_ERROR, reply_markup=build_cancel_keyboard())
            return

        if target not in {"today", "tomorrow"}:
            await query.answer("Неверное направление.", show_alert=True)
            return

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
        context.user_data["active_day"] = day
        await query.answer()
        if target == "today":
            await query.message.reply_text(
                render_plan(day, day_obj, show_hint=True),
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
            context.user_data["view_scope"] = "day"
            context.user_data["view_day"] = day
            context.user_data["active_day"] = day
            await query.message.reply_text(
                render_plan(day, day_obj, show_hint=True),
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
