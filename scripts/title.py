#!/usr/bin/env python3
"""chat-title-manager 本机执行层。

给「新聊天自动命名」提供三件它自己做不到的保护：
  1. 节流       同一会话两次改名之间至少间隔 --interval 秒（默认 120）
  2. 同题跳过   归一化后与现标题相同则不写库，保持幂等
  3. 手动改名保护  你亲手改过的标题自动钉住，不再被覆盖

仅适用于会话元数据落在 sqlite 的应用（当前即 WorkBuddy）。
其他平台仍按 SKILL.md 走原生改名工具，本脚本不适用。

用法：
  title.py current
  title.py status  [--session ID] [--interval 120]
  title.py set     --title "1113｜开发-某功能" [--session ID] [--force] [--allow-pinned]
  title.py pin     [--session ID] [--note "说明"]
  title.py unpin   [--session ID]
  title.py list

输出为单行 JSON，退出码：0 成功/幂等跳过，2 参数或会话定位失败，3 受保护，4 节流，1 其他错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".chat-title-manager"
HISTORY_DIR = STATE_DIR / "history"
STATE_FILE = STATE_DIR / "state.json"
PIN_FILE = STATE_DIR / "pinned.json"
DB_PATH = HOME / ".workbuddy" / "workbuddy.db"

DEFAULT_INTERVAL = 120
AMBIGUOUS_GAP_MS = 60_000


def now_ms() -> int:
    return int(time.time() * 1000)


def norm(text: str | None) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFC", str(text))
    s = "".join(s.split())
    return s.casefold()


def load_json(path: Path, default: dict) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_state() -> dict:
    st = load_json(STATE_FILE, {})
    st.setdefault("version", 1)
    st.setdefault("sessions", {})
    return st


def load_pins() -> dict:
    p = load_json(PIN_FILE, {})
    p.setdefault("version", 1)
    p.setdefault("sessions", {})
    return p


def connect(ro: bool) -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise SystemExit(emit({"status": "error", "error": "db-not-found", "path": str(DB_PATH)}, 1))
    if ro:
        uri = "file:" + DB_PATH.as_posix() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=10)
    return sqlite3.connect(str(DB_PATH), timeout=10)


ROW_COLS = "id, cwd, title, custom_title, created_at, last_activity_at, updated_at"


def row_to_dict(row) -> dict:
    keys = ROW_COLS.split(", ")
    d = dict(zip(keys, row))
    last = d.get("last_activity_at") or d.get("updated_at") or 0
    d["last_activity_at"] = last
    return d


def fetch_by_id(conn: sqlite3.Connection, sid: str) -> dict | None:
    cur = conn.execute(
        f"select {ROW_COLS} from sessions where id = ? and (deleted_at is null or deleted_at = 0)", (sid,)
    )
    row = cur.fetchone()
    if row:
        return row_to_dict(row)
    cur = conn.execute(
        f"select {ROW_COLS} from sessions where id like ? and (deleted_at is null or deleted_at = 0)"
        " order by coalesce(last_activity_at, updated_at) desc",
        (sid + "%",),
    )
    rows = cur.fetchall()
    if len(rows) == 1:
        return row_to_dict(rows[0])
    if len(rows) > 1:
        return {"_ambiguous_prefix": sid, "_candidates": [r[0] for r in rows[:5]]}
    return None


def fetch_current(conn: sqlite3.Connection, cwd: str) -> dict:
    cand = cwd.replace("\\", "/")
    cur = conn.execute(
        f"select {ROW_COLS} from sessions where cwd = ? and (deleted_at is null or deleted_at = 0)"
        " order by coalesce(last_activity_at, updated_at) desc limit 2",
        (cand,),
    )
    rows = cur.fetchall()
    if not rows:
        return {"_error": "no-session-in-cwd", "cwd": cand}
    first = row_to_dict(rows[0])
    if len(rows) > 1:
        second = row_to_dict(rows[1])
        gap = (first.get("last_activity_at") or 0) - (second.get("last_activity_at") or 0)
        if gap < AMBIGUOUS_GAP_MS:
            return {
                "_error": "ambiguous-cwd",
                "cwd": cand,
                "candidates": [first["id"], second["id"]],
                "hint": "同一目录有并行会话，请显式传 --session",
            }
    return first


def resolve(conn: sqlite3.Connection, sid: str | None, cwd: str | None) -> dict:
    if sid:
        found = fetch_by_id(conn, sid)
        if not found:
            return {"_error": "session-not-found", "session": sid}
        return found
    return fetch_current(conn, cwd or os.getcwd())


def emit(payload: dict, code: int = 0) -> int:
    payload.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    print(json.dumps(payload, ensure_ascii=False))
    return code


def snapshot(op: str, sess: dict, old: str | None, new: str | None, extra: dict | None = None) -> str:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{op}.json"
    payload = {
        "op": op,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "db": str(DB_PATH),
        "rows": [
            {
                "id": sess.get("id"),
                "cwd": sess.get("cwd"),
                "title": sess.get("title"),
                "old_custom_title": old,
                "new_custom_title": new,
                "created_at": sess.get("created_at"),
            }
        ],
    }
    if extra:
        payload.update(extra)
    save_json(path, payload)
    return str(path)


def pin_session(sid: str, reason: str, note: str | None, title: str | None) -> dict:
    pins = load_pins()
    entry = {"pinned_at": now_ms(), "reason": reason, "title": title}
    if note:
        entry["note"] = note
    pins["sessions"][sid] = entry
    save_json(PIN_FILE, pins)
    return entry


def guard(conn, sess: dict, new_title: str, args) -> tuple[str, dict]:
    """返回 (判定结果, 附加信息)。结果取值：ok / same-title / manual-edit / pinned / throttled"""
    sid = sess["id"]
    current = sess.get("custom_title")
    state = load_state()
    pins = load_pins()
    meta = state["sessions"].get(sid, {})

    pinned = pins["sessions"].get(sid)
    if pinned and not args.allow_pinned:
        return "pinned", {"pinned": pinned}

    managed_title = meta.get("last_title")
    if current and managed_title is not None and norm(current) != norm(managed_title) and not args.allow_pinned:
        entry = pin_session(sid, "manual-edit", "检测到标题被手工修改，自动钉住", current)
        return "manual-edit", {"pinned": entry, "detected_title": current, "managed_title": managed_title}

    if norm(current) == norm(new_title):
        return "same-title", {"current": current}

    if not args.force:
        last = int(meta.get("last_set_at") or 0)
        interval = int(args.interval or DEFAULT_INTERVAL)
        if last:
            elapsed = (now_ms() - last) / 1000
            if elapsed < interval:
                return "throttled", {
                    "elapsed_sec": round(elapsed, 1),
                    "interval_sec": interval,
                    "next_allowed_in_sec": round(interval - elapsed, 1),
                }
    return "ok", {}


def cmd_current(args) -> int:
    conn = connect(ro=True)
    sess = resolve(conn, getattr(args, "session", None), getattr(args, "cwd", None))
    if "_error" in sess or "_ambiguous_prefix" in sess:
        return emit({"status": "error", **sess}, 2)
    return emit({"status": "ok", "session": sess})


def cmd_status(args) -> int:
    conn = connect(ro=True)
    sess = resolve(conn, args.session, args.cwd)
    if "_error" in sess or "_ambiguous_prefix" in sess:
        return emit({"status": "error", **sess}, 2)
    sid = sess["id"]
    state = load_state()
    pins = load_pins()
    meta = state["sessions"].get(sid, {})
    pinned = pins["sessions"].get(sid)
    manual_drift = bool(
        sess.get("custom_title")
        and meta.get("last_title") is not None
        and norm(sess["custom_title"]) != norm(meta["last_title"])
    )
    interval = int(args.interval or DEFAULT_INTERVAL)
    last = int(meta.get("last_set_at") or 0)
    remain = max(0.0, interval - (now_ms() - last) / 1000) if last else 0.0
    return emit(
        {
            "status": "ok",
            "session": sess,
            "pinned": pinned,
            "manual_edit_detected": manual_drift,
            "managed_title": meta.get("last_title"),
            "last_set_at": meta.get("last_set_at"),
            "throttle_remaining_sec": round(remain, 1),
            "interval_sec": interval,
        }
    )


def cmd_set(args) -> int:
    new_title = (args.title or "").strip()
    if not new_title:
        return emit({"status": "error", "error": "empty-title"}, 2)
    if "|" in new_title.replace("｜", ""):
        return emit({"status": "error", "error": "use-fullwidth-bar", "hint": "分隔符须为全角 ｜"}, 2)

    conn = connect(ro=False)
    sess = resolve(conn, args.session, args.cwd)
    if "_error" in sess or "_ambiguous_prefix" in sess:
        return emit({"status": "error", **sess}, 2)

    verdict, extra = guard(conn, sess, new_title, args)
    if verdict != "ok":
        code = 0 if verdict == "same-title" else (4 if verdict == "throttled" else 3)
        return emit({"status": "skipped:" + verdict, "session_id": sess["id"], "current": sess.get("custom_title"), **extra}, code)

    old = sess.get("custom_title")
    snap = snapshot("single", sess, old, new_title)
    conn.execute("update sessions set custom_title = ? where id = ?", (new_title, sess["id"]))
    conn.commit()
    conn.close()

    state = load_state()
    state["sessions"][sess["id"]] = {
        "last_title": new_title,
        "last_set_at": now_ms(),
        "cwd": sess.get("cwd"),
    }
    save_json(STATE_FILE, state)

    verify = connect(ro=True)
    row = verify.execute("select custom_title from sessions where id = ?", (sess["id"],)).fetchone()
    got = row[0] if row else None
    ok = norm(got) == norm(new_title)
    return emit(
        {
            "status": "renamed" if ok else "verify-failed",
            "session_id": sess["id"],
            "old": old,
            "new": got,
            "snapshot": snap,
        },
        0 if ok else 1,
    )


def cmd_pin(args) -> int:
    conn = connect(ro=True)
    sess = resolve(conn, args.session, args.cwd)
    if "_error" in sess or "_ambiguous_prefix" in sess:
        return emit({"status": "error", **sess}, 2)
    entry = pin_session(sess["id"], "user-pin", args.note, sess.get("custom_title"))
    return emit({"status": "pinned", "session_id": sess["id"], "entry": entry})


def cmd_unpin(args) -> int:
    conn = connect(ro=True)
    sess = resolve(conn, args.session, args.cwd)
    if "_error" in sess or "_ambiguous_prefix" in sess:
        return emit({"status": "error", **sess}, 2)
    pins = load_pins()
    removed = pins["sessions"].pop(sess["id"], None)
    save_json(PIN_FILE, pins)
    return emit({"status": "unpinned" if removed else "not-pinned", "session_id": sess["id"], "removed": removed})


def cmd_list(args) -> int:
    pins = load_pins()
    state = load_state()
    managed = sorted(
        ({"id": k, **v} for k, v in state["sessions"].items()),
        key=lambda x: x.get("last_set_at") or 0,
        reverse=True,
    )[:20]
    return emit({"status": "ok", "pinned": pins["sessions"], "recent_managed": managed})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="title.py", description="聊天标题执行层：节流 / 同题跳过 / 手动改名保护")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, with_session=True):
        if with_session:
            sp.add_argument("--session", help="会话 ID，可传前缀；省略则按当前目录推断")
        sp.add_argument("--cwd", help="用于推断会话的工作目录，默认取进程 cwd")

    sp = sub.add_parser("current", help="推断并打印当前会话")
    common(sp)
    sp.set_defaults(func=cmd_current)

    sp = sub.add_parser("status", help="查看现标题、钉住状态、节流余量")
    common(sp)
    sp.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("set", help="写标题（带三道保护）")
    common(sp)
    sp.add_argument("--title", required=True)
    sp.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    sp.add_argument("--force", action="store_true", help="忽略节流")
    sp.add_argument("--allow-pinned", action="store_true", help="越过钉住与手改保护（慎用）")
    sp.set_defaults(func=cmd_set)

    sp = sub.add_parser("pin", help="钉住标题，禁止自动覆盖")
    common(sp)
    sp.add_argument("--note")
    sp.set_defaults(func=cmd_pin)

    sp = sub.add_parser("unpin", help="解除钉住，重新纳入自动命名")
    common(sp)
    sp.set_defaults(func=cmd_unpin)

    sp = sub.add_parser("list", help="列出钉住项与最近写入记录")
    sp.set_defaults(func=cmd_list)
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except sqlite3.Error as exc:
        return emit({"status": "error", "error": "sqlite", "detail": str(exc)}, 1)


if __name__ == "__main__":
    sys.exit(main())
