"""红测争用：两名操作者抢「同一主体的人群写入互斥」。

口径：败方零正式改，只记争用痕（contention_marks）；
争用与取舍分两段 —— 先抢行锁，再在同一事务内做互斥取舍。
"""
import threading
import uuid

from fastapi.testclient import TestClient

from app import app


def _h(client, user, password):
    t = client.post("/api/login", json={"username": user, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _mk_cohort(client, h, code, title, mutex_group):
    r = client.post(
        "/api/cohorts",
        headers=h,
        json={"code": code, "title": title, "note": "红测争用", "mutex_group": mutex_group},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _members(client, h, cid):
    return client.get(f"/api/cohorts/{cid}/members", headers=h).json()["items"]


def _count(client, h, cid):
    items = client.get("/api/cohorts", headers=h).json()["items"]
    return {c["id"]: c for c in items}[cid]["member_count"]


def duel_cohort_mutex(url_a, url_b, headers_a, headers_b, payload):
    """并发双请求压锁：两个会话同时抢写同一主体的人群成员。"""
    barrier = threading.Barrier(2)
    results = []

    def fire(url, headers):
        client = TestClient(app)
        barrier.wait(timeout=10)
        r = client.post(url, headers=headers, json=payload)
        results.append((r.status_code, r.json()))

    ta = threading.Thread(target=fire, args=(url_a, headers_a))
    tb = threading.Thread(target=fire, args=(url_b, headers_b))
    ta.start()
    tb.start()
    ta.join(30)
    tb.join(30)
    assert len(results) == 2, "并发双请求未都返回"
    return results


def test_duel_cohort_mutex_single_winner():
    """双会话抢同一主体的互斥挂靠：一胜一败，败方零正式改，只记争用痕。"""
    run = uuid.uuid4().hex[:8]
    group = f"mg-{run}"
    subject = f"s-race-{run}"
    with TestClient(app) as client:
        ha = _h(client, "admin", "admin123")
        hw = _h(client, "writer", "writer123")
        a = _mk_cohort(client, ha, f"rca-{run}", "互斥甲组", group)
        b = _mk_cohort(client, ha, f"rcb-{run}", "互斥乙组", group)

        results = duel_cohort_mutex(
            f"/api/cohorts/{a}/members",
            f"/api/cohorts/{b}/members",
            ha,
            hw,
            {"subject_key": subject},
        )
        statuses = sorted(s for s, _ in results)
        assert statuses == [200, 409], f"双会话须一胜一败，实际 {statuses}: {results}"

        # 账实：主体只进一侧正式册，败方零正式改，member_count 与成员行一致
        mem_a = [m["subject_key"] for m in _members(client, ha, a)]
        mem_b = [m["subject_key"] for m in _members(client, ha, b)]
        assert (subject in mem_a) != (subject in mem_b), f"主体须只在一侧: A={mem_a} B={mem_b}"
        assert _count(client, ha, a) == len(mem_a)
        assert _count(client, ha, b) == len(mem_b)

        # 败方只记争用痕：锁键 = 互斥组 + 主体
        marks = client.get("/api/locks/contention", headers=ha, params={"limit": 200}).json()["items"]
        key = f"cohort-mutex:{group}:{subject}"
        mine = [m for m in marks if m["lock_key"] == key]
        assert mine, f"缺争用痕 {key}: {marks[:5]}"
        assert mine[0]["loser"] in ("admin", "writer")
