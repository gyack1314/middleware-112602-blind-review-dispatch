"""复测调度中间件的规则测试：独立性、盲审、更正、仲裁、容量与崩溃恢复。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

# 与入口一致：隔离模式（python3 -I）下显式引导仓库目录
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from retest_scheduler.engine import RetestEngine
from retest_scheduler.middleware import RetestMiddleware
from retest_scheduler.models import (
    DECIDE,
    EXPIRED,
    MIGRATED,
    PENDING,
    REVIEW,
    REVOKED,
    SUBMITTED,
    SUPERSEDED,
    EngineError,
)
from retest_scheduler.storage import JsonlStore


def feed(mw, rows):
    """rows: (at, kind, record) 元组列表；返回回执列表。"""
    out = []
    for at, kind, record in rows:
        out.append(mw.handle(at, kind, record))
    return out


def lab(lab_id, affinity="g", skills=("grip",), slots=1, active=True):
    return {
        "lab": lab_id,
        "affinity": affinity,
        "skills": list(skills),
        "slots": slots,
        "active": active,
    }


def packet(evidence=1, deadline=20, case="c", item="force", skill="grip"):
    return {
        "case": case,
        "item": item,
        "skill": skill,
        "evidence": evidence,
        "deadline": deadline,
    }


def claim(lab_id, role=REVIEW, request="r", case="c", item="force"):
    return {"lab": lab_id, "case": case, "item": item, "role": role, "request": request}


def submit(lab_id, assignment, evidence=1, verdict="pass", refs=None):
    return {
        "lab": lab_id,
        "assignment": assignment,
        "evidence": evidence,
        "verdict": verdict,
        "refs": refs or [],
    }


class FreshMiddleware:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.mw = RetestMiddleware(self.dir, snapshot_interval=1000)
        return self.mw, self.dir

    def __exit__(self, *exc):
        self.mw.close()
        self._tmp.cleanup()


class RuleTestCase(unittest.TestCase):
    def assert_engine_error(self, code, fn):
        try:
            fn()
        except EngineError as error:
            self.assertEqual(error.code, code)
        else:
            self.fail(f"期望抛出 EngineError({code})，但未抛出")


class IndependenceTests(RuleTestCase):
    def test_same_affinity_different_lab_ids_are_not_independent(self):
        with FreshMiddleware() as (mw, _):
            feed(mw, [(0, "lab", lab("L1", "family")), (0, "lab", lab("L2", "family"))])
            feed(mw, [(1, "packet", packet())])
            feed(mw, [(2, "claim", claim("L1", request="a"))])
            self.assert_engine_error("affinity_conflict", lambda: mw.handle(3, "claim", claim("L2", request="b")))

    def test_two_independent_groups_certify_on_compatible_verdicts(self):
        with FreshMiddleware() as (mw, _):
            feed(mw, [
                (0, "lab", lab("L1", "g1")),
                (0, "lab", lab("L2", "g2")),
                (1, "packet", packet()),
                (2, "claim", claim("L1", request="a")),
                (3, "claim", claim("L2", request="b")),
                (4, "submit", submit("L1", 1, verdict="pass")),
                (5, "submit", submit("L2", 2, verdict="pass")),
            ])
            cert = mw.engine.state.packets[("c", "force")].certificates
            self.assertEqual(cert, {1: 1})
            trace = mw.trace_certificate(1)
            self.assertTrue(trace["independence_ok"])
            self.assertEqual(trace["certificate"]["source"], "review")
            self.assertEqual(trace["certificate"]["decision_seqs"], [1, 2])

    def test_substitute_claim_keeps_earlier_valid_result(self):
        # L1 已提交；L2 领取后退出；L3 替补，L1 的结果必须保留并参与出证
        with FreshMiddleware() as (mw, _):
            feed(mw, [
                (0, "lab", lab("L1", "g1")),
                (0, "lab", lab("L2", "g2")),
                (0, "lab", lab("L3", "g3")),
                (1, "packet", packet()),
                (2, "claim", claim("L1", request="a")),
                (3, "claim", claim("L2", request="b")),
                (4, "submit", submit("L1", 1, verdict="pass")),
            ])
            # L2 退出，在途任务被撤回，份额释放
            mw.handle(5, "lab", lab("L2", "g2", active=False))
            self.assertEqual(mw.engine.state.assignments[2].status, REVOKED)
            feed(mw, [
                (6, "claim", claim("L3", request="c")),
                (7, "submit", submit("L3", 3, verdict="pass")),
            ])
            trace = mw.trace_certificate(1)
            self.assertEqual(trace["certificate"]["decision_seqs"], [1, 3])

    def test_blockers_distinguish_capability_conflict_and_full_slots(self):
        with FreshMiddleware() as (mw, _):
            feed(mw, [
                (0, "lab", lab("L1", "g1")),
                (0, "lab", lab("NoSkill", "g2", skills=("other",))),
                (0, "lab", lab("Full", "g3", slots=1)),
                (1, "packet", packet()),
                (2, "claim", claim("L1", request="a")),
                (3, "submit", submit("L1", 1)),
                # Full 的唯一份额被另一个项目占满
                (1, "packet", packet(case="c", item="other", skill="grip")),
                (2, "claim", claim("Full", request="x", item="other")),
            ])
            view = mw.engine.inspect_case("c")
            item_force = next(i for i in view["items"] if i["item"] == "force")
            codes = {
                (b["lab"], r["code"])
                for b in item_force["blockers"]
                for r in b["reasons"]
            }
            self.assertIn(("NoSkill", "skill_unsupported"), codes)
            self.assertIn(("Full", "lab_slots_full"), codes)
            # 全部属于已占用组时给出无独立群体说明
            mw.handle(4, "lab", lab("L1b", "g1"))
            view2 = mw.engine.inspect_case("c")
            item_force2 = next(i for i in view2["items"] if i["item"] == "force")
            types = {b["type"] for b in item_force2["blockers"]}
            self.assertIn("candidate_blocked", types)


class CapacityTests(RuleTestCase):
    def test_affinity_shared_slots_span_lab_ids(self):
        with FreshMiddleware() as (mw, _):
            feed(mw, [
                (0, "lab", lab("L1", "family", slots=1)),
                (0, "lab", lab("L2", "family", slots=1)),
                (0, "lab", lab("M", "ind", slots=2)),
                (1, "packet", packet(item="i1")),
                (1, "packet", packet(item="i2")),
                (1, "packet", packet(item="i3")),
                (2, "claim", claim("L1", request="a", item="i1")),
            ])
            # family 组容量 2，L2 仍可在另一项目领取
            r = mw.handle(3, "claim", claim("L2", request="b", item="i2"))
            self.assertEqual(r["assignment"], 2)
            # M 有 2 份额：可同时占两起
            r1 = mw.handle(4, "claim", claim("M", request="c", item="i1"))
            r2 = mw.handle(5, "claim", claim("M", request="d", item="i2"))
            self.assertEqual((r1["assignment"], r2["assignment"]), (3, 4))
            self.assert_engine_error("lab_slots_full", lambda: mw.handle(6, "claim", claim("M", request="e", item="i3")))
            # 提交后份额释放，可在第三个项目领取（同项目同组不能重复占独立席位）
            mw.handle(7, "submit", submit("M", 3, verdict="pass"))
            again = mw.handle(8, "claim", claim("M", request="f", item="i3"))
            self.assertEqual(again["status"], PENDING)
            self.assertEqual(again["assignment"], 5)

    def test_revoke_releases_capacity_for_redispatch(self):
        with FreshMiddleware() as (mw, _):
            feed(mw, [
                (0, "lab", lab("L1", "g1")),
                (0, "lab", lab("L2", "g2")),
                (1, "packet", packet()),
                (2, "claim", claim("L1", request="a")),
            ])
            mw.handle(3, "revoke", {"assignment": 1, "reason": "运营撤销"})
            self.assertEqual(mw.engine.state.assignments[1].status, REVOKED)
            r = mw.handle(4, "claim", claim("L1", request="a2"))
            self.assertEqual(r["assignment"], 2)

    def test_late_submission_after_deadline_cannot_pose_as_substitute(self):
        with FreshMiddleware() as (mw, _):
            feed(mw, [
                (0, "lab", lab("L1", "g1")),
                (0, "lab", lab("L2", "g2")),
                (1, "packet", packet(deadline=10)),
                (2, "claim", claim("L1", request="a")),
            ])
            # 截止后直接提交：任务仍 pending，触发晚报分支
            self.assert_engine_error(
                "late_submission", lambda: mw.handle(11, "submit", submit("L1", 1))
            )
            # 查询时清扫：任务过期，份额释放
            mw.handle(12, "inspect", {"case": "c"})
            self.assertEqual(mw.engine.state.assignments[1].status, EXPIRED)
            # 重新开放一个同项目的新一轮（新证据、新截止），替补可正常领取
            mw.handle(13, "packet", packet(evidence=2, deadline=40))
            r = mw.handle(14, "claim", claim("L2", request="b"))
            self.assertEqual(r["assignment"], 2)
            self.assertEqual(r["evidence"], 2)


class BlindReviewTests(RuleTestCase):
    def _disagreement(self, mw):
        feed(mw, [
            (0, "lab", lab("L1", "g1")),
            (0, "lab", lab("L2", "g2")),
            (0, "lab", lab("N", "g3")),
            (1, "packet", packet()),
            (2, "claim", claim("L1", request="a")),
            (3, "claim", claim("L2", request="b")),
            (4, "submit", submit("L1", 1, verdict="pass")),
            (5, "submit", submit("L2", 2, verdict="return")),
        ])

    def test_decide_claim_only_after_disagreement_and_arbiter_independent(self):
        with FreshMiddleware() as (mw, _):
            self._disagreement(mw)
            # 同组不能仲裁
            self.assert_engine_error("arbiter_not_independent", lambda: mw.handle(6, "claim", claim("L1", role=DECIDE, request="z")))
            # 没有分歧时不能领 decide（另起干净项目验证）
            receipt = mw.handle(7, "claim", claim("N", role=DECIDE, request="z"))
            self.assertEqual(receipt["role"], DECIDE)
            blind = receipt["blind_packet"]
            # 盲包不得出现当事机构身份
            raw = json.dumps(blind, ensure_ascii=False)
            self.assertNotIn("L1", raw)
            self.assertNotIn("L2", raw)
            self.assertEqual(
                blind["disagreement"], {"party_alpha": "pass", "party_beta": "return"}
            )

    def test_lab_inspect_never_shows_other_parties_identities(self):
        with FreshMiddleware() as (mw, _):
            self._disagreement(mw)
            mw.handle(7, "claim", claim("N", role=DECIDE, request="z"))
            view = mw.engine.inspect_lab("N")
            raw = json.dumps(view, ensure_ascii=False)
            self.assertNotIn("L1", raw)
            self.assertNotIn("L2", raw)
            self.assertIn("party_alpha", raw)

    def test_arbitration_certificate_references_item_and_exact_evidence(self):
        with FreshMiddleware() as (mw, _):
            self._disagreement(mw)
            feed(mw, [
                (7, "claim", claim("N", role=DECIDE, request="z")),
                (8, "submit", submit("N", 3, verdict="return", refs=["ev1#hash"])),
            ])
            trace = mw.trace_certificate(1)
            cert = trace["certificate"]
            self.assertEqual(cert["source"], "arbitration")
            self.assertEqual((cert["case"], cert["item"], cert["evidence"]),
                             ("c", "force", 1))
            self.assertEqual(cert["verdict"], "return")
            self.assertEqual(cert["decision_seqs"], [1, 2, 3])
            # 出证后多余在途席位自动关闭
            self.assertEqual(mw.engine.state.assignments[3].status, SUPERSEDED)


class RevisionTests(RuleTestCase):
    def test_unsubmitted_migrates_submitted_stays_on_old_version(self):
        with FreshMiddleware() as (mw, _):
            feed(mw, [
                (0, "lab", lab("L1", "g1")),
                (0, "lab", lab("L2", "g2")),
                (0, "lab", lab("X", "gx")),
                (0, "lab", lab("Y", "gy")),
                (1, "packet", packet(evidence=1, deadline=20)),
                (2, "claim", claim("L1", request="a")),
                (3, "claim", claim("L2", request="b")),
                (4, "submit", submit("L1", 1, verdict="pass")),
            ])
            # 无关项目的在途工作
            feed(mw, [
                (1, "packet", packet(case="c", item="unrelated", deadline=30)),
                (2, "claim", claim("X", request="u", item="unrelated")),
            ])
            # 更正：已授出编号 1、2、3，迁移任务取新编号 4
            mw.handle(6, "packet", packet(evidence=2, deadline=30))
            self.assertEqual(mw.engine.state.assignments[1].status, SUBMITTED)
            self.assertEqual(mw.engine.state.assignments[2].status, MIGRATED)
            new_seq = mw.engine.state.assignments[2].migrated_to
            self.assertEqual(new_seq, 4)
            migrated = mw.engine.state.assignments[new_seq]
            self.assertEqual((migrated.evidence, migrated.lab_id, migrated.role),
                             (2, "L2", REVIEW))
            # 份额未因迁移增加
            self.assertEqual(mw.engine.lab_usage("L2"), 1)
            # 旧结果不能就新版本出证：版本 2 缺两个席位
            packet_state = mw.engine.state.packets[("c", "force")]
            self.assertEqual(packet_state.current_evidence, 2)
            self.assertEqual(packet_state.certificates, {})
            # 无关项目不受影响
            self.assertEqual(mw.engine.state.assignments[3].status, PENDING)
            # 就迁移任务提交旧版本必须被拒绝
            self.assert_engine_error("evidence_mismatch", lambda: mw.handle(7, "submit", submit("L2", new_seq, evidence=1, verdict="pass")))
            # 新版本重新凑齐两个独立组后出证
            mw.handle(8, "submit", submit("L2", new_seq, evidence=2, verdict="pass"))
            feed(mw, [(9, "claim", claim("Y", request="v")),
                      (10, "submit", submit("Y", 5, evidence=2, verdict="pass"))])
            self.assertEqual(packet_state.certificates, {2: 1})
            history = mw.engine.inspect_case("c")
            force = next(i for i in history["items"] if i["item"] == "force")
            self.assertEqual(
                [v["evidence"] for v in force["version_history"]], [1, 2]
            )
            self.assertIsNone(force["version_history"][0]["certificate"])

    def test_revision_overtakes_open_arbitration(self):
        with FreshMiddleware() as (mw, _):
            feed(mw, [
                (0, "lab", lab("A", "a")),
                (0, "lab", lab("B", "b")),
                (0, "lab", lab("C", "c")),
                (1, "packet", packet(evidence=1, deadline=21)),
                (2, "claim", claim("A", request="x")),
                (3, "claim", claim("B", request="y")),
                (4, "submit", submit("A", 1, verdict="pass")),
                (5, "submit", submit("B", 2, verdict="return")),
                (6, "claim", claim("C", role=DECIDE, request="z")),
            ])
            # 更正发生在仲裁回执之前
            mw.handle(7, "packet", packet(evidence=2, deadline=30))
            case = mw.engine.state.cases[1]
            self.assertEqual(case.status, "abandoned")
            self.assertEqual(mw.engine.state.assignments[3].status, REVOKED)
            # 旧仲裁任务的晚报被拒绝（旧结果晚报不能冒充替补）
            self.assert_engine_error("assignment_not_open", lambda: mw.handle(8, "submit", submit("C", 3, evidence=1, verdict="pass")))
            # 旧决定只归旧版本：版本 2 重新等待两份独立复测
            view = mw.engine.inspect_case("c")
            self.assertEqual(view["waiting"][0]["open_review_slots"], 2)

    def test_decided_arbitration_on_old_version_does_not_certify_new_one(self):
        with FreshMiddleware() as (mw, _):
            feed(mw, [
                (0, "lab", lab("A", "a")),
                (0, "lab", lab("B", "b")),
                (0, "lab", lab("C", "c")),
                (1, "packet", packet(evidence=1, deadline=30)),
                (2, "claim", claim("A", request="x")),
                (3, "claim", claim("B", request="y")),
                (4, "submit", submit("A", 1, verdict="pass")),
                (5, "submit", submit("B", 2, verdict="return")),
                (6, "claim", claim("C", role=DECIDE, request="z")),
                (7, "submit", submit("C", 3, verdict="pass")),
            ])
            self.assertEqual(
                mw.engine.state.packets[("c", "force")].certificates, {1: 1}
            )
            mw.handle(8, "packet", packet(evidence=2, deadline=40))
            packet_state = mw.engine.state.packets[("c", "force")]
            # 版本 2 没有证书，旧证书只归版本 1
            self.assertEqual(packet_state.certificates, {1: 1})
            self.assertIsNone(packet_state.certificates.get(2))


class ExitAndRedispatchTests(RuleTestCase):
    def test_exit_after_submission_keeps_result_and_blocks_new_claims_only(self):
        # 原方已提交完且证据未更正：退出只影响新的领取，旧结果保留
        with FreshMiddleware() as (mw, _):
            feed(mw, [
                (0, "lab", lab("L1", "g1")),
                (0, "lab", lab("L2", "g2")),
                (1, "packet", packet(deadline=30)),
                (2, "claim", claim("L1", request="a")),
                (3, "submit", submit("L1", 1, verdict="pass")),
            ])
            mw.handle(4, "lab", lab("L1", "g1", active=False))
            # 已提交结果仍有效，可供出证
            self.assertEqual(mw.engine.state.assignments[1].status, SUBMITTED)
            # 退出机构不能再领新任务
            self.assert_engine_error(
                "lab_inactive", lambda: mw.handle(5, "claim", claim("L1", request="b"))
            )
            # 第二独立组提交后，L1 的旧结果仍参与出证
            feed(mw, [
                (6, "claim", claim("L2", request="c")),
                (7, "submit", submit("L2", 2, verdict="pass")),
            ])
            trace = mw.trace_certificate(1)
            self.assertEqual(trace["certificate"]["decision_seqs"], [1, 2])

    def test_arbiter_exit_redispatches_disagreement_while_reviews_stand(self):
        with FreshMiddleware() as (mw, _):
            feed(mw, [
                (0, "lab", lab("L1", "g1")),
                (0, "lab", lab("L2", "g2")),
                (0, "lab", lab("N1", "g3")),
                (0, "lab", lab("N2", "g4")),
                (1, "packet", packet(deadline=30)),
                (2, "claim", claim("L1", request="a")),
                (3, "claim", claim("L2", request="b")),
                (4, "submit", submit("L1", 1, verdict="pass")),
                (5, "submit", submit("L2", 2, verdict="return")),
                (6, "claim", claim("N1", role=DECIDE, request="z1")),
            ])
            # 仲裁席位已占，第二个仲裁者不能重复领
            self.assert_engine_error(
                "arbiter_seat_taken",
                lambda: mw.handle(7, "claim", claim("N2", role=DECIDE, request="z2")),
            )
            # 仲裁者退出：在途仲裁任务撤回，争议案保持 open，两份复测不动
            mw.handle(8, "lab", lab("N1", "g3", active=False))
            case = mw.engine.state.cases[1]
            self.assertEqual(case.status, "open")
            self.assertIsNone(case.arbiter_assignment)
            self.assertEqual(mw.engine.state.assignments[3].status, REVOKED)
            self.assertEqual(mw.engine.state.assignments[1].status, SUBMITTED)
            # 新仲裁者可领取并完成出证
            feed(mw, [
                (9, "claim", claim("N2", role=DECIDE, request="z2")),
                (10, "submit", submit("N2", 4, verdict="return")),
            ])
            trace = mw.trace_certificate(1)
            self.assertEqual(trace["certificate"]["source"], "arbitration")
            self.assertEqual(trace["certificate"]["decision_seqs"], [1, 2, 4])

    def test_items_progress_independently_under_shared_capacity(self):
        with FreshMiddleware() as (mw, _):
            feed(mw, [
                (0, "lab", lab("L1", "g1", slots=2)),
                (0, "lab", lab("L2", "g2", slots=2)),
                (1, "packet", packet(item="i1", deadline=20)),
                (1, "packet", packet(item="i2", deadline=40)),
                (2, "claim", claim("L1", request="a1", item="i1")),
                (2, "claim", claim("L1", request="a2", item="i2")),
                (3, "claim", claim("L2", request="b1", item="i1")),
            ])
            # i1 先出证；i2 仍等待第二独立组
            feed(mw, [
                (4, "submit", submit("L1", 1, verdict="pass")),
                (5, "submit", submit("L2", 3, verdict="pass")),
            ])
            view = mw.engine.inspect_case("c")
            by_item = {v["item"]: v for v in view["items"]}
            self.assertTrue(by_item["i1"]["certified"])
            self.assertFalse(by_item["i2"]["certified"])
            self.assertEqual(by_item["i2"]["open_review_slots"], 1)
            # i1 出证不影响 i2 的在途任务
            self.assertEqual(mw.engine.state.assignments[2].status, PENDING)
            # i2 随后独立完成
            feed(mw, [
                (6, "claim", claim("L2", request="b2", item="i2")),
                (7, "submit", submit("L1", 2, verdict="return")),
                (8, "submit", submit("L2", 4, verdict="return")),
            ])
            view2 = mw.engine.inspect_case("c")
            self.assertEqual(view2["waiting"], [])


class IdempotencyAndRecoveryTests(RuleTestCase):
    def test_duplicate_request_returns_same_assignment_without_new_slot(self):
        with FreshMiddleware() as (mw, _):
            feed(mw, [
                (0, "lab", lab("L1", "g1")),
                (1, "packet", packet()),
                (2, "claim", claim("L1", request="dup")),
            ])
            again = mw.handle(3, "claim", claim("L1", request="dup"))
            self.assertTrue(again["duplicate"])
            self.assertEqual(again["assignment"], 1)
            self.assertEqual(len(mw.engine.state.assignments), 1)

    def test_restart_rebuilds_slots_and_blinding_from_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            with RetestMiddleware(directory) as mw:
                feed(mw, [
                    (0, "lab", lab("L1", "g1")),
                    (0, "lab", lab("L2", "g2")),
                    (0, "lab", lab("N", "g3")),
                    (1, "packet", packet(deadline=20)),
                    (2, "claim", claim("L1", request="a")),
                    (3, "claim", claim("L2", request="b")),
                    (4, "submit", submit("L1", 1, verdict="pass")),
                    (5, "submit", submit("L2", 2, verdict="return")),
                    (6, "claim", claim("N", role=DECIDE, request="z")),
                ])
            # 模拟服务异常退出后重新运行：新进程从同一目录打开
            with RetestMiddleware(directory) as mw2:
                self.assertEqual(mw2.engine.lab_usage("N"), 1)
                task = mw2.engine.state.assignments[3]
                self.assertEqual(task.status, PENDING)
                self.assertIn("party_alpha", task.blinded)
                raw = json.dumps(task.blinded, ensure_ascii=False)
                self.assertNotIn("L1", raw)
                # 序号单调继续，不与历史编号冲突
                mw2.handle(7, "revoke", {"assignment": 3, "reason": "x"})
            # 第三次打开：日志重放结果一致
            with RetestMiddleware(directory) as mw3:
                self.assertEqual(mw3.engine.state.assignments[3].status, REVOKED)
                self.assertEqual(mw3.engine.lab_usage("N"), 0)
                journal = JsonlStore(directory).journal_records()
                seqs = [r["seq"] for r in journal]
                self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

    def test_snapshot_then_replay_matches_full_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            with RetestMiddleware(directory, snapshot_interval=3) as mw:
                feed(mw, [
                    (0, "lab", lab("L1", "g1")),
                    (0, "lab", lab("L2", "g2")),
                    (1, "packet", packet()),
                    (2, "claim", claim("L1", request="a")),
                    (3, "claim", claim("L2", request="b")),
                    (4, "submit", submit("L1", 1, verdict="pass")),
                    (5, "submit", submit("L2", 2, verdict="pass")),
                ])
                expected = mw.engine.state_json()
            with RetestMiddleware(directory, snapshot_interval=3) as mw2:
                self.assertEqual(mw2.engine.state_json(), expected)
                self.assertTrue(Path(directory, "snapshot.json").exists())


class EnginePureCommandTests(unittest.TestCase):
    """命令只返回事件，不修改状态；落盘失败时内存不被污染。"""

    def test_command_does_not_mutate_state_before_commit(self):
        engine = RetestEngine()
        engine.apply_event("lab_registered", {
            "lab": "L1", "affinity": "g1", "skills": ["grip"],
            "slots": 1, "active": True,
        })
        engine.apply_event("packet_published", {
            "case": "c", "item": "force", "skill": "grip",
            "evidence": 1, "deadline": 20, "published_at": 1,
        })
        before = engine.state_json()
        events, receipt = engine.claim(2, "L1", "c", "force", REVIEW, "a")
        self.assertEqual(receipt["assignment"], 1)
        # 未 apply 前状态不变
        self.assertEqual(engine.state_json(), before)
        for event_type, payload in events:
            engine.apply_event(event_type, payload)
        self.assertNotEqual(engine.state_json(), before)
        self.assertEqual(engine.state.assignments[1].status, PENDING)


if __name__ == "__main__":
    unittest.main(verbosity=2)
