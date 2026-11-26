"""人群圈选：互斥挂靠甲路径拒写 + 乙路径影子成员草案。

正确性口径（账实互斥，两边不得混用）：
- 甲路径 `POST /{id}/members`：同事务写正式成员册；第二互斥挂靠直接拒写，正式册零增。
- 乙路径 `POST /{id}/members/shadow`：冲突挂靠只进影子成员草案，裁决升正前正式成员零改。
- 争用与取舍分两段：先抢行锁（同一主体的人群写入互斥），再在同一事务内做互斥取舍；
  败方零正式改，只记争用痕（contention_marks）。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_svc import write_audit, write_history
from auth import current_user, require_writer
from db import connect
from locks import acquire, release
from validators import require_str

router = APIRouter(prefix="/api/cohorts", tags=["cohorts"])


class CohortIn(BaseModel):
    code: str
    title: str
    note: str = ""
    mutex_group: str = ""


class MemberIn(BaseModel):
    subject_key: str


def _lock_key(cohort_id: int, group: str, subject_key: str) -> str:
    """同一主体的人群写入互斥锁：互斥组内同主体串行，无组人群退化为册内键。"""
    if group:
        return f"cohort-mutex:{group}:{subject_key}"
    return f"cohort-member:{cohort_id}:{subject_key}"


def _mutex_conflict(conn, group: str, subject_key: str, exclude_cohort_id: int):
    """账实联查：同一事务内跨表联查正式成员册，找同一互斥组他群的正式挂靠。"""
    if not group:
        return None
    return conn.execute(
        """
        SELECT c.id, c.code, c.title
        FROM cohort_members cm
        JOIN cohorts c ON c.id = cm.cohort_id
        WHERE cm.subject_key = ? AND c.mutex_group = ? AND c.id != ?
        ORDER BY c.id
        """,
        (subject_key, group, exclude_cohort_id),
    ).fetchone()


def _mark_contention(conn, key: str, loser: str, winner: str) -> None:
    """记争用痕：败方零正式改，只留痕迹。"""
    conn.execute(
        "INSERT INTO contention_marks(lock_key, loser, winner, created_at) VALUES(?,?,?,?)",
        (key, loser, winner, time.time()),
    )


@router.get("")
def list_cohorts(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM cohorts ORDER BY id").fetchall()]
    return {"items": rows}


@router.get("/member-shadows")
def list_member_shadows(cohort_id: int | None = None, authorization: str | None = Header(default=None)):
    """影子成员草案列表（乙路径台账）。"""
    current_user(authorization)
    sql = """
        SELECT s.*, c.code AS cohort_code, c.mutex_group
        FROM cohort_member_shadows s
        JOIN cohorts c ON c.id = s.cohort_id
    """
    args: tuple = ()
    if cohort_id:
        sql += " WHERE s.cohort_id=?"
        args = (cohort_id,)
    sql += " ORDER BY s.id DESC"
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    return {"items": rows}


@router.post("/member-shadows/{shadow_id}/promote")
def promote_member_shadow(shadow_id: int, authorization: str | None = Header(default=None)):
    """裁决升正：同一事务内复核互斥并原子落正式册；复核不过则草案废弃，正式零改。"""
    user = current_user(authorization)
    require_writer(user)
    with connect() as conn:
        sh = conn.execute("SELECT * FROM cohort_member_shadows WHERE id=?", (shadow_id,)).fetchone()
        if not sh or sh["status"] != "open":
            raise HTTPException(400, detail="影子成员草案不可升正")
        cohort = conn.execute("SELECT * FROM cohorts WHERE id=?", (sh["cohort_id"],)).fetchone()
        if not cohort:
            raise HTTPException(404, detail="人群不存在")
        group = cohort["mutex_group"] or ""
        sk = sh["subject_key"]
        key = _lock_key(sh["cohort_id"], group, sk)
        acquire(conn, key, user["username"])
        try:
            conflict = _mutex_conflict(conn, group, sk, sh["cohort_id"])
            dup = conn.execute(
                "SELECT 1 FROM cohort_members WHERE cohort_id=? AND subject_key=?",
                (sh["cohort_id"], sk),
            ).fetchone()
            if conflict or dup:
                # 裁决拒升：草案废弃，正式册零改，只记争用痕与审计
                conn.execute(
                    "UPDATE cohort_member_shadows SET status='discarded' WHERE id=?", (shadow_id,)
                )
                reason = f"已在互斥人群 {conflict['code']}" if conflict else "已在目标人群"
                winner = f"official:{conflict['code']}" if conflict else f"cohort:{sh['cohort_id']}"
                _mark_contention(conn, key, user["username"], winner)
                write_audit(
                    conn, user["username"], "cohort_mutex_reject", "cohort_member_shadow",
                    shadow_id, f"{sk} {reason}，裁决拒升",
                )
                conn.commit()
                raise HTTPException(409, detail=f"互斥复核未过：{sk} {reason}，裁决拒升")
            cur = conn.execute(
                "INSERT INTO cohort_members(cohort_id, subject_key) VALUES(?,?)",
                (sh["cohort_id"], sk),
            )
            conn.execute(
                "UPDATE cohorts SET member_count = member_count + 1 WHERE id=?", (sh["cohort_id"],)
            )
            conn.execute("UPDATE cohort_member_shadows SET status='promoted' WHERE id=?", (shadow_id,))
            write_history(
                conn, user["username"], "cohort_member_promote",
                f"影子成员升正 人群{sh['cohort_id']} 加入 {sk}",
            )
            write_audit(
                conn, user["username"], "cohort_member_promote", "cohort_member",
                cur.lastrowid, f"from_shadow={shadow_id}",
            )
            conn.commit()
            return {"mode": "official", "cohort_id": sh["cohort_id"], "subject_key": sk}
        finally:
            release(conn, key, user["username"])
            conn.commit()


@router.post("/member-shadows/{shadow_id}/discard")
def discard_member_shadow(shadow_id: int, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    with connect() as conn:
        sh = conn.execute("SELECT * FROM cohort_member_shadows WHERE id=?", (shadow_id,)).fetchone()
        if not sh or sh["status"] != "open":
            raise HTTPException(400, detail="影子成员草案不可废弃")
        conn.execute("UPDATE cohort_member_shadows SET status='discarded' WHERE id=?", (shadow_id,))
        write_audit(
            conn, user["username"], "cohort_member_shadow_discard", "cohort_member_shadow",
            shadow_id, sh["subject_key"],
        )
        conn.commit()
    return {"id": shadow_id, "status": "discarded"}


@router.get("/{cohort_id}/members")
def list_members(cohort_id: int, authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM cohort_members WHERE cohort_id=? ORDER BY id", (cohort_id,)
            ).fetchall()
        ]
    return {"items": rows}


@router.post("")
def create_cohort(body: CohortIn, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    code = require_str(body.code, "人群码", 2)
    title = require_str(body.title, "标题", 2)
    mutex_group = (body.mutex_group or "").strip()
    with connect() as conn:
        if conn.execute("SELECT 1 FROM cohorts WHERE code=?", (code,)).fetchone():
            raise HTTPException(400, detail="人群码已存在")
        cur = conn.execute(
            "INSERT INTO cohorts(code, title, member_count, note, mutex_group) VALUES(?,?,0,?,?)",
            (code, title, body.note or "", mutex_group),
        )
        cid = cur.lastrowid
        write_audit(conn, user["username"], "create_cohort", "cohort", cid, code)
        conn.commit()
    return {"id": cid, "code": code, "mutex_group": mutex_group}


@router.post("/{cohort_id}/members")
def add_member(cohort_id: int, body: MemberIn, authorization: str | None = Header(default=None)):
    """甲路径：同事务写正式成员册；第二互斥挂靠直接拒写，正式册零增。"""
    user = current_user(authorization)
    require_writer(user)
    sk = require_str(body.subject_key, "主体键", 2)
    with connect() as conn:
        cohort = conn.execute("SELECT * FROM cohorts WHERE id=?", (cohort_id,)).fetchone()
        if not cohort:
            raise HTTPException(404, detail="人群不存在")
        group = cohort["mutex_group"] or ""
        key = _lock_key(cohort_id, group, sk)
        acquire(conn, key, user["username"])
        try:
            if conn.execute(
                "SELECT 1 FROM cohort_members WHERE cohort_id=? AND subject_key=?", (cohort_id, sk)
            ).fetchone():
                raise HTTPException(400, detail="主体已在人群中")
            conflict = _mutex_conflict(conn, group, sk, cohort_id)
            if conflict:
                # 取舍段：第二互斥挂靠拒写，正式册零增，只记争用痕与审计
                _mark_contention(conn, key, user["username"], f"official:{conflict['code']}")
                write_audit(
                    conn, user["username"], "cohort_mutex_reject", "cohort", cohort_id,
                    f"{sk} 已在互斥人群 {conflict['code']}，第二挂拒写",
                )
                raise HTTPException(
                    409, detail=f"主体已在互斥人群 {conflict['code']}，第二挂拒写"
                )
            cur = conn.execute(
                "INSERT INTO cohort_members(cohort_id, subject_key) VALUES(?,?)", (cohort_id, sk)
            )
            conn.execute(
                "UPDATE cohorts SET member_count = member_count + 1 WHERE id=?", (cohort_id,)
            )
            write_history(conn, user["username"], "cohort_add", f"人群{cohort_id} 加入 {sk}")
            write_audit(conn, user["username"], "cohort_add", "cohort_member", cur.lastrowid, sk)
            conn.commit()
            return {"cohort_id": cohort_id, "subject_key": sk}
        finally:
            release(conn, key, user["username"])
            conn.commit()


@router.post("/{cohort_id}/members/shadow")
def shadow_member(cohort_id: int, body: MemberIn, authorization: str | None = Header(default=None)):
    """乙路径：冲突挂靠只进影子成员草案，裁决升正前正式成员零改。"""
    user = current_user(authorization)
    require_writer(user)
    sk = require_str(body.subject_key, "主体键", 2)
    with connect() as conn:
        cohort = conn.execute("SELECT * FROM cohorts WHERE id=?", (cohort_id,)).fetchone()
        if not cohort:
            raise HTTPException(404, detail="人群不存在")
        group = cohort["mutex_group"] or ""
        key = _lock_key(cohort_id, group, sk)
        acquire(conn, key, user["username"])
        try:
            if conn.execute(
                "SELECT 1 FROM cohort_members WHERE cohort_id=? AND subject_key=?", (cohort_id, sk)
            ).fetchone():
                raise HTTPException(400, detail="主体已在人群中")
            if conn.execute(
                "SELECT 1 FROM cohort_member_shadows WHERE cohort_id=? AND subject_key=? AND status='open'",
                (cohort_id, sk),
            ).fetchone():
                raise HTTPException(400, detail="影子草案已存在")
            conflict = _mutex_conflict(conn, group, sk, cohort_id)
            now = time.time()
            cur = conn.execute(
                "INSERT INTO cohort_member_shadows(cohort_id, subject_key, status, created_at, created_by) "
                "VALUES(?,?,?,?,?)",
                (cohort_id, sk, "open", now, user["username"]),
            )
            sid = cur.lastrowid
            write_history(
                conn, user["username"], "cohort_member_shadow", f"人群{cohort_id} 影子草案 {sk}"
            )
            write_audit(
                conn, user["username"], "cohort_member_shadow", "cohort_member_shadow", sid,
                "冲突挂靠只进影子草案" if conflict else "影子草案登记",
            )
            conn.commit()
            return {
                "mode": "shadow",
                "id": sid,
                "cohort_id": cohort_id,
                "subject_key": sk,
                "conflict": bool(conflict),
                "official_changed": False,
            }
        finally:
            release(conn, key, user["username"])
            conn.commit()
