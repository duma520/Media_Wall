# -*- coding: utf-8 -*-
"""fileops + history 单元自测：全程在临时目录跑，不碰真实媒体根与 data/。

跑法（项目根）：python _scaffold\\_test_fileops.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROG))

from mediawall import fileops, history  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def expect_error(name: str, fn, needle: str = "") -> None:
    try:
        fn()
    except fileops.FileOpError as exc:
        ok = (needle in str(exc)) if needle else True
        check(name, ok, f"错误信息不含 {needle!r}：{exc}")
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"抛了非 FileOpError：{type(exc).__name__}: {exc}")
    else:
        check(name, False, "本应报错但成功了")


def build_tree(root: Path) -> None:
    d = root / "01 电影" / "2020" / "片名A"
    d.mkdir(parents=True)
    (d / "片名A.mp4").write_bytes(b"x" * 1024)
    (d / "cover.jpg").write_bytes(b"y" * 128)
    (root / "02 目标").mkdir(parents=True)
    (root / "02 目标" / "片名A.mp4").write_bytes(b"z")


def test_fileops(tmp: Path) -> None:
    print("\n[1] 回收站：删除 → 撤回 → 恢复")
    root = tmp / "media1"
    build_tree(root)
    fileops._undo_override = tmp / "undo1.json"
    fileops._undo_override.write_text("[]", encoding="utf-8")

    entry = fileops.move_to_trash(root, "01 电影/2020/片名A")
    check("删除后源目录消失", not (root / "01 电影/2020/片名A").exists())
    check("回收站有 1 项", len(fileops.list_trash(root)) == 1)
    check("撤回记录 op=trash", entry.get("op") == "trash")
    check("回收站目录是隐藏名", (root / fileops.TRASH_DIRNAME).is_dir())

    msg = fileops.undo_last(root)
    check("撤回后目录回来", (root / "01 电影/2020/片名A" / "片名A.mp4").is_file(), msg)
    check("撤回后回收站空了", len(fileops.list_trash(root)) == 0)

    entry = fileops.move_to_trash(root, "01 电影/2020/片名A")
    restored = fileops.restore(root, entry["trash_id"])
    check("手动 restore 还原到原位", restored == "01 电影/2020/片名A"
          and (root / "01 电影/2020/片名A/cover.jpg").is_file())

    print("\n[2] 移动 + 撤回")
    fileops.move_to_trash(root, "01 电影/2020/片名A")
    fileops.restore(root, fileops.list_trash(root)[0]["id"])
    fileops._undo_override.write_text("[]", encoding="utf-8")

    fileops.move_item(root, "01 电影/2020/片名A/cover.jpg", "02 目标")
    check("文件已移动到目标", (root / "02 目标/cover.jpg").is_file()
          and not (root / "01 电影/2020/片名A/cover.jpg").exists())
    msg = fileops.undo_last(root)
    check("撤回移动后回到原处", (root / "01 电影/2020/片名A/cover.jpg").is_file(), msg)

    print("\n[3] 安全边界")
    expect_error("分区根不可删除", lambda: fileops.move_to_trash(root, "01 电影"), "受保护")
    expect_error("媒体根本身不可删除", lambda: fileops.move_to_trash(root, "."), "受保护")
    expect_error("空路径拒绝", lambda: fileops.move_to_trash(root, ""), "路径为空")
    expect_error("越界路径拒绝", lambda: fileops.resolve(root, "../外面.mp4"), "越界")
    expect_error("不存在的目标报错", lambda: fileops.move_to_trash(root, "01 电影/没有这个"), "不存在")

    d2 = root / "03 另一个" / "子"
    d2.mkdir(parents=True)
    (d2 / "a.mp4").write_bytes(b"a")
    (root / "02 目标" / "a.mp4").write_bytes(b"aa")
    expect_error("目标同名冲突时报错", lambda: fileops.move_item(root, "03 另一个/子/a.mp4", "02 目标"),
                 "同名")

    print("\n[4] 清空回收站")
    fileops.move_to_trash(root, "01 电影/2020/片名A")
    before = len(fileops.list_trash(root))
    result = fileops.empty_trash(root)
    check("清空返回项数", result["count"] == before and before == 1, str(result))
    check("清空后回收站为空", len(fileops.list_trash(root)) == 0)
    check("清空后原件不再存在", not (root / "01 电影/2020/片名A").exists())


def test_history(tmp: Path) -> None:
    print("\n[5] 历史记录")
    hp = tmp / "history.jsonl"
    history._path_override = hp
    history.clear()
    history._limits = lambda: (500, True)  # 固定上限，避免受真实 config 影响

    check("记录播放事件", history.record("192.168.1.10", "play", "01 电影/a.mp4", "01 电影") is True)
    check("同 IP 同文件 30s 内去重",
          history.record("192.168.1.10", "play", "01 电影/a.mp4", "01 电影") is False)
    check("同 IP 不同文件会记录",
          history.record("192.168.1.10", "play", "01 电影/b.mp4", "01 电影") is True)
    check("不同 IP 同文件会记录",
          history.record("192.168.1.11", "play", "01 电影/a.mp4", "01 电影") is True)
    check("页面访问可记录", history.record("192.168.1.12", "page") is True)
    check("空 IP 拒绝", history.record("", "play", "x.mp4") is False)

    events = history.list_events(limit=10)
    check("事件条数正确", len(events) == 4, f"实际 {len(events)}")
    check("最新在前", events[0].get("kind") == "page", str(events[0]))
    check("字段完整", all({"ts", "ip", "kind", "rel", "category"} <= set(e) for e in events))
    check("条数统计", history.event_count() == 4, str(history.event_count()))

    print("\n[6] 保留条数裁剪")
    history._limits = lambda: (3, True)
    history.clear()
    for i in range(12):
        history.record("10.0.0.1", "play", f"movie-{i}.mp4", "01 电影")
    raw = hp.read_text(encoding="utf-8").strip().splitlines()
    check("裁剪后文件行数受控", len(raw) <= 6, f"实际 {len(raw)} 行")
    kept = history.list_events(limit=3)
    check("保留的是最新 3 条", [e["rel"] for e in kept] == ["movie-11.mp4", "movie-10.mp4", "movie-9.mp4"],
          str([e["rel"] for e in kept]))

    print("\n[7] 关闭页面记录后不再写 page")
    history._limits = lambda: (500, False)
    history.clear()
    check("page 事件被丢弃", history.record("10.0.0.2", "page") is False)
    check("play 事件仍记录", history.record("10.0.0.2", "play", "c.mp4") is True)
    check("清空历史", history.clear() is True and history.list_events() == [])


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mw_fileops_"))
    try:
        test_fileops(tmp)
        test_history(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n结果: PASS {PASS} / FAIL {FAIL}")
    print("FILEOPS_OK" if FAIL == 0 else "FILEOPS_FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
