"""Schemas for unstructured customer-voice sources: tickets, reviews and call transcripts."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ValidationInfo, field_validator, model_validator

from src.cleaning import standardize as std
from src.validation.base import RecordModel, check_range, id_parser, note, require, soft
from src.validation.schemas.catalog import foreign_key

TICKET_ID = id_parser(r"TKT-\d{6}")
REVIEW_ID = id_parser(r"REV-\d{6}")
CALL_ID = id_parser(r"CALL-\d{6}")

_CLOSED_STATUSES = {"resolved", "closed"}
_PRODUCT_CATEGORIES = {"product_issue", "product_inquiry", "return", "warranty", "technical_support"}
_SENTIMENT_POLARITY = {
    "positive": "positive", "satisfied": "positive", "relieved": "positive",
    "neutral": "neutral", "inquiry": "neutral",
    "negative": "negative", "dissatisfied": "negative", "frustrated": "negative",
    "concerned": "negative", "confused": "negative",
}


def _text(v: Any) -> str | None:
    return None if std.is_null(v) else std.normalize_text(v)


def _is_iso(value: Any) -> bool:
    return isinstance(value, str) and bool(re.match(r"^\d{4}-\d{2}-\d{2}", value.strip()))


class SupportTicket(RecordModel):
    ticket_id: str
    customer_id: str | None
    product_id: str | None
    channel: str | None
    category: str | None
    priority: str | None
    status: str | None
    created_at: datetime
    resolved_at: datetime | None
    agent_id: str | None
    satisfaction_score: int | None
    transcript: str | None
    summary: str | None
    tags: list[str]
    resolution_type: str | None

    @field_validator("ticket_id", mode="before")
    @classmethod
    def _id(cls, v: Any) -> str:
        return require("support_tickets.invalid_id", TICKET_ID, v)

    @field_validator("customer_id", "product_id", "agent_id", mode="before")
    @classmethod
    def _fk(cls, v: Any) -> str | None:
        return foreign_key(v)

    @field_validator("channel", "priority", "status", mode="before")
    @classmethod
    def _enum(cls, v: Any, info: ValidationInfo) -> str | None:
        token = std.normalize_token(v)
        if token is not None and str(v).strip() != token:
            note(info, "support_tickets.enum_case")
        return token

    @field_validator("category", "resolution_type", mode="before")
    @classmethod
    def _token(cls, v: Any) -> str | None:
        return std.normalize_token(v)

    @field_validator("created_at", mode="before")
    @classmethod
    def _created(cls, v: Any, info: ValidationInfo) -> datetime:
        parsed = require("support_tickets.invalid_created_at", std.parse_datetime, v)
        if not _is_iso(v):
            note(info, "support_tickets.date_format")
        return parsed

    @field_validator("resolved_at", mode="before")
    @classmethod
    def _resolved(cls, v: Any) -> datetime | None:
        return std.parse_datetime(v)

    @field_validator("satisfaction_score", mode="before")
    @classmethod
    def _satisfaction(cls, v: Any, info: ValidationInfo) -> int | None:
        score = soft(info, "support_tickets.invalid_satisfaction", std.parse_int, v)
        score = check_range(info, "support_tickets.invalid_satisfaction", score, low=1, high=5)
        return int(score) if score is not None else None

    @field_validator("transcript", "summary", mode="before")
    @classmethod
    def _long_text(cls, v: Any) -> str | None:
        # Keep line breaks in transcripts; only trim the ends.
        return None if std.is_null(v) else str(v).strip()

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v: Any) -> list[str]:
        return std.split_tags(v)

    @model_validator(mode="after")
    def _cross_field(self, info: ValidationInfo) -> SupportTicket:
        if self.status in _CLOSED_STATUSES:
            if self.resolved_at is None:
                note(info, "support_tickets.missing_resolved_at")
            if self.satisfaction_score is None:
                note(info, "support_tickets.missing_satisfaction")
        if self.resolved_at and self.resolved_at < self.created_at:
            note(info, "support_tickets.resolved_before_created")
        if self.category in _PRODUCT_CATEGORIES and self.product_id is None:
            note(info, "support_tickets.missing_product_ref")
        return self


class ProductReview(RecordModel):
    review_id: str
    product_id: str | None
    customer_id: str | None
    rating: int | None
    title: str | None
    review_text: str | None
    helpful_votes: int | None
    verified_purchase: bool | None
    review_date: date | None
    sentiment: str | None
    images: list[str]
    response_text: str | None
    responder: str | None
    response_date: date | None

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        response = data.get("response") or {}
        return {
            **data,
            "response_text": response.get("response_text"),
            "responder": response.get("responder"),
            "response_date": response.get("response_date"),
        }

    @field_validator("review_id", mode="before")
    @classmethod
    def _id(cls, v: Any) -> str:
        return require("product_reviews.invalid_id", REVIEW_ID, v)

    @field_validator("product_id", "customer_id", mode="before")
    @classmethod
    def _fk(cls, v: Any) -> str | None:
        return foreign_key(v)

    @field_validator("rating", mode="before")
    @classmethod
    def _rating(cls, v: Any, info: ValidationInfo) -> int | None:
        if std.is_null(v):
            note(info, "product_reviews.missing_rating")
            return None
        rating = soft(info, "product_reviews.invalid_rating", std.parse_int, v)
        rating = check_range(info, "product_reviews.invalid_rating", rating, low=1, high=5)
        return int(rating) if rating is not None else None

    @field_validator("title", "review_text", "responder", "response_text", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str | None:
        return _text(v)

    @field_validator("helpful_votes", mode="before")
    @classmethod
    def _votes(cls, v: Any) -> int | None:
        return std.parse_int(v)

    @field_validator("verified_purchase", mode="before")
    @classmethod
    def _verified(cls, v: Any) -> bool | None:
        return std.parse_bool(v)

    @field_validator("review_date", mode="before")
    @classmethod
    def _date(cls, v: Any, info: ValidationInfo) -> date | None:
        parsed = std.parse_date(v)
        if parsed is not None and not _is_iso(v):
            note(info, "product_reviews.date_format")
        return parsed

    @field_validator("response_date", mode="before")
    @classmethod
    def _response_date(cls, v: Any) -> date | None:
        return std.parse_date(v)

    @field_validator("sentiment", mode="before")
    @classmethod
    def _sentiment(cls, v: Any, info: ValidationInfo) -> str | None:
        token = std.normalize_token(v)
        if token is None:
            note(info, "product_reviews.missing_sentiment")
        return token

    @field_validator("images", mode="before")
    @classmethod
    def _images(cls, v: Any) -> list[str]:
        return [str(i) for i in v] if isinstance(v, list) else []


class Utterance(BaseModel):
    offset_seconds: int
    speaker: str
    text: str


class CallTranscript(RecordModel):
    call_id: str
    ticket_id: str | None
    customer_id: str | None
    agent_id: str | None
    agent_name: str | None
    call_start: datetime
    call_end: datetime | None
    duration_seconds: int | None
    call_type: str | None
    phone_number: str | None
    queue_wait_seconds: int | None
    hold_time_seconds: int | None
    transfers: int | None
    language: str | None
    sentiment_raw: str | None
    sentiment_start: str | None = None
    sentiment_end: str | None = None
    sentiment: str | None = None
    resolution_achieved: bool | None
    utterances: list[Utterance]
    keywords_detected: list[str]
    action_items: list[str]
    quality_score: int | None

    @model_validator(mode="before")
    @classmethod
    def _rename(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = {**data, "sentiment_raw": data.get("sentiment_overall")}
        return data

    @field_validator("call_id", mode="before")
    @classmethod
    def _id(cls, v: Any) -> str:
        return require("call_transcripts.invalid_id", CALL_ID, v)

    @field_validator("ticket_id", "customer_id", "agent_id", mode="before")
    @classmethod
    def _fk(cls, v: Any) -> str | None:
        return foreign_key(v)

    @field_validator("agent_name", "language", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str | None:
        return _text(v)

    @field_validator("call_start", mode="before")
    @classmethod
    def _start(cls, v: Any) -> datetime:
        return require("call_transcripts.invalid_start", std.parse_datetime, v)

    @field_validator("call_end", mode="before")
    @classmethod
    def _end(cls, v: Any) -> datetime | None:
        return std.parse_datetime(v)

    @field_validator("duration_seconds", "queue_wait_seconds", "hold_time_seconds", "transfers", mode="before")
    @classmethod
    def _int(cls, v: Any) -> int | None:
        return std.parse_int(v)

    @field_validator("call_type", "sentiment_raw", mode="before")
    @classmethod
    def _token(cls, v: Any) -> str | None:
        return std.normalize_token(v)

    @field_validator("phone_number", mode="before")
    @classmethod
    def _phone(cls, v: Any) -> str | None:
        try:
            return std.normalize_phone(v)
        except ValueError:
            return None

    @field_validator("resolution_achieved", mode="before")
    @classmethod
    def _bool(cls, v: Any) -> bool | None:
        return std.parse_bool(v)

    @field_validator("utterances", mode="before")
    @classmethod
    def _utterances(cls, v: Any) -> list[dict[str, Any]]:
        if not isinstance(v, list):
            return []
        parsed = []
        for item in v:
            if not isinstance(item, dict) or std.is_null(item.get("text")):
                continue
            offset = std.parse_hms(item.get("timestamp"))
            parsed.append({
                "offset_seconds": int(offset.total_seconds()) if offset is not None else -1,
                "speaker": std.normalize_token(item.get("speaker")) or "unknown",
                "text": str(item["text"]).strip(),
            })
        return parsed

    @field_validator("keywords_detected", "action_items", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return [str(i).strip() for i in v if not std.is_null(i)] if isinstance(v, list) else []

    @field_validator("quality_score", mode="before")
    @classmethod
    def _quality(cls, v: Any, info: ValidationInfo) -> int | None:
        score = check_range(info, "call_transcripts.invalid_quality_score", std.parse_int(v), low=0, high=100)
        return int(score) if score is not None else None

    @model_validator(mode="after")
    def _cross_field(self, info: ValidationInfo) -> CallTranscript:
        if self.ticket_id is None:
            note(info, "call_transcripts.missing_ticket_ref")
        if self.agent_id is None or self.agent_name is None:
            note(info, "call_transcripts.missing_agent")

        measured = int((self.call_end - self.call_start).total_seconds()) if self.call_end else None
        if measured is not None and measured >= 0 and self.duration_seconds != measured:
            self.duration_seconds = measured
            note(info, "call_transcripts.duration_recomputed")
        elif self.duration_seconds is not None and self.duration_seconds < 0:
            self.duration_seconds = None
            note(info, "call_transcripts.invalid_duration")

        if not self.utterances:
            note(info, "call_transcripts.missing_utterances")
        offsets = [u.offset_seconds for u in self.utterances]
        if offsets != sorted(offsets):
            self.utterances = sorted(self.utterances, key=lambda u: u.offset_seconds)
            note(info, "call_transcripts.utterances_sorted")

        if self.sentiment_raw:
            start, _, end = self.sentiment_raw.partition("_to_")
            end = end or start
            self.sentiment_start = _SENTIMENT_POLARITY.get(start)
            self.sentiment_end = _SENTIMENT_POLARITY.get(end)
            self.sentiment = self.sentiment_end
            if self.sentiment_raw != self.sentiment:
                note(info, "call_transcripts.sentiment")
        return self
