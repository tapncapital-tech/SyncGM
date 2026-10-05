"""SyncGM Execution Package."""
from execution.agent import (
    ActionChip,
    ActionType,
    AgentResponse,
    SyncGMAgent,
    UIConflictCard,
    UIFieldDiff,
)
from execution.diff_engine import SyncPlanner, ThreeWayDiffEngine
from execution.exceptions import (
    AuthenticationError,
    ContactNotFoundError,
    RateLimitError,
    SyncAPIError,
    TokenExpiredError,
)
from execution.google_client import GoogleContactsClient
from execution.matcher import ContactMatcher
from execution.models import (
    ConflictChoice,
    ConflictItem,
    ContactMatchPair,
    ContactUpdateAction,
    EmailAddress,
    FieldPatch,
    MatchBreakdown,
    MatchCategory,
    MatchingResult,
    NameDetails,
    OrganizationDetails,
    OrphanedContact,
    PhoneNumber,
    PostalAddress,
    SyncPlan,
    UnifiedContact,
)
from execution.ms_client import MicrosoftContactsClient
from execution.tools import (
    GLOBAL_SYNC_STORE,
    InMemorySyncStore,
    compare_contacts,
    configure_schedule,
    execute_sync,
    get_pending_conflicts,
    get_sync_audit_report,
    preview_sync,
    resolve_conflict_item,
)

__all__ = [
    # Models
    "UnifiedContact",
    "NameDetails",
    "EmailAddress",
    "PhoneNumber",
    "PostalAddress",
    "OrganizationDetails",
    "MatchCategory",
    "MatchBreakdown",
    "ContactMatchPair",
    "MatchingResult",
    "FieldPatch",
    "ContactUpdateAction",
    "ConflictChoice",
    "ConflictItem",
    "OrphanedContact",
    "SyncPlan",
    # Agent UI
    "ActionType",
    "ActionChip",
    "UIFieldDiff",
    "UIConflictCard",
    "AgentResponse",
    "SyncGMAgent",
    # Tools & Store
    "InMemorySyncStore",
    "GLOBAL_SYNC_STORE",
    "preview_sync",
    "execute_sync",
    "get_pending_conflicts",
    "resolve_conflict_item",
    "configure_schedule",
    "compare_contacts",
    "get_sync_audit_report",
    # Matching & Diff Engines
    "ContactMatcher",
    "ThreeWayDiffEngine",
    "SyncPlanner",
    # Clients & Exceptions
    "GoogleContactsClient",
    "MicrosoftContactsClient",
    "SyncAPIError",
    "RateLimitError",
    "TokenExpiredError",
    "AuthenticationError",
    "ContactNotFoundError",
]
