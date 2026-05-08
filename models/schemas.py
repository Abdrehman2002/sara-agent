from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field
import uuid


ComplaintType = Literal[
    "bus_delay", "staff_behavior", "ticket_issue", "refund", "luggage", "other"
]

TicketStatus = Literal["Confirmed", "Delayed", "Cancelled", "Completed"]


class Complaint(BaseModel):
    customer_name: str
    phone: str
    complaint_type: ComplaintType
    description: str
    call_id: str
    date: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))


class Ticket(BaseModel):
    """CRM response after creating a complaint."""
    id: str = Field(default_factory=lambda: f"TKT-{uuid.uuid4().hex[:6].upper()}")
    status: str = "created"

    @property
    def id_spelled_out(self) -> str:
        """Readable version for TTS — 'T K T dash A B 1 2 3 4'"""
        parts = []
        for ch in self.id:
            if ch == "-":
                parts.append("dash")
            elif ch.isdigit():
                parts.append(ch)
            else:
                parts.append(ch.upper())
        return " ".join(parts)


class BookingRecord(BaseModel):
    ticket_id: str
    passenger_name: str
    route: str
    date: str
    time: str
    seat: str
    bus: str
    status: TicketStatus
    note: str


class FAQEntry(BaseModel):
    q: str
    a: str
    keywords: list[str] = Field(default_factory=list)


class CallContext(BaseModel):
    """Session-wide state passed between agents."""
    call_id: str
    caller_phone: str
    started_at: datetime = Field(default_factory=datetime.now)

    class Config:
        arbitrary_types_allowed = True
