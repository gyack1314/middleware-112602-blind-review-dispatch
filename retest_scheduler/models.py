"""领域模型：机构、复测包、授出编号、仲裁案、出证。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

REVIEW = "review"
DECIDE = "decide"
ROLES = (REVIEW, DECIDE)

PASS = "pass"
RETURN = "return"
VERDICTS = (PASS, RETURN)

# 授出编号生命周期
PENDING = "pending"          # 已领取、未提交，占用份额
SUBMITTED = "submitted"      # 已提交有效结论，份额已释放
REVOKED = "revoked"          # 机构退出或运营撤销
EXPIRED = "expired"          # 截止未提交
MIGRATED = "migrated"        # 更正时迁移到新证据版本（仅 review 在途任务）
SUPERSEDED = "superseded"    # 项目出证后自动关闭的其余在途任务

# 仲裁案生命周期
CASE_OPEN = "open"
CASE_DECIDED = "decided"
CASE_ABANDONED = "abandoned"  # 更正后原版本争议不再参与当前出证


class EngineError(Exception):
    """业务规则拒绝。code 为稳定的英文错误码，中文消息用于人工解释。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass
class Lab:
    lab_id: str
    affinity: str
    skills: List[str]
    slots: int
    active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lab": self.lab_id,
            "affinity": self.affinity,
            "skills": list(self.skills),
            "slots": self.slots,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Lab":
        return cls(
            lab_id=data["lab"],
            affinity=data["affinity"],
            skills=list(data["skills"]),
            slots=data["slots"],
            active=data["active"],
        )


@dataclass
class Assignment:
    """一次真实授出的编号；被拒绝的申请不占编号。"""

    seq: int
    lab_id: str
    affinity: str
    case_id: str
    item: str
    role: str
    request_key: str
    granted_at: int
    evidence: int
    deadline: int
    status: str = PENDING
    verdict: Optional[str] = None
    submitted_at: Optional[int] = None
    submitted_evidence: Optional[int] = None
    refs: List[str] = field(default_factory=list)
    migrated_to: Optional[int] = None
    case_seq: Optional[int] = None
    blinded: Optional[Dict[str, str]] = None
    revoked_reason: Optional[str] = None

    @property
    def in_flight(self) -> bool:
        """是否仍占用机构份额。"""
        return self.status == PENDING

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "lab": self.lab_id,
            "affinity": self.affinity,
            "case": self.case_id,
            "item": self.item,
            "role": self.role,
            "request": self.request_key,
            "granted_at": self.granted_at,
            "evidence": self.evidence,
            "deadline": self.deadline,
            "status": self.status,
            "verdict": self.verdict,
            "submitted_at": self.submitted_at,
            "submitted_evidence": self.submitted_evidence,
            "refs": list(self.refs),
            "migrated_to": self.migrated_to,
            "case_seq": self.case_seq,
            "blinded": self.blinded,
            "revoked_reason": self.revoked_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Assignment":
        return cls(
            seq=data["seq"],
            lab_id=data["lab"],
            affinity=data["affinity"],
            case_id=data["case"],
            item=data["item"],
            role=data["role"],
            request_key=data["request"],
            granted_at=data["granted_at"],
            evidence=data["evidence"],
            deadline=data["deadline"],
            status=data["status"],
            verdict=data.get("verdict"),
            submitted_at=data.get("submitted_at"),
            submitted_evidence=data.get("submitted_evidence"),
            refs=list(data.get("refs", [])),
            migrated_to=data.get("migrated_to"),
            case_seq=data.get("case_seq"),
            blinded=data.get("blinded"),
            revoked_reason=data.get("revoked_reason"),
        )


@dataclass
class ArbitrationCase:
    """同一证据版本下两份有效独立结论不一致时产生的争议。"""

    seq: int
    case_id: str
    item: str
    evidence: int
    deadline: int
    created_at: int
    review_seqs: List[int]
    status: str = CASE_OPEN
    arbiter_lab: Optional[str] = None
    arbiter_assignment: Optional[int] = None
    decided_at: Optional[int] = None
    adopted_verdict: Optional[str] = None
    decided_evidence: Optional[int] = None
    refs: List[str] = field(default_factory=list)
    close_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "case": self.case_id,
            "item": self.item,
            "evidence": self.evidence,
            "deadline": self.deadline,
            "created_at": self.created_at,
            "review_seqs": list(self.review_seqs),
            "status": self.status,
            "arbiter_lab": self.arbiter_lab,
            "arbiter_assignment": self.arbiter_assignment,
            "decided_at": self.decided_at,
            "adopted_verdict": self.adopted_verdict,
            "decided_evidence": self.decided_evidence,
            "refs": list(self.refs),
            "close_reason": self.close_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArbitrationCase":
        return cls(
            seq=data["seq"],
            case_id=data["case"],
            item=data["item"],
            evidence=data["evidence"],
            deadline=data["deadline"],
            created_at=data["created_at"],
            review_seqs=list(data["review_seqs"]),
            status=data["status"],
            arbiter_lab=data.get("arbiter_lab"),
            arbiter_assignment=data.get("arbiter_assignment"),
            decided_at=data.get("decided_at"),
            adopted_verdict=data.get("adopted_verdict"),
            decided_evidence=data.get("decided_evidence"),
            refs=list(data.get("refs", [])),
            close_reason=data.get("close_reason"),
        )


@dataclass
class Certificate:
    """对某观察项目当前证据版本出具的结论。"""

    cert_seq: int
    case_id: str
    item: str
    evidence: int
    verdict: str
    created_at: int
    source: str                       # review 双签 或 arbitration
    decision_seqs: List[int]          # 出证引用的有效独立决定（授出编号）
    arbiter_case: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cert_seq": self.cert_seq,
            "case": self.case_id,
            "item": self.item,
            "evidence": self.evidence,
            "verdict": self.verdict,
            "created_at": self.created_at,
            "source": self.source,
            "decision_seqs": list(self.decision_seqs),
            "arbiter_case": self.arbiter_case,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Certificate":
        return cls(
            cert_seq=data["cert_seq"],
            case_id=data["case"],
            item=data["item"],
            evidence=data["evidence"],
            verdict=data["verdict"],
            created_at=data["created_at"],
            source=data["source"],
            decision_seqs=list(data["decision_seqs"]),
            arbiter_case=data.get("arbiter_case"),
        )
