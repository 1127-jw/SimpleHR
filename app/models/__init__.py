from .candidate import CandidateModel, CandidateAIScoreModel, ResumeModel
from .candidate_status_history import (
    NEXT_ID_ARCHIVED,
    NEXT_ID_SETTLED,
    CandidateStatusHistoryModel,
)
from .position import PositionModel
from .InterviewModel import InterviewModel
from .user import UserModel, DepartmentModel, DingdingUserModel
from .mail_submission import MailResumeSubmissionModel, MailSubmissionStatusEnum
from .mail_review import MailReviewModel, MailReviewStatusEnum


__all__ = [
    "DingdingUserModel",
    "DepartmentModel",
    "UserModel",
    "InterviewModel",
    "PositionModel",
    "CandidateAIScoreModel",
    "CandidateModel",
    "ResumeModel",
    "MailResumeSubmissionModel",
    "MailSubmissionStatusEnum",
    "MailReviewModel",
    "MailReviewStatusEnum",
    "CandidateStatusHistoryModel",
    "NEXT_ID_ARCHIVED",
    "NEXT_ID_SETTLED",
]