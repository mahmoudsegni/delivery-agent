"""MemberAbsence model for dated time off."""

from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin


class MemberAbsence(Base, BaseMixin):
    """A dated absence (inclusive range) that prorates a member's sprint capacity.

    Planning math only — standup rounds and the binary ``is_available`` flag are
    unaffected; see the TeamMember docstring.
    """

    __tablename__ = "member_absences"
    __table_args__ = (CheckConstraint("starts_on <= ends_on", name="ck_absence_dates"),)

    member_id: Mapped[str] = mapped_column(
        ForeignKey("team_members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
