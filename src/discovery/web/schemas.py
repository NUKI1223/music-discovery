"""Public API contracts. Exported to docs/web-openapi.json before implementation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from discovery.contracts import ArtistPopularityLabel, RequestIntent


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filters(Model):
    genre_scope_id: str = Field(default="hip-hop", description="Genre ID, or all for unrestricted recommendations across the catalog.")
    mode: Literal["close", "explore", "surprise"] = "explore"
    obscurity: Literal["prefer", "require"] = "prefer"
    locked_subgenre_ids: list[str] = Field(default_factory=list, max_length=20)
    exclude_known_artists: bool = False
    reference_artist_ids: list[str] = Field(default_factory=list, max_length=20)
    requested_artist_ids: list[str] = Field(default_factory=list, max_length=20)
    excluded_artist_ids: list[str] = Field(default_factory=list, max_length=50)
    preferred_descriptors: list[str] = Field(default_factory=list, max_length=8)
    avoided_descriptors: list[str] = Field(default_factory=list, max_length=8)

    def intent(self, **overrides) -> RequestIntent:
        """Personal artist exclusions are resolved by the server before ranking."""
        return RequestIntent(**(self.model_dump(exclude={"exclude_known_artists"}) | overrides))

    def validate_catalog(self, catalog) -> None:
        self.intent().validate_catalog(catalog)
        if not set(self.excluded_artist_ids) <= {a.id for a in catalog.artists}:
            raise ValueError("Unknown excluded artist")


class Credentials(Model):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    password: str = Field(min_length=12, max_length=128)


class Registration(Credentials):
    password_confirmation: str = Field(min_length=12, max_length=128)


class PasswordChange(Model):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class User(Model):
    id: str
    username: str


class Session(Model):
    user: User
    csrf_token: str


class Profile(Model):
    onboarded: bool = False
    filters: Filters = Field(default_factory=Filters)
    seed_artist_ids: list[str] = Field(default_factory=list, max_length=50)
    avoid_tag_ids: list[str] = Field(default_factory=list, max_length=50)
    request_only_artist_ids: list[str] = Field(default_factory=list, max_length=50)
    limit_widely_known_artists: bool = False
    max_widely_known_artists: int = Field(default=2, ge=0, le=2)
    artist_popularity_labels: dict[str, ArtistPopularityLabel] = Field(default_factory=dict, max_length=500)


class Lookup(Model):
    id: str
    name: str


class SourceLink(Model):
    label: str
    url: str


class HistoricalRating(Model):
    source: str
    rating: float | None
    scale: float
    count: int | None


class CollectionChat(Model):
    id: str
    title: str
    deleted: bool = False


class ReleaseCard(Model):
    release_id: str
    decision_id: str
    title: str
    artists: list[str]
    year: int | None
    genres: list[str]
    explanation: list[str]
    warnings: list[str]
    links: list[SourceLink]
    historical_ratings: list[HistoricalRating]
    feedback: Literal["known", "save_for_later", "listened_like", "listened_dislike"] | None = None
    listened: bool = False
    chats: list[CollectionChat] = Field(default_factory=list)
    saved_at: str | None = None


class ChatCreate(Model):
    title: str = Field(default="Новый чат", min_length=1, max_length=120)
    filters: Filters


class ChatSummary(Model):
    id: str
    title: str
    filters: Filters
    created_at: str
    updated_at: str


class MessageRequest(Model):
    message_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(default="", max_length=4000)
    apply_filters: bool = Field(default=False, description="Rank by the current confirmed chat filters without calling the assistant; text is ignored.")
    release_id: str | None = None


class Message(Model):
    id: str
    role: Literal["user", "assistant"]
    text: str
    created_at: str
    status: Literal["pending", "completed", "failed", "interrupted"] = "completed"
    cards: list[ReleaseCard] = Field(default_factory=list)
    error_code: str | None = None
    proposed_filters: Filters | None = None


class ChatMemory(Model):
    notes: str = Field(default="", max_length=1600)
    summary: list[dict] = Field(default_factory=list)
    summarized_messages: int = 0
    latest_request: str = ""


class MemoryUpdate(Model):
    notes: str = Field(default="", max_length=1600)
    reset: bool = False


class ChatDetail(ChatSummary):
    messages: list[Message]
    has_older: bool = False
    has_newer: bool = False
    oldest_seq: int | None = None
    newest_seq: int | None = None
    memory: ChatMemory = Field(default_factory=ChatMemory)


class FeedbackRequest(Model):
    feedback_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    decision_id: str
    release_id: str
    kind: Literal["known", "save_for_later", "listened_like", "listened_dislike"]


class Ok(Model):
    ok: bool = True


class APIError(Model):
    detail: str


class ListeningLink(Model):
    service: str
    url: str
    source_url: str


class Artwork(Model):
    listening_links: list[ListeningLink] = Field(default_factory=list)
    link_status: dict[str, str] = Field(default_factory=dict)
    listening_service: str | None = None
    source_url: str | None = None
    aoty_status: str | None = None
    artwork: str | None = None
    listening_url: str | None = None
