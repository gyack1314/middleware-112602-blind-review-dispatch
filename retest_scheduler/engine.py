"""调度引擎：全部业务规则都在本模块内。

命令方法只做校验并返回 (事件列表, 回执)，不直接修改状态；
由 middleware.RetestMiddleware 先把事件追加到日志，再 apply_event 生效，
从而保证“已占份额、盲审隔离”在进程异常退出后仍可完整重建。
"""

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    DECIDE,
    EXPIRED,
    MIGRATED,
    PENDING,
    REVIEW,
    REVOKED,
    SUBMITTED,
    SUPERSEDED,
    VERDICTS,
    ArbitrationCase,
    Assignment,
    Certificate,
    CASE_ABANDONED,
    CASE_DECIDED,
    CASE_OPEN,
    EngineError,
    Lab,
)


def _key(case_id: str, item: str) -> Tuple[str, str]:
    return case_id, item


@dataclass
class PacketState:
    """一个观察项目的全部证据版本与当前进度。"""

    case_id: str
    item: str
    skill: str
    versions: Dict[int, int] = field(default_factory=dict)   # evidence -> deadline
    order: List[int] = field(default_factory=list)           # 证据版本发布顺序
    current_evidence: Optional[int] = None
    # evidence -> 证书序号；出证后同一版本不再重复出证
    certificates: Dict[int, int] = field(default_factory=dict)

    @property
    def deadline(self) -> int:
        return self.versions[self.current_evidence]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case": self.case_id,
            "item": self.item,
            "skill": self.skill,
            "versions": dict(self.versions),
            "order": list(self.order),
            "current_evidence": self.current_evidence,
            "certificates": dict(self.certificates),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PacketState":
        return cls(
            case_id=data["case"],
            item=data["item"],
            skill=data["skill"],
            versions={int(k): v for k, v in data["versions"].items()},
            order=[int(v) for v in data["order"]],
            current_evidence=data["current_evidence"],
            certificates={int(k): v for k, v in data.get("certificates", {}).items()},
        )


@dataclass
class State:
    labs: Dict[str, Lab] = field(default_factory=dict)
    packets: Dict[Tuple[str, str], PacketState] = field(default_factory=dict)
    assignments: Dict[int, Assignment] = field(default_factory=dict)
    cases: Dict[int, ArbitrationCase] = field(default_factory=dict)
    certificates: Dict[int, Certificate] = field(default_factory=dict)
    next_assignment_seq: int = 1
    next_case_seq: int = 1
    next_cert_seq: int = 1
    restart_count: int = 0
    # request 键 -> 授出编号；同一申请重放/重连不得重复授出
    idempotency: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "labs": {k: v.to_dict() for k, v in self.labs.items()},
            "packets": [v.to_dict() for _, v in sorted(self.packets.items())],
            "assignments": {str(k): v.to_dict() for k, v in self.assignments.items()},
            "cases": {str(k): v.to_dict() for k, v in self.cases.items()},
            "certificates": {
                str(k): v.to_dict() for k, v in self.certificates.items()
            },
            "next_assignment_seq": self.next_assignment_seq,
            "next_case_seq": self.next_case_seq,
            "next_cert_seq": self.next_cert_seq,
            "restart_count": self.restart_count,
            "idempotency": dict(self.idempotency),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "State":
        state = cls()
        state.labs = {k: Lab.from_dict(v) for k, v in data.get("labs", {}).items()}
        state.packets = {}
        for raw in data.get("packets", []):
            packet = PacketState.from_dict(raw)
            state.packets[(packet.case_id, packet.item)] = packet
        state.assignments = {
            int(k): Assignment.from_dict(v)
            for k, v in data.get("assignments", {}).items()
        }
        state.cases = {
            int(k): ArbitrationCase.from_dict(v)
            for k, v in data.get("cases", {}).items()
        }
        state.certificates = {
            int(k): Certificate.from_dict(v)
            for k, v in data.get("certificates", {}).items()
        }
        state.next_assignment_seq = data.get("next_assignment_seq", 1)
        state.next_case_seq = data.get("next_case_seq", 1)
        state.next_cert_seq = data.get("next_cert_seq", 1)
        state.restart_count = data.get("restart_count", 0)
        state.idempotency = dict(data.get("idempotency", {}))
        return state


# 事件名集中登记，存储层据此校验日志
EVENT_TYPES = (
    "lab_registered",
    "packet_published",
    "assignment_granted",
    "assignment_migrated",
    "result_submitted",
    "assignment_closed",
    "case_opened",
    "case_attached",
    "case_decided",
    "case_abandoned",
    "certificate_issued",
    "restart_recorded",
)


class RetestEngine:
    """无外部依赖的纯内存领域引擎；命令返回事件，状态仅由 apply_event 改变。"""

    def __init__(self, state: Optional[State] = None):
        self.state = state or State()

    # ------------------------------------------------------------------
    # 事件回放：崩溃恢复时按日志重建，派生关系全部可重算
    # ------------------------------------------------------------------
    def apply_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        st = self.state
        if event_type == "lab_registered":
            st.labs[payload["lab"]] = Lab.from_dict(payload)
        elif event_type == "packet_published":
            key = (payload["case"], payload["item"])
            packet = st.packets.get(key)
            if packet is None:
                packet = PacketState(
                    case_id=payload["case"],
                    item=payload["item"],
                    skill=payload["skill"],
                )
                st.packets[key] = packet
            packet.versions[payload["evidence"]] = payload["deadline"]
            if payload["evidence"] not in packet.order:
                packet.order.append(payload["evidence"])
            packet.current_evidence = payload["evidence"]
        elif event_type in ("assignment_granted", "assignment_migrated"):
            assignment = Assignment.from_dict(payload["assignment"])
            st.assignments[assignment.seq] = assignment
            st.idempotency[assignment.request_key] = assignment.seq
            # 命令本身不推进计数器，序号在事件生效时 monotonic 推进
            st.next_assignment_seq = max(st.next_assignment_seq, assignment.seq + 1)
        elif event_type == "result_submitted":
            assignment = st.assignments[payload["seq"]]
            assignment.status = SUBMITTED
            assignment.verdict = payload["verdict"]
            assignment.submitted_at = payload["submitted_at"]
            assignment.submitted_evidence = payload["evidence"]
            assignment.refs = list(payload.get("refs", []))
        elif event_type == "assignment_closed":
            assignment = st.assignments[payload["seq"]]
            assignment.status = payload["status"]
            if payload.get("reason") is not None:
                assignment.revoked_reason = payload["reason"]
            if payload.get("migrated_to") is not None:
                assignment.migrated_to = payload["migrated_to"]
            # 仲裁者退出/截止：争议案回到可领取状态，等待新的独立第三方
            if (
                assignment.role == DECIDE
                and assignment.case_seq is not None
                and payload["status"] in (REVOKED, EXPIRED)
            ):
                case = st.cases[assignment.case_seq]
                if (
                    case.status == CASE_OPEN
                    and case.arbiter_assignment == assignment.seq
                ):
                    case.arbiter_lab = None
                    case.arbiter_assignment = None
        elif event_type == "case_opened":
            st.cases[payload["seq"]] = ArbitrationCase.from_dict(payload)
            st.next_case_seq = max(st.next_case_seq, payload["seq"] + 1)
        elif event_type == "case_attached":
            assignment = st.assignments[payload["assignment_seq"]]
            assignment.case_seq = payload["case_seq"]
            assignment.blinded = dict(payload["blinded"])
            case = st.cases[payload["case_seq"]]
            case.arbiter_lab = assignment.lab_id
            case.arbiter_assignment = assignment.seq
        elif event_type == "case_decided":
            case = st.cases[payload["case_seq"]]
            case.status = CASE_DECIDED
            case.decided_at = payload["decided_at"]
            case.adopted_verdict = payload["adopted_verdict"]
            case.decided_evidence = payload["evidence"]
            case.refs = list(payload.get("refs", []))
        elif event_type == "case_abandoned":
            case = st.cases[payload["case_seq"]]
            case.status = CASE_ABANDONED
            case.close_reason = payload["reason"]
        elif event_type == "certificate_issued":
            cert = Certificate.from_dict(payload)
            st.certificates[cert.cert_seq] = cert
            st.packets[(cert.case_id, cert.item)].certificates[cert.evidence] = (
                cert.cert_seq
            )
            st.next_cert_seq = max(st.next_cert_seq, cert.cert_seq + 1)
        elif event_type == "restart_recorded":
            st.restart_count += 1
        else:
            raise EngineError("unknown_event", f"未知事件：{event_type}")

    # ------------------------------------------------------------------
    # 基础查询与容量
    # ------------------------------------------------------------------
    def _lab(self, lab_id: str) -> Lab:
        lab = self.state.labs.get(lab_id)
        if lab is None:
            raise EngineError("unknown_lab", f"机构未登记：{lab_id}")
        return lab

    def _packet(self, case_id: str, item: str) -> PacketState:
        packet = self.state.packets.get(_key(case_id, item))
        if packet is None:
            raise EngineError("unknown_item", f"观察项目未发布：{case_id}/{item}")
        return packet

    def _assignment(self, seq: int) -> Assignment:
        assignment = self.state.assignments.get(seq)
        if assignment is None:
            raise EngineError("unknown_assignment", f"授出编号不存在：{seq}")
        return assignment

    def lab_usage(self, lab_id: str) -> int:
        return sum(
            1
            for a in self.state.assignments.values()
            if a.lab_id == lab_id and a.in_flight
        )

    def affinity_usage(self, affinity: str) -> int:
        return sum(
            1
            for a in self.state.assignments.values()
            if a.affinity == affinity and a.in_flight
        )

    def affinity_capacity(self, affinity: str) -> int:
        return sum(
            lab.slots for lab in self.state.labs.values() if lab.affinity == affinity
        )

    def _affinity_has_capacity(self, affinity: str) -> bool:
        return self.affinity_usage(affinity) < self.affinity_capacity(affinity)

    def _lab_has_capacity(self, lab: Lab) -> bool:
        return self.lab_usage(lab.lab_id) < lab.slots

    # ------------------------------------------------------------------
    # 机构登记 / 退出（返回事件，不改状态）
    # ------------------------------------------------------------------
    def register_lab(
        self,
        now: int,
        lab_id: str,
        affinity: str,
        skills: List[str],
        slots: int,
        active: bool,
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, Any]]:
        if not isinstance(lab_id, str) or not lab_id:
            raise EngineError("bad_lab", "lab 标识不能为空")
        if not isinstance(affinity, str) or not affinity:
            raise EngineError("bad_affinity", "affinity 不能为空")
        if not isinstance(skills, list) or not skills or not all(
            isinstance(s, str) and s for s in skills
        ):
            raise EngineError("bad_skills", "skills 必须是非空字符串数组")
        if type(slots) is not int or slots < 1:
            raise EngineError("bad_slots", "slots 必须是正整数")

        existing = self.state.labs.get(lab_id)
        if existing is not None:
            merged_skills = list(dict.fromkeys([*existing.skills, *skills]))
            new_lab = Lab(lab_id, affinity, merged_skills, slots, active)
            if active:
                if self.lab_usage(lab_id) > slots:
                    raise EngineError(
                        "capacity_shrink_blocked",
                        f"机构 {lab_id} 仍有 {self.lab_usage(lab_id)} 起在途任务，"
                        f"不能把份额降为 {slots}",
                    )
                projected_group = self.affinity_capacity(affinity)
                if existing.affinity == affinity:
                    projected_group += slots - existing.slots
                if self.affinity_usage(affinity) > projected_group:
                    raise EngineError(
                        "group_capacity_exceeded",
                        f"关联组 {affinity} 的共享份额 {projected_group} "
                        f"不足以承接在途占用 {self.affinity_usage(affinity)}",
                    )
        else:
            new_lab = Lab(lab_id, affinity, list(skills), slots, active)

        events: List[Tuple[str, Dict[str, Any]]] = [
            ("lab_registered", new_lab.to_dict())
        ]
        # 退出语义：active=false 时撤回该机构全部在途任务，已提交结果保留
        if not active and (existing is None or existing.active):
            for assignment in list(self.state.assignments.values()):
                if assignment.lab_id == lab_id and assignment.in_flight:
                    events.append(
                        (
                            "assignment_closed",
                            {
                                "seq": assignment.seq,
                                "status": REVOKED,
                                "reason": f"机构 {lab_id} 退出，撤回在途任务",
                                "at": now,
                            },
                        )
                    )
        receipt = {
            "kind": "lab",
            "lab": lab_id,
            "affinity": affinity,
            "skills": new_lab.skills,
            "slots": slots,
            "active": active,
            "revoked_assignments": [
                payload["seq"] for _, payload in events if payload.get("status") == REVOKED
            ],
        }
        return events, receipt

    # ------------------------------------------------------------------
    # 复测包发布与证据更正
    # ------------------------------------------------------------------
    def publish_packet(
        self,
        now: int,
        case_id: str,
        item: str,
        skill: str,
        evidence: int,
        deadline: int,
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, Any]]:
        if type(evidence) is not int or evidence < 1:
            raise EngineError("bad_evidence", "evidence 必须是正整数版本号")
        if type(deadline) is not int or deadline < now:
            raise EngineError("bad_deadline", "deadline 必须是不早于当前时刻的整数秒点")

        key = _key(case_id, item)
        packet = self.state.packets.get(key)
        if packet is not None:
            if skill != packet.skill:
                raise EngineError("skill_changed", "证据更正不得改变观察项目所需技能")
            if evidence <= packet.current_evidence:
                raise EngineError(
                    "evidence_not_newer",
                    f"证据版本必须高于当前版本 {packet.current_evidence}",
                )

        event = (
            "packet_published",
            {
                "case": case_id,
                "item": item,
                "skill": skill,
                "evidence": evidence,
                "deadline": deadline,
                "published_at": now,
            },
        )
        receipt = {
            "kind": "packet",
            "case": case_id,
            "item": item,
            "skill": skill,
            "evidence": evidence,
            "deadline": deadline,
            "revision": packet is not None,
        }
        return [event], receipt

    def revise_evidence(
        self, now: int, case_id: str, item: str, evidence: int, deadline: int
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, Any]]:
        """证据更正：发布新版本，迁移未提交 review，旧结果只归旧版本，无关项目不动。"""
        packet = self._packet(case_id, item)
        old_evidence = packet.current_evidence
        events, receipt = self.publish_packet(
            now, case_id, item, packet.skill, evidence, deadline
        )

        # 在途仲裁随原包被更正而归档：仲裁者不能再就旧案决定，旧争议只归旧版本
        for case in list(self.state.cases.values()):
            if (
                case.case_id == case_id
                and case.item == item
                and case.status == CASE_OPEN
            ):
                events.append(
                    (
                        "case_abandoned",
                        {
                            "case_seq": case.seq,
                            "reason": f"证据更正至版本 {evidence}，"
                            f"原版本 {case.evidence} 争议归档",
                            "at": now,
                        },
                    )
                )
                if case.arbiter_assignment is not None:
                    arbiter_task = self.state.assignments[case.arbiter_assignment]
                    if arbiter_task.in_flight:
                        events.append(
                            (
                                "assignment_closed",
                                {
                                    "seq": arbiter_task.seq,
                                    "status": REVOKED,
                                    "reason": f"证据更正至版本 {evidence}，原仲裁任务关闭",
                                    "at": now,
                                },
                            )
                        )

        # 未提交 review 迁移新版本（份额不增不减）；decide 固定原版本，不迁移
        next_seq = self.state.next_assignment_seq
        for assignment in list(self.state.assignments.values()):
            if (
                assignment.case_id == case_id
                and assignment.item == item
                and assignment.role == REVIEW
                and assignment.status == PENDING
                and assignment.evidence == old_evidence
            ):
                migrated = Assignment(
                    seq=next_seq,
                    lab_id=assignment.lab_id,
                    affinity=assignment.affinity,
                    case_id=case_id,
                    item=item,
                    role=REVIEW,
                    request_key=assignment.request_key,
                    granted_at=now,
                    evidence=evidence,
                    deadline=deadline,
                )
                events.append(
                    ("assignment_migrated", {"assignment": migrated.to_dict()})
                )
                events.append(
                    (
                        "assignment_closed",
                        {
                            "seq": assignment.seq,
                            "status": MIGRATED,
                            "reason": f"证据更正至版本 {evidence}，未提交任务迁移",
                            "migrated_to": next_seq,
                            "at": now,
                        },
                    )
                )
                next_seq += 1

        receipt["migrated_assignments"] = [
            payload["assignment"]["seq"]
            for type_, payload in events
            if type_ == "assignment_migrated"
        ]
        receipt["abandoned_cases"] = [
            payload["case_seq"]
            for type_, payload in events
            if type_ == "case_abandoned"
        ]
        return events, receipt

    # ------------------------------------------------------------------
    # 领取（盲派单核心）
    # ------------------------------------------------------------------
    def _current_review_results(self, packet: PacketState) -> List[Assignment]:
        return [
            a
            for a in self.state.assignments.values()
            if a.case_id == packet.case_id
            and a.item == packet.item
            and a.role == REVIEW
            and a.status == SUBMITTED
            and a.submitted_evidence == packet.current_evidence
        ]

    def _in_flight_review_groups(self, packet: PacketState) -> set:
        return {
            a.affinity
            for a in self.state.assignments.values()
            if a.case_id == packet.case_id
            and a.item == packet.item
            and a.role == REVIEW
            and a.in_flight
            and a.evidence == packet.current_evidence
        }

    def _open_review_slots(self, packet: PacketState) -> int:
        valid_groups = {a.affinity for a in self._current_review_results(packet)}
        return max(0, 2 - len(valid_groups | self._in_flight_review_groups(packet)))

    def claim(
        self,
        now: int,
        lab_id: str,
        case_id: str,
        item: str,
        role: str,
        request_key: str,
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, Any]]:
        if role not in (REVIEW, DECIDE):
            raise EngineError("bad_role", f"role 只能是 {REVIEW} 或 {DECIDE}")
        if not isinstance(request_key, str) or not request_key:
            raise EngineError("bad_request", "request 领取键不能为空")
        # 同一申请键重放：直接回原授出编号，不二次占份额（崩溃恢复语义）
        if request_key in self.state.idempotency:
            old = self.state.assignments[self.state.idempotency[request_key]]
            return [], {
                "kind": "claim",
                "request": request_key,
                "assignment": old.seq,
                "lab": old.lab_id,
                "case": old.case_id,
                "item": old.item,
                "role": old.role,
                "evidence": old.evidence,
                "deadline": old.deadline,
                "status": old.status,
                "duplicate": True,
            }

        lab = self._lab(lab_id)
        if not lab.active:
            raise EngineError("lab_inactive", f"机构 {lab_id} 已退出，不能领取新任务")
        packet = self._packet(case_id, item)
        if role == REVIEW:
            return self._claim_review(now, lab, packet, request_key)
        return self._claim_decide(now, lab, packet, request_key)

    def _claim_review(
        self, now: int, lab: Lab, packet: PacketState, request_key: str
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, Any]]:
        if now > packet.deadline:
            raise EngineError(
                "deadline_passed",
                f"复测已截止（{packet.deadline}），当前时刻 {now}",
            )
        if packet.skill not in lab.skills:
            raise EngineError(
                "skill_unsupported",
                f"机构 {lab.lab_id} 声明能力 {lab.skills}，缺项目所需 {packet.skill}",
            )
        if not self._lab_has_capacity(lab):
            raise EngineError(
                "lab_slots_full",
                f"机构 {lab.lab_id} 的 {lab.slots} 个份额已被在途任务占满",
            )
        if not self._affinity_has_capacity(lab.affinity):
            raise EngineError(
                "affinity_slots_full",
                f"关联组 {lab.affinity} 的共享份额 "
                f"{self.affinity_capacity(lab.affinity)} 已占满",
            )
        valid_groups = {a.affinity for a in self._current_review_results(packet)}
        if lab.affinity in valid_groups:
            raise EngineError(
                "affinity_conflict",
                f"关联组 {lab.affinity} 已就证据版本 {packet.current_evidence} "
                "提交有效结论，同一机构组的不同工号不构成独立复测",
            )
        if lab.affinity in self._in_flight_review_groups(packet):
            raise EngineError(
                "affinity_conflict",
                f"关联组 {lab.affinity} 已有在途领取，同组工号不能重复占独立席位",
            )
        if self._open_review_slots(packet) <= 0:
            raise EngineError("slots_filled", "该项目当前版本的独立席位已凑齐")

        seq = self.state.next_assignment_seq
        assignment = Assignment(
            seq=seq,
            lab_id=lab.lab_id,
            affinity=lab.affinity,
            case_id=packet.case_id,
            item=packet.item,
            role=REVIEW,
            request_key=request_key,
            granted_at=now,
            evidence=packet.current_evidence,
            deadline=packet.deadline,
        )
        event = ("assignment_granted", {"assignment": assignment.to_dict()})
        receipt = {
            "kind": "claim",
            "request": request_key,
            "assignment": seq,
            "lab": lab.lab_id,
            "case": packet.case_id,
            "item": packet.item,
            "role": REVIEW,
            "evidence": assignment.evidence,
            "deadline": assignment.deadline,
            "status": PENDING,
            "duplicate": False,
        }
        return [event], receipt

    def _open_decide_case(self, packet: PacketState) -> Optional[ArbitrationCase]:
        for case in self.state.cases.values():
            if (
                case.case_id == packet.case_id
                and case.item == packet.item
                and case.evidence == packet.current_evidence
                and case.status == CASE_OPEN
            ):
                return case
        return None

    def _claim_decide(
        self, now: int, lab: Lab, packet: PacketState, request_key: str
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, Any]]:
        case = self._open_decide_case(packet)
        if case is None:
            raise EngineError(
                "no_open_disagreement",
                "当前证据版本没有两份有效但相互分歧的结论，无需仲裁领取",
            )
        if case.arbiter_assignment is not None:
            current_arbiter = self.state.assignments[case.arbiter_assignment]
            if current_arbiter.in_flight:
                raise EngineError(
                    "arbiter_seat_taken",
                    f"该争议已有在途仲裁机构 {current_arbiter.lab_id}；"
                    "其退出或截止后才能重新分派",
                )
        if now > case.deadline:
            raise EngineError(
                "deadline_passed",
                f"仲裁任务已截止（{case.deadline}），当前时刻 {now}",
            )
        if packet.skill not in lab.skills:
            raise EngineError(
                "skill_unsupported",
                f"机构 {lab.lab_id} 缺少仲裁所需能力 {packet.skill}",
            )
        party_affinities = {
            self.state.assignments[seq].affinity for seq in case.review_seqs
        }
        if lab.affinity in party_affinities:
            raise EngineError(
                "arbiter_not_independent",
                f"仲裁者关联组 {lab.affinity} 与争议方同组，必须独立于两家复测机构",
            )
        if not self._lab_has_capacity(lab):
            raise EngineError(
                "lab_slots_full", f"机构 {lab.lab_id} 的份额已被在途任务占满"
            )
        if not self._affinity_has_capacity(lab.affinity):
            raise EngineError(
                "affinity_slots_full", f"关联组 {lab.affinity} 的共享份额已占满"
            )

        seq = self.state.next_assignment_seq
        # 盲审：只交付匿名当事方代号，不出现机构身份
        blinded = {
            "party_alpha": {
                "verdict": self.state.assignments[case.review_seqs[0]].verdict
            },
            "party_beta": {
                "verdict": self.state.assignments[case.review_seqs[1]].verdict
            },
        }
        assignment = Assignment(
            seq=seq,
            lab_id=lab.lab_id,
            affinity=lab.affinity,
            case_id=packet.case_id,
            item=packet.item,
            role=DECIDE,
            request_key=request_key,
            granted_at=now,
            evidence=case.evidence,
            deadline=case.deadline,
            case_seq=case.seq,
            blinded=blinded,
        )
        events = [
            ("assignment_granted", {"assignment": assignment.to_dict()}),
            (
                "case_attached",
                {
                    "case_seq": case.seq,
                    "assignment_seq": seq,
                    "blinded": blinded,
                    "at": now,
                },
            ),
        ]
        receipt = {
            "kind": "claim",
            "request": request_key,
            "assignment": seq,
            "lab": lab.lab_id,
            "case": packet.case_id,
            "item": packet.item,
            "role": DECIDE,
            "evidence": case.evidence,
            "deadline": case.deadline,
            "status": PENDING,
            "duplicate": False,
            "arbitration_case": case.seq,
            "blind_packet": {
                "case": packet.case_id,
                "item": packet.item,
                "skill": packet.skill,
                "evidence": case.evidence,
                "disagreement": {
                    "party_alpha": blinded["party_alpha"]["verdict"],
                    "party_beta": blinded["party_beta"]["verdict"],
                },
                "note": "当事机构身份对仲裁端不可见，仅按匿名代号交付同一证据与分歧",
            },
        }
        return events, receipt

    # ------------------------------------------------------------------
    # 提交结论
    # ------------------------------------------------------------------
    def submit_result(
        self,
        now: int,
        lab_id: str,
        seq: int,
        evidence: int,
        verdict: str,
        refs: Optional[List[str]] = None,
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, Any]]:
        lab = self._lab(lab_id)
        assignment = self._assignment(seq)
        if assignment.lab_id != lab_id:
            raise EngineError(
                "assignment_owner_mismatch",
                f"授出编号 {seq} 属于机构 {assignment.lab_id}，{lab_id} 无权提交",
            )
        if assignment.status != PENDING:
            raise EngineError(
                "assignment_not_open",
                f"授出编号 {seq} 当前状态为 {assignment.status}，不能重复提交",
            )
        if verdict not in VERDICTS:
            raise EngineError("bad_verdict", f"verdict 只能是 pass 或 return")
        if type(evidence) is not int or evidence < 1:
            raise EngineError("bad_evidence", "evidence 必须是正整数版本号")
        if now > assignment.deadline:
            raise EngineError(
                "late_submission",
                f"已超过截止时刻 {assignment.deadline}，旧结果晚报不能冒充替补",
            )
        if evidence != assignment.evidence:
            raise EngineError(
                "evidence_mismatch",
                f"该任务绑定证据版本 {assignment.evidence}，收到的是 {evidence}；"
                "更正前的任务不能就新版本出证",
            )

        events: List[Tuple[str, Dict[str, Any]]] = [
            (
                "result_submitted",
                {
                    "seq": seq,
                    "verdict": verdict,
                    "submitted_at": now,
                    "evidence": evidence,
                    "refs": list(refs or []),
                },
            )
        ]
        if assignment.role == REVIEW:
            events.extend(self._after_review_submit(now, assignment, verdict))
        else:
            events.extend(self._after_decide_submit(now, assignment, verdict, refs or []))

        receipt = {
            "kind": "submit",
            "lab": lab_id,
            "assignment": seq,
            "role": assignment.role,
            "verdict": verdict,
            "evidence": evidence,
        }
        for type_, payload in events:
            if type_ == "certificate_issued":
                receipt["certificate_issued"] = payload["cert_seq"]
            elif type_ == "case_opened":
                receipt["disagreement_case_opened"] = payload["seq"]
        return events, receipt

    def _after_review_submit(
        self, now: int, trigger: Assignment, verdict: str
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """基于“提交后”的假设视图计算派生事件，不触碰真实状态。"""
        packet = self._packet(trigger.case_id, trigger.item)
        existing = self._current_review_results(packet)
        # 假设本次提交生效后的有效结果集合（仅副本，不改真实状态）
        assumed = replace(
            trigger,
            status=SUBMITTED,
            verdict=verdict,
            submitted_evidence=trigger.evidence,
        )
        hypothetical = [*existing, assumed]
        affinities = {a.affinity for a in hypothetical}
        if len(hypothetical) < 2 or len(affinities) < 2:
            return []
        verdicts = {a.verdict for a in hypothetical}
        # 同一版本只允许一个未决争议案
        if self._open_decide_case(packet) is not None:
            return []
        if len(verdicts) == 1:
            chosen = sorted(hypothetical, key=lambda a: a.seq)[:2]
            cert_seq = self.state.next_cert_seq
            cert = Certificate(
                cert_seq=cert_seq,
                case_id=packet.case_id,
                item=packet.item,
                evidence=packet.current_evidence,
                verdict=chosen[0].verdict,
                created_at=now,
                source="review",
                decision_seqs=[a.seq for a in chosen],
            )
            events = [("certificate_issued", cert.to_dict())]
            events.extend(self._supersede_events(now, packet))
            return events
        # 结论分歧：开仲裁案，等待独立第三方 decide
        ordered = sorted(hypothetical, key=lambda a: a.seq)
        case_seq = self.state.next_case_seq
        case = ArbitrationCase(
            seq=case_seq,
            case_id=packet.case_id,
            item=packet.item,
            evidence=packet.current_evidence,
            deadline=packet.deadline,
            created_at=now,
            review_seqs=[ordered[0].seq, ordered[1].seq],
        )
        return [("case_opened", case.to_dict())]

    def _supersede_events(self, now: int, packet: PacketState) -> List[Tuple[str, Dict[str, Any]]]:
        return [
            (
                "assignment_closed",
                {
                    "seq": a.seq,
                    "status": SUPERSEDED,
                    "reason": "项目当前版本已出证，关闭多余在途席位",
                    "at": now,
                },
            )
            for a in list(self.state.assignments.values())
            if a.case_id == packet.case_id
            and a.item == packet.item
            and a.in_flight
            and a.evidence == packet.current_evidence
        ]

    def _after_decide_submit(
        self,
        now: int,
        arbiter_task: Assignment,
        verdict: str,
        refs: List[str],
    ) -> List[Tuple[str, Dict[str, Any]]]:
        case = self.state.cases[arbiter_task.case_seq]
        packet = self._packet(case.case_id, case.item)
        events = [
            (
                "case_decided",
                {
                    "case_seq": case.seq,
                    "decided_at": now,
                    "adopted_verdict": verdict,
                    "evidence": arbiter_task.evidence,
                    "refs": list(refs),
                },
            )
        ]
        # 采纳结论必须引用观察项目与确切证据版本；原案版本须仍是当前版本
        if (
            case.evidence == packet.current_evidence
            and packet.current_evidence not in packet.certificates
        ):
            cert_seq = self.state.next_cert_seq
            cert = Certificate(
                cert_seq=cert_seq,
                case_id=case.case_id,
                item=case.item,
                evidence=case.evidence,
                verdict=verdict,
                created_at=now,
                source="arbitration",
                decision_seqs=[*case.review_seqs, arbiter_task.seq],
                arbiter_case=case.seq,
            )
            events.append(("certificate_issued", cert.to_dict()))
            events.extend(self._supersede_events(now, packet))
        return events

    # ------------------------------------------------------------------
    # 撤销、截止清扫（同样只产出事件）
    # ------------------------------------------------------------------
    def revoke_assignment(
        self, now: int, seq: int, reason: str
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, Any]]:
        assignment = self._assignment(seq)
        if not assignment.in_flight:
            raise EngineError(
                "assignment_not_open",
                f"授出编号 {seq} 状态为 {assignment.status}，无需撤销",
            )
        events = [
            (
                "assignment_closed",
                {
                    "seq": seq,
                    "status": REVOKED,
                    "reason": reason or "运营撤销未完成任务",
                    "at": now,
                },
            )
        ]
        return events, {"kind": "revoke", "assignment": seq, "status": REVOKED}

    def sweep(
        self, now: int
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[Dict[str, Any]]]:
        """把所有已截止在途任务标记过期，释放份额、开放替补。"""
        events: List[Tuple[str, Dict[str, Any]]] = []
        expired = []
        for assignment in list(self.state.assignments.values()):
            if assignment.in_flight and now > assignment.deadline:
                events.append(
                    (
                        "assignment_closed",
                        {
                            "seq": assignment.seq,
                            "status": EXPIRED,
                            "reason": f"截止 {assignment.deadline} 未提交",
                            "at": now,
                        },
                    )
                )
                expired.append(
                    {
                        "assignment": assignment.seq,
                        "case": assignment.case_id,
                        "item": assignment.item,
                        "role": assignment.role,
                    }
                )
        return events, expired

    def record_restart(
        self, now: int
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, Any]]:
        return [("restart_recorded", {"at": now})], {
            "kind": "restart",
            "restart_count_after": self.state.restart_count + 1,
            "assignments_preserved": len(self.state.assignments),
            "in_flight_preserved": sum(
                1 for a in self.state.assignments.values() if a.in_flight
            ),
        }

    # ------------------------------------------------------------------
    # 查询视图（只读；inspect 前的清扫通过返回事件交给调用方落盘）
    # ------------------------------------------------------------------
    def pending_sweep_events(self, now: int) -> List[Tuple[str, Dict[str, Any]]]:
        events, _ = self.sweep(now)
        return events

    def inspect_lab(self, lab_id: str) -> Dict[str, Any]:
        lab = self._lab(lab_id)
        mine = sorted(
            (a for a in self.state.assignments.values() if a.lab_id == lab_id),
            key=lambda a: a.seq,
        )
        tasks = []
        for a in mine:
            view = {
                "assignment": a.seq,
                "case": a.case_id,
                "item": a.item,
                "role": a.role,
                "status": a.status,
                "evidence": a.evidence,
                "deadline": a.deadline,
                "verdict": a.verdict,
                "submitted_evidence": a.submitted_evidence,
            }
            if a.role == DECIDE and a.blinded is not None:
                # 盲审视图：只有匿名代号，永远不含其他机构身份
                view["blind_packet"] = {
                    "evidence": a.evidence,
                    "party_alpha": a.blinded["party_alpha"]["verdict"],
                    "party_beta": a.blinded["party_beta"]["verdict"],
                }
            tasks.append(view)
        return {
            "kind": "inspect",
            "scope": "lab",
            "lab": lab_id,
            "affinity": lab.affinity,
            "active": lab.active,
            "skills": list(lab.skills),
            "slots": lab.slots,
            "lab_usage": self.lab_usage(lab_id),
            "affinity_usage": self.affinity_usage(lab.affinity),
            "affinity_capacity": self.affinity_capacity(lab.affinity),
            "tasks": tasks,
            "visible_note": "仅返回本机构接到的包与要求，不含其他机构的身份或决定",
        }

    def _packet_view(self, packet: PacketState) -> Dict[str, Any]:
        evidence = packet.current_evidence
        current = sorted(
            (
                a
                for a in self.state.assignments.values()
                if a.case_id == packet.case_id
                and a.item == packet.item
                and (
                    (a.role == REVIEW and a.evidence == evidence)
                    or (a.role == DECIDE and a.case_seq is not None)
                )
            ),
            key=lambda a: a.seq,
        )
        history_versions = []
        for ver in packet.order:
            ver_results = [
                a
                for a in self.state.assignments.values()
                if a.case_id == packet.case_id
                and a.item == packet.item
                and a.role == REVIEW
                and a.status == SUBMITTED
                and a.submitted_evidence == ver
            ]
            history_versions.append(
                {
                    "evidence": ver,
                    "deadline": packet.versions[ver],
                    "decisions": [
                        {
                            "assignment": a.seq,
                            "lab": a.lab_id,
                            "affinity": a.affinity,
                            "verdict": a.verdict,
                            "submitted_at": a.submitted_at,
                            "refs": a.refs,
                        }
                        for a in sorted(ver_results, key=lambda x: x.seq)
                    ],
                    "certificate": packet.certificates.get(ver),
                }
            )
        open_cases = [
            {
                "case_seq": c.seq,
                "evidence": c.evidence,
                "review_seqs": list(c.review_seqs),
                "status": c.status,
                "arbiter_lab": c.arbiter_lab,
                "arbiter_assignment": c.arbiter_assignment,
            }
            for c in self.state.cases.values()
            if c.case_id == packet.case_id and c.item == packet.item
        ]
        cert_seq = packet.certificates.get(evidence)
        cert = self.state.certificates.get(cert_seq) if cert_seq else None
        return {
            "case": packet.case_id,
            "item": packet.item,
            "skill": packet.skill,
            "current_evidence": evidence,
            "deadline": packet.deadline,
            "certified": cert is not None,
            "certificate": cert.to_dict() if cert else None,
            "open_review_slots": self._open_review_slots(packet),
            "assignments": [a.to_dict() for a in current],
            "arbitration_cases": sorted(open_cases, key=lambda c: c["case_seq"]),
            "version_history": history_versions,
            "blockers": self._blockers(packet),
        }

    def _blockers(self, packet: PacketState) -> List[Dict[str, Any]]:
        """凑不齐两个独立群体时，明确区分欠能力、关联冲突与份额占满。"""
        if packet.current_evidence in packet.certificates:
            return []
        occupied = set()
        for a in self.state.assignments.values():
            if (
                a.case_id == packet.case_id
                and a.item == packet.item
                and a.role == REVIEW
                and (
                    (a.status == SUBMITTED and a.submitted_evidence == packet.current_evidence)
                    or (a.in_flight and a.evidence == packet.current_evidence)
                )
            ):
                occupied.add(a.affinity)
        candidates = [
            lab for lab in self.state.labs.values() if lab.affinity not in occupied
        ]
        blockers: List[Dict[str, Any]] = []
        if not candidates:
            blockers.append(
                {
                    "type": "no_independent_group",
                    "message": "已登记机构全部属于已占用关联组，缺少新的独立群体",
                }
            )
            return blockers
        for lab in sorted(candidates, key=lambda x: x.lab_id):
            reasons = []
            if not lab.active:
                reasons.append(("lab_inactive", "机构已退出"))
            if packet.skill not in lab.skills:
                reasons.append(("skill_unsupported", "欠能力：未声明所需技能"))
            if lab.active and self.lab_usage(lab.lab_id) >= lab.slots:
                reasons.append(("lab_slots_full", "机构份额已占满"))
            if lab.active and not self._affinity_has_capacity(lab.affinity):
                reasons.append(("affinity_slots_full", "关联组共享份额已占满"))
            if reasons:
                blockers.append(
                    {
                        "type": "candidate_blocked",
                        "lab": lab.lab_id,
                        "affinity": lab.affinity,
                        "reasons": [
                            {"code": code, "message": message}
                            for code, message in reasons
                        ],
                    }
                )
        viable = any(
            lab.active
            and packet.skill in lab.skills
            and self._lab_has_capacity(lab)
            and self._affinity_has_capacity(lab.affinity)
            for lab in candidates
        )
        if not viable and not blockers:
            blockers.append(
                {"type": "no_independent_group", "message": "没有可领取的独立机构"}
            )
        return blockers

    def inspect_case(self, case_id: str) -> Dict[str, Any]:
        packets = sorted(
            (p for p in self.state.packets.values() if p.case_id == case_id),
            key=lambda p: p.item,
        )
        if not packets:
            raise EngineError("unknown_case", f"未找到 case：{case_id}")
        items = [self._packet_view(p) for p in packets]
        waiting = [
            {"case": v["case"], "item": v["item"], "open_review_slots": v["open_review_slots"]}
            for v in items
            if not v["certified"]
        ]
        return {
            "kind": "inspect",
            "scope": "case",
            "case": case_id,
            "items": items,
            "waiting": waiting,
        }

    def inspect_overview(self) -> Dict[str, Any]:
        packets = sorted(
            self.state.packets.values(), key=lambda p: (p.case_id, p.item)
        )
        items = [self._packet_view(p) for p in packets]
        waiting = [
            {"case": v["case"], "item": v["item"], "open_review_slots": v["open_review_slots"]}
            for v in items
            if not v["certified"]
        ]
        return {
            "kind": "inspect",
            "scope": "overview",
            "items": items,
            "waiting": waiting,
            "certificates": [c.to_dict() for c in self.state.certificates.values()],
            "labs": [
                lab.to_dict()
                for lab in sorted(self.state.labs.values(), key=lambda x: x.lab_id)
            ],
        }

    def trace_certificate(self, cert_seq: int) -> Dict[str, Any]:
        """委托方审计：某次出证用了哪几项有效独立决定。"""
        cert = self.state.certificates.get(cert_seq)
        if cert is None:
            raise EngineError("unknown_certificate", f"证书不存在：{cert_seq}")
        decisions = []
        for seq in cert.decision_seqs:
            a = self.state.assignments[seq]
            decisions.append(
                {
                    "assignment": a.seq,
                    "lab": a.lab_id,
                    "affinity": a.affinity,
                    "role": a.role,
                    "verdict": a.verdict,
                    "evidence": a.submitted_evidence,
                    "submitted_at": a.submitted_at,
                    "refs": a.refs,
                }
            )
        affinities = {d["affinity"] for d in decisions if d["role"] == REVIEW}
        return {
            "kind": "cert_trace",
            "certificate": cert.to_dict(),
            "decisions": decisions,
            "independent_affinity_groups": sorted(affinities),
            "independence_ok": len(affinities) >= 2,
        }

    def dump_state(self) -> Dict[str, Any]:
        return self.state.to_dict()

    def state_json(self) -> str:
        import json

        return json.dumps(self.state.to_dict(), ensure_ascii=False, sort_keys=True)
