from datetime import date
from enum import Enum
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=4000)]
Message = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=4000)]
Location = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=160)]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Status(str, Enum):
    NEW = 'new'
    ACCEPTED = 'accepted'
    IN_PROGRESS = 'in_progress'
    RESOLVED = 'resolved'
    CONFIRMED = 'confirmed'
    REOPENED = 'reopened'


class TicketCreate(Input):
    category: Literal[
        'heating', 'water', 'electricity', 'elevator', 'cleaning', 'yard', 'other'
    ] = 'other'
    location: Location
    description: Description


class StatusChange(Input):
    status: Status
    comment: Message
    expected_version: int = Field(ge=1)


class CommentCreate(Input):
    text: Message


class VerifiedRegistration(Input):
    type: Literal['permanent', 'temporary']
    house_address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=300)]
    fias_house_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    valid_until: date | None = None


class GosuslugiResidencyClaim(Input):
    state: Annotated[str, StringConstraints(strip_whitespace=True, min_length=20, max_length=200)]
    subject_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    registrations: list[VerifiedRegistration] = Field(min_length=1, max_length=5)

class Ticket(BaseModel):
    id: str
    house_id: str
    resident_id: str
    category: str
    location: str
    description: str
    status: Status
    priority: Literal['normal', 'urgent', 'emergency']
    version: int
    created_at: str
    updated_at: str
    due_at: str | None = None
    first_response_at: str | None = None
    closed_at: str | None = None


class Event(BaseModel):
    id: int
    actor_name: str
    actor_role: str
    kind: str
    status: Status
    text: str
    created_at: str


class TicketAttachment(BaseModel):
    id: int
    source: str
    type: str
    external_id: str | None = None
    metadata: dict
    created_at: str


class TicketDetail(Ticket):
    events: list[Event]
    attachments: list[TicketAttachment] = Field(default_factory=list)


class Profile(BaseModel):
    id: str
    name: str
    role: str
    house_id: str
    address: str


class AnnouncementCreate(Input):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=200)]
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=4000)]


class Announcement(BaseModel):
    id: str
    house_id: str
    title: str
    body: str
    created_by: str
    created_at: str
    sent_at: str | None = None
