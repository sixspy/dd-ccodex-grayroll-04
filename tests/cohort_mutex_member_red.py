"""红测甲：互斥人群第二挂必须拒写，成员正式册零增。

事故：主体 s-dual 同时进了互斥人群甲组与乙组，规则任引一组，求值两侧都可能命中。
口径（甲路径）：第二互斥挂靠直接拒写，成员正式册零增；账（member_count）实（成员行）一致。
"""
import uuid

from fastapi.testclient import TestClient

from app import app


def _h(client, user="admin", password="admin123"):
    t = client.post("/api/login", json={"username": user, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _mk_cohort(client, h, code, title, mutex_group=""):
    r = client.post(
        "/api/cohorts",
        headers=h,
        json={"code": code, "title": title, "note": "红测甲", "mutex_group": mutex_group},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _members(client, h, cid):
    return client.get(f"/api/cohorts/{cid}/members", headers=h).json()["items"]


def _count(client, h, cid):
    items = client.get("/api/cohorts", headers=h).json()["items"]
    return {c["id"]: c for c in items}[cid]["member_count"]


def test_second_mutex_attach_rejected_official_zero_growth():
    """已在互斥甲组的主体再挂乙组：拒写，乙组正式册零增，甲组不动。"""
    run = uuid.uuid4().hex[:8]
    group = f"mg-{run}"
    subject = f"s-dual-{run}"
    with TestClient(app) as client:
        h = _h(client)
        a = _mk_cohort(client, h, f"mxa-{run}", "互斥甲组", group)
        b = _mk_cohort(client, h, f"mxb-{run}", "互斥乙组", group)

        # 第一挂：主体进入互斥甲组，正式册 +1
        r1 = client.post(f"/api/cohorts/{a}/members", headers=h, json={"subject_key": subject})
        assert r1.status_code == 200, r1.text
        assert _count(client, h, a) == 1

        # 第二挂：同一主体再挂互斥乙组 —— 必须直接拒写
        r2 = client.post(f"/api/cohorts/{b}/members", headers=h, json={"subject_key": subject})
        assert r2.status_code == 409, f"第二互斥挂靠未被拒写: {r2.status_code} {r2.text}"

        # 账实互斥：乙组正式册零增，甲组保持 1，member_count 与实际成员行一致
        assert _members(client, h, b) == []
        assert _count(client, h, b) == 0
        assert [m["subject_key"] for m in _members(client, h, a)] == [subject]
        assert _count(client, h, a) == 1


def test_non_mutex_cohorts_still_allow_shared_subject():
    """不同互斥组（或无组）人群不受限：同一主体可同挂多册，防过度封死。"""
    run = uuid.uuid4().hex[:8]
    subject = f"s-free-{run}"
    with TestClient(app) as client:
        h = _h(client)
        a = _mk_cohort(client, h, f"fra-{run}", "自由甲", f"mg-a-{run}")
        b = _mk_cohort(client, h, f"frb-{run}", "自由乙", f"mg-b-{run}")
        c = _mk_cohort(client, h, f"frc-{run}", "自由丙", "")
        for cid in (a, b, c):
            r = client.post(f"/api/cohorts/{cid}/members", headers=h, json={"subject_key": subject})
            assert r.status_code == 200, r.text
        for cid in (a, b, c):
            assert _count(client, h, cid) == 1
            assert _count(client, h, cid) == len(_members(client, h, cid))
