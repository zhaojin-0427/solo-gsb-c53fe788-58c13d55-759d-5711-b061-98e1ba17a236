"""逐条文案修订与恢复导入原文的端到端回归测试。

覆盖：
- 保存归一化：去标签、折叠空白、按 Unicode 扩展字素簇计数，得到非空当前
  文案；空白稿（归一化为空）400 拒绝且不落库；
- 生效文案贯穿：详情 CPS 告警、timeline_hash、JSON/SRT/ZIP 导出文案同步
  更新；JSON 原文哈希仍指向导入内容；cue_id、原时间、采纳与镜头留白结论
  保持不变；
- 恢复导入原文：清除修订稿，生效文案/字素数/指纹回落；
- 旧页面识别：base_text 过期（另一标签页先改过）→ 409 拒绝覆盖，最新
  文案可由详情读到；不存在的 cue / 项目 → 400 / 404；
- 并发交错：两个修订同时到达，写锁串行后先到者生效、迟到者 409；
- 交付核验按生效文案配对：修订后回传新文案通过、回传旧文案不再全通过；
- 旧库迁移：cues 表补 edited_text / edited_graphemes 列。
"""
import asyncio
import hashlib
import io
import json
import os
import sqlite3
import sys
import threading
import zipfile

DB = "/tmp/test_text_revision.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_PATH"] = DB  # 须在导入 app 之前设置

sys.path.insert(0, "/workspace")

from fastapi import HTTPException, UploadFile  # noqa: E402

from app import database as db  # noqa: E402
from app import main as m  # noqa: E402

db.init_db()

SRT = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
       "2\n00:00:01,040 --> 00:00:02,040\n乙\n")
SRT_SHOTS = ("1\n00:00:00,100 --> 00:00:00,500\n镜头内字幕\n\n"
             "2\n00:00:00,900 --> 00:00:01,300\n跨镜头字幕\n")
SHOTS = json.dumps([{"start_ms": 0, "end_ms": 1200},
                    {"start_ms": 1200, "end_ms": 2400}])
RAW_HASH_JIA = hashlib.sha256("甲".encode("utf-8")).hexdigest()


def _upload(name: str, content: str) -> UploadFile:
    return UploadFile(io.BytesIO(content.encode("utf-8")), filename=name)


def _create(srt=SRT, shots=None, cps=21.0):
    return asyncio.run(m.create_project(
        name="文案修订场景", cps_limit=cps, min_gap_ms=40, tolerance_ms=200,
        subtitle=_upload("a.srt", srt),
        shots_file=_upload("s.json", shots) if shots else None,
    ))


def _revise(pid, uid, text, base_text):
    return m.revise_cue_text(pid, uid, m.CueTextIn(text=text,
                                                   base_text=base_text))


def _reset(pid, uid, base_text):
    return m.reset_cue_text(pid, uid, m.CueTextResetIn(base_text=base_text))


def _cue(proj, i=0):
    return proj["analysis"]["cues"][i]


def _expect(status, fn, *args, **kw):
    try:
        fn(*args, **kw)
    except HTTPException as exc:
        assert exc.status_code == status, \
            f"期望 {status}，实得 {exc.status_code}: {exc.detail}"
        return exc
    raise AssertionError(f"应抛出 {status}")


# ---------- 保存归一化与生效文案 ----------

def test_revise_normalizes_and_updates_cps():
    proj = _create()
    pid = proj["id"]
    c0 = _cue(proj, 0)
    assert c0["text"] == "甲" and c0["graphemes"] == 1
    assert c0["text_edited"] is False and c0["imported_text"] == "甲"
    assert c0["cps"] == 1.0 and c0["cps_ok"] is True

    # 带标签 + 多余空白 + emoji 组合序列：保存时去标签、折叠空白、
    # 按扩展字素簇计数（👨‍👩‍👧 为 1 个字素）
    proj = _revise(pid, c0["uid"], "<i>你好</i>   世界\n 👨‍👩‍👧 ", "甲")
    c0 = _cue(proj, 0)
    assert c0["text"] == "你好 世界 👨‍👩‍👧", c0["text"]
    assert c0["graphemes"] == 7, c0["graphemes"]
    assert c0["cps"] == 7.0 and c0["cps_ok"] is True
    assert c0["text_edited"] is True
    assert c0["imported_text"] == "甲" and c0["imported_graphemes"] == 1
    assert c0["raw_hash"] == RAW_HASH_JIA  # 原文哈希仍指向导入内容
    # 未修订的 cue 不受影响
    c1 = _cue(proj, 1)
    assert c1["text"] == "乙" and c1["text_edited"] is False
    # 库行同样区分导入原文与修订稿
    row = next(r for r in proj["cues"] if r["uid"] == c0["uid"])
    assert row["text"] == "甲" and row["edited_text"] == "你好 世界 👨‍👩‍👧"
    assert row["text_edited"] is True
    assert row["effective_text"] == "你好 世界 👨‍👩‍👧"
    row1 = next(r for r in proj["cues"] if r["uid"] == c1["uid"])
    assert row1["edited_text"] is None and row1["effective_text"] == "乙"
    print("revise normalizes (tag/whitespace/grapheme) + CPS recompute ok")


def test_blank_and_missing_rejected_without_partial_write():
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]
    before = m.get_project(pid)
    # 空白稿：纯空白、空串、去标签后为空，一律 400 且不写入
    for blank in ("", "   \n  ", "<i></i>", "<b> </b>"):
        _expect(400, _revise, pid, uid, blank, "甲")
    # 不存在的 cue / 项目
    _expect(400, _revise, pid, 999999, "新文案", "甲")
    _expect(404, _revise, 999999, uid, "新文案", "甲")
    _expect(400, _reset, pid, 999999, "甲")
    _expect(404, _reset, 999999, uid, "甲")
    after = m.get_project(pid)
    assert after == before, "失败请求不得留下任何部分写入"
    print("blank / missing cue rejected, no partial write ok")


def test_cps_alert_follows_revision_and_reset():
    proj = _create(cps=3.0)   # CPS 上限 3：原「甲」1 字素/秒 通过
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]
    assert proj["analysis"]["stats"]["cps_violations"] == 0
    # 修订为 5 字素 → CPS 5 超限，告警与统计同步出现
    proj = _revise(pid, uid, "五个字素呀", "甲")
    c0 = _cue(proj, 0)
    assert c0["graphemes"] == 5 and c0["cps"] == 5.0
    assert c0["cps_ok"] is False
    assert proj["analysis"]["stats"]["cps_violations"] == 1
    # 恢复导入原文 → CPS 告警同步消失
    proj = _reset(pid, uid, "五个字素呀")
    c0 = _cue(proj, 0)
    assert c0["text"] == "甲" and c0["graphemes"] == 1
    assert c0["cps_ok"] is True
    assert proj["analysis"]["stats"]["cps_violations"] == 0
    print("CPS alert follows revision and reset ok")


def test_timeline_hash_and_exports_follow_effective_text():
    proj = _create()
    pid = proj["id"]
    h0 = proj["timeline_hash"]
    uid = _cue(proj, 0)["uid"]

    def exports():
        doc = json.loads(m.export(pid, fmt="json").body.decode("utf-8"))
        srt = m.export(pid, fmt="srt").body.decode("utf-8")
        with zipfile.ZipFile(io.BytesIO(m.export(pid, fmt="zip").body)) as zf:
            zdoc = json.loads(zf.read("correction.json").decode("utf-8"))
            zsrt = zf.read("corrected.srt").decode("utf-8")
        return doc, srt, zdoc, zsrt

    proj = _revise(pid, uid, "新文案", "甲")
    h1 = proj["timeline_hash"]
    assert h1 != h0, "文案修订改变生效轴，指纹必须变化"
    doc, srt, zdoc, zsrt = exports()
    c0 = doc["cues"][0]
    assert c0["text"] == "新文案" and c0["edited"] is True
    assert c0["imported_text"] == "甲"
    assert c0["raw_text_hash"] == RAW_HASH_JIA, "JSON 原文哈希仍指向导入内容"
    assert doc["cues"][1]["edited"] is False
    assert "新文案" in srt and "\n甲\n" not in srt
    assert zdoc == doc and zsrt == srt, "ZIP 与独立导出必须一致"

    # 恢复导入原文：指纹回落，导出回到导入文案
    proj = _reset(pid, uid, "新文案")
    assert proj["timeline_hash"] == h0, "恢复原文后生效轴复原，指纹应回落"
    doc, srt, _, _ = exports()
    c0 = doc["cues"][0]
    assert c0["text"] == "甲" and c0["edited"] is False
    assert c0["imported_text"] == "甲"
    assert "\n甲\n" in srt and "新文案" not in srt
    print("timeline_hash + JSON/SRT/ZIP follow effective text ok")


def test_revision_keeps_timing_adoption_and_gap_reviews():
    proj = _create(srt=SRT_SHOTS, shots=SHOTS)
    pid = proj["id"]
    cues = proj["analysis"]["cues"]
    assert [(c["proposal_start_ms"], c["proposal_end_ms"]) for c in cues] == [
        (100, 500), (800, 1200)]
    # 预置采纳与留白结论
    proj = m.adopt(pid, m.AdoptIn(cue_uid=cues[1]["uid"],
                                  new_start_ms=800, new_end_ms=1200))
    gap = proj["gap_review"]["items"][0]
    proj = m.review_gap(pid, m.GapReviewIn(
        shot_ord=gap["shot_ord"], gap_start_ms=gap["gap_start_ms"],
        gap_end_ms=gap["gap_end_ms"], decision="intentional"))
    decisions_before = [(it["shot_ord"], it["gap_start_ms"], it["gap_end_ms"],
                         it["decision"])
                        for it in proj["gap_review"]["items"]]
    assert any(d == "intentional" for *_, d in decisions_before)

    # 修订第一条文案：时间、提案、采纳、留白结论全部保持不变
    proj = _revise(pid, cues[0]["uid"], "修订后的镜头内字幕", "镜头内字幕")
    c0, c1 = proj["analysis"]["cues"]
    assert c0["cue_id"] == cues[0]["cue_id"]
    assert (c0["start_ms"], c0["end_ms"]) == (100, 500)
    assert (c0["proposal_start_ms"], c0["proposal_end_ms"]) == (100, 500)
    assert c0["text"] == "修订后的镜头内字幕"
    assert c1["adopted"] and c1["adopted_start_ms"] == 800
    decisions_after = [(it["shot_ord"], it["gap_start_ms"], it["gap_end_ms"],
                        it["decision"])
                       for it in proj["gap_review"]["items"]]
    assert decisions_after == decisions_before, "留白结论不受文案修订影响"
    doc = json.loads(m.export(pid, fmt="json").body.decode("utf-8"))
    assert doc["cues"][1]["corrected"] == {"start_ms": 800, "end_ms": 1200}
    assert doc["cues"][0]["original"] == {"start_ms": 100, "end_ms": 500}
    print("revision keeps cue_id/timing/adoption/gap reviews ok")


# ---------- 旧页面识别与并发 ----------

def test_stale_base_text_rejected_409():
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]
    # base_text 与当前生效文案不符（另一标签页先改过的情形）→ 409，不写入
    _expect(409, _revise, pid, uid, "别人的修改", "过期文案")
    assert _cue(m.get_project(pid), 0)["text"] == "甲"

    # 甲 → 乙 后，仍按「甲」提交的旧页面一律 409；最新文案由详情可见
    _revise(pid, uid, "乙改", "甲")
    _expect(409, _revise, pid, uid, "覆盖", "甲")
    _expect(409, _reset, pid, uid, "甲")
    latest = _cue(m.get_project(pid), 0)
    assert latest["text"] == "乙改" and latest["text_edited"] is True
    # 按最新文案作 base 则可正常操作
    proj = _reset(pid, uid, "乙改")
    assert _cue(proj, 0)["text"] == "甲"
    # 未修订时恢复原文是幂等空操作
    proj = _reset(pid, uid, "甲")
    assert _cue(proj, 0)["text_edited"] is False
    print("stale base_text -> 409, latest text visible, no overwrite ok")


class _Gate:
    """被调函数执行后置 entered、放行前阻塞（持锁不提交）。"""

    def __init__(self, orig):
        self.orig = orig
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args, **kwargs):
        out = self.orig(*args, **kwargs)
        self.entered.set()
        assert self.release.wait(timeout=10), "门控未放行"
        return out


def _run(fn):
    box = {}

    def worker():
        try:
            box["result"] = fn()
        except BaseException as exc:  # 含 HTTPException
            box["error"] = exc

    th = threading.Thread(target=worker)
    th.start()
    return th, box


def test_concurrent_revisions_second_rejected():
    """两个标签页同时修订同一 cue：先提交者生效，迟到者写锁内发现
    base_text 已过期 → 409，不会覆盖先到者的修订。"""
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]
    gate = _Gate(m.db.update_cue_text)
    m.db.update_cue_text = gate
    try:
        # A：按 base「甲」提交「甲A」，落库后持锁等待
        th_a, box_a = _run(lambda: _revise(pid, uid, "甲A", "甲"))
        assert gate.entered.wait(timeout=10), "修订事务未进入持锁阶段"
        # B：同样按旧 base「甲」提交「甲B」——BEGIN IMMEDIATE 等 A 提交
        th_b, box_b = _run(lambda: _revise(pid, uid, "甲B", "甲"))
        threading.Event().wait(0.5)  # 确保 B 在写锁上排队
        gate.release.set()
        th_a.join(timeout=20)
        th_b.join(timeout=20)
        assert not th_a.is_alive() and not th_b.is_alive()
    finally:
        m.db.update_cue_text = gate.orig

    assert "error" not in box_a, box_a.get("error")
    err = box_b.get("error")
    assert isinstance(err, HTTPException) and err.status_code == 409, err
    # 终态：只有 A 的修订生效，B 未覆盖
    c0 = _cue(m.get_project(pid), 0)
    assert c0["text"] == "甲A" and c0["text_edited"] is True
    print("concurrent revisions: first commits, stale second 409 ok")


# ---------- 交付核验按生效文案 ----------

def test_verify_delivery_uses_effective_text():
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]
    # 生效时间轴：0–1000 甲 / 1040–2040 乙（min_gap=40 提案即原值+40）
    proj = _revise(pid, uid, "新文案", "甲")
    h1 = proj["timeline_hash"]

    def verify(content):
        return asyncio.run(m.verify_delivery(pid, _upload("d.srt", content)))

    # 回传稿用修订后的新文案：全部通过，指纹与详情一致
    v = verify("1\n00:00:00,000 --> 00:00:01,000\n新文案\n\n"
               "2\n00:00:01,040 --> 00:00:02,040\n乙\n")
    assert v["timeline_hash"] == h1
    assert v["summary"]["pass"] == 2 and v["matched_text_count"] == 2
    # 回传稿仍用导入原文「甲」：同文案配对数下降，不再全部通过
    v = verify("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
               "2\n00:00:01,040 --> 00:00:02,040\n乙\n")
    assert v["matched_text_count"] == 1 and v["summary"]["pass"] == 1
    # 恢复原文后回传「甲」重新全部通过
    proj = _reset(pid, uid, "新文案")
    v = verify("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
               "2\n00:00:01,040 --> 00:00:02,040\n乙\n")
    assert v["timeline_hash"] == proj["timeline_hash"]
    assert v["summary"]["pass"] == 2
    print("verify-delivery pairs by effective text ok")


# ---------- 旧库迁移 ----------

def test_migration_adds_edited_columns():
    path = "/tmp/test_text_revision_migrate.db"
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cues ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,"
        " cue_id TEXT NOT NULL, ord INTEGER NOT NULL,"
        " start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,"
        " text TEXT NOT NULL, raw_text TEXT NOT NULL,"
        " graphemes INTEGER NOT NULL, raw_hash TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO cues (project_id, cue_id, ord, start_ms, end_ms, text,"
        " raw_text, graphemes, raw_hash) VALUES (1,'1',1,0,1000,'甲','甲',1,'h')")
    conn.commit()
    db._migrate(conn)   # 旧库就地补列
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cues)")}
    assert {"edited_text", "edited_graphemes"} <= cols
    row = conn.execute("SELECT text, edited_text FROM cues").fetchone()
    assert row == ("甲", None), "迁移不得影响已存数据"
    conn.close()
    os.remove(path)
    print("legacy DB migration adds edited columns ok")


def main():
    test_revise_normalizes_and_updates_cps()
    test_blank_and_missing_rejected_without_partial_write()
    test_cps_alert_follows_revision_and_reset()
    test_timeline_hash_and_exports_follow_effective_text()
    test_revision_keeps_timing_adoption_and_gap_reviews()
    test_stale_base_text_rejected_409()
    test_concurrent_revisions_second_rejected()
    test_verify_delivery_uses_effective_text()
    test_migration_adds_edited_columns()
    print("text revision regression ok")


if __name__ == "__main__":
    main()
