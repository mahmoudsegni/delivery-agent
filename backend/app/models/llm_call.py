"""LLMCall model — one row per Groq call for the usage ledger."""

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin


class LLMCall(Base, BaseMixin):
    """A single Groq completion: tokens, latency, and the feature that spent them."""

    __tablename__ = "llm_calls"

    feature: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
