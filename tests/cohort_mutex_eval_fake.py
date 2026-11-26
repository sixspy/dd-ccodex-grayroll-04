"""红测乙：求值不得把未升正的双挂当成合法命中。

口径（乙路径）：冲突挂靠只进影子成员草案，裁决升正前正式成员零改；
求值账实联查只认正式成员册，命中只对应单一人群成员。与甲路径不得混用。
"""
import uuid

from fastapi.testclient import TestClient

from app import app


def _h(client, user="admin", password="admin123"):
    t = client.post("/api/login", json={"username": user, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _mk_cohort(client, h, code, title, mutex_group):
    r = client.post(
        "/api/cohorts",
        headers=h,
        json={"code": code, "title": title, "note": "红测乙", "mutex_group": mutex_group},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _members(client, h, cid):
    return client.get(f"/api/cohorts/{cid}/members", headers=h).json()["items"]


def _count(client, h, cid):
    items = client.get("/api/cohorts", headers=h).json()["items"]
    return {c["id"]: c for c in items}[cid]["member_count"]


def _mk_flag_with_cohort_rule(client, h, run, tag, cohort_id):
    key = f"mutex.{tag}.{run}"
    r = client.post("/api/flags", headers=h, json={"key": key, "title": f"红测乙{tag}", "owner": "admin"})
    assert r.status_code == 200, r.text
    p = client.post(
        "/api/releases/publish",
        headers=h,
        json={
            "flag_id": r.json()["id"],
            "env_id": 1,
            "kind": "cohort",
            "percent": 0,
            "cohort_id": cohort_id,
            "summary": f"红测乙{tag}人群规则",
            "use_shadow": False,
        },
    )
    assert p.status_code == 200, p.text
    return key


def _eval(client, h, flag_key, subject):
    r = client.post(
        "/api/evaluate",
        headers=h,
        json={"flag_key": flag_key, "env_code": "dev", "subject_key": subject},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_unpromoted_shadow_attach_is_not_a_legal_hit():
    """未升正的双挂（影子草案）在求值侧不得命中；裁决拒升后正式册仍零改。"""
    run = uuid.uuid4().hex[:8]
    group = f"mg-{run}"
    subject = f"s-dual-{run}"
    with TestClient(app) as client:
        h = _h(client)
        a = _mk_cohort(client, h, f"exa-{run}", "互斥甲组", group)
        b = _mk_cohort(client, h, f"exb-{run}", "互斥乙组", group)

        # 主体正式挂互斥甲组
        r1 = client.post(f"/api/cohorts/{a}/members", headers=h, json={"subject_key": subject})
        assert r1.status_code == 200, r1.text

        # 乙路径：冲突挂靠只进影子成员草案，正式成员零改
        rs = client.post(f"/api/cohorts/{b}/members/shadow", headers=h, json={"subject_key": subject})
        assert rs.status_code == 200, rs.text
        assert rs.json()["mode"] == "shadow"
        sid = rs.json()["id"]
        assert _members(client, h, b) == []
        assert _count(client, h, b) == 0

        key_a = _mk_flag_with_cohort_rule(client, h, run, "a", a)
        key_b = _mk_flag_with_cohort_rule(client, h, run, "b", b)

        # 求值账实联查：只认正式册 —— 甲侧命中，乙侧（未升正双挂）不得命中
        ev_b = _eval(client, h, key_b, subject)
        assert ev_b["cohort_hit"] is False, f"未升正双挂被当成合法命中: {ev_b}"
        assert ev_b["enabled"] is False
        ev_a = _eval(client, h, key_a, subject)
        assert ev_a["cohort_hit"] is True
        assert ev_a["enabled"] is True

        # 命中只对应单一人群成员：互斥组内正式归属唯一且两侧一致
        assert ev_a["official_mutex_cohorts"] == ev_b["official_mutex_cohorts"]
        assert len(ev_a["official_mutex_cohorts"]) == 1

        # 裁决：互斥复核不过 → 拒升（409），正式册仍零改，求值仍不命中
        rp = client.post(f"/api/cohorts/member-shadows/{sid}/promote", headers=h, json={})
        assert rp.status_code == 409, rp.text
        assert _members(client, h, b) == []
        assert _count(client, h, b) == 0
        assert _eval(client, h, key_b, subject)["cohort_hit"] is False


def test_shadow_promote_single_side_then_hits():
    """乙路径全流程：无冲突主体走影子草案 → 裁决升正 → 求值单侧命中。"""
    run = uuid.uuid4().hex[:8]
    group = f"mg-{run}"
    subject = f"s-solo-{run}"
    code_b = f"epb-{run}"
    with TestClient(app) as client:
        h = _h(client)
        _mk_cohort(client, h, f"epa-{run}", "互斥甲组", group)
        b = _mk_cohort(client, h, code_b, "互斥乙组", group)

        rs = client.post(f"/api/cohorts/{b}/members/shadow", headers=h, json={"subject_key": subject})
        assert rs.status_code == 200, rs.text
        sid = rs.json()["id"]
        assert _members(client, h, b) == []  # 升正前正式零改

        rp = client.post(f"/api/cohorts/member-shadows/{sid}/promote", headers=h, json={})
        assert rp.status_code == 200, rp.text
        assert [m["subject_key"] for m in _members(client, h, b)] == [subject]
        assert _count(client, h, b) == 1

        key_b = _mk_flag_with_cohort_rule(client, h, run, "pb", b)
        ev = _eval(client, h, key_b, subject)
        assert ev["cohort_hit"] is True
        assert ev["enabled"] is True
        assert ev["official_mutex_cohorts"] == [code_b]
