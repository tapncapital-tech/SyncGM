"""Google ADK Agent & Guided Action UI Orchestrator (Section 3 & Section 5)."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from execution import tools
from execution.tools import InMemorySyncStore


class ActionType(str, Enum):
    MAIN_MENU = "main_menu"
    SYNC_NOW = "sync_now"
    PREVIEW_CHANGES = "preview_changes"
    APPLY_SAFE_CHANGES = "apply_safe_changes"
    COMPARE_CONTACTS = "compare_contacts"
    VIEW_CONFLICTS = "view_conflicts"
    RESOLVE_CONFLICT = "resolve_conflict"
    CONFIGURE_SCHEDULE = "configure_schedule"
    SET_SCHEDULE = "set_schedule"
    SET_POLICY = "set_policy"


class ActionChip(BaseModel):
    """Interactive button / chip presented to the user in guided UI."""
    model_config = ConfigDict(extra="ignore")

    id: str
    label: str
    action: ActionType
    payload: dict[str, Any] = Field(default_factory=dict)


class UIFieldDiff(BaseModel):
    """Side-by-side field difference component."""
    model_config = ConfigDict(extra="ignore")

    field_name: str
    google_value: Optional[str] = None
    microsoft_value: Optional[str] = None
    base_value: Optional[str] = None


class UIConflictCard(BaseModel):
    """Structured side-by-side card for interactive conflict resolution."""
    model_config = ConfigDict(extra="ignore")

    conflict_id: str
    contact_name: str
    field_diffs: list[UIFieldDiff] = Field(default_factory=list)
    action_buttons: list[ActionChip] = Field(default_factory=list)


class AgentResponse(BaseModel):
    """Standardized response payload from the ADK Agent."""
    model_config = ConfigDict(extra="ignore")

    message: str
    action_chips: list[ActionChip] = Field(default_factory=list)
    conflict_cards: list[UIConflictCard] = Field(default_factory=list)
    data: Optional[dict[str, Any]] = None


# =============================================================================
# ADK SYSTEM PROMPT & AGENT ORCHESTRATOR
# =============================================================================

ADK_SYSTEM_INSTRUCTIONS = """
You are the SyncGM Guided Synchronization Agent.
You operate strictly as a deterministic, button-driven assistant.
Rules:
1. Do not ask open-ended questions. Always provide concrete action chips.
2. Present side-by-side diff cards whenever contact conflicts or differences occur.
3. Translate user chip selections into internal engine tool calls.
4. Never make up data or perform unverified writes.
"""


class SyncGMAgent:
    """Google ADK Guided Agent managing conversation state and action routing."""

    def __init__(self, store: Optional[InMemorySyncStore] = None) -> None:
        self.store = store or tools.GLOBAL_SYNC_STORE
        self.system_instructions = ADK_SYSTEM_INSTRUCTIONS

    def _get_main_menu_chips(self) -> list[ActionChip]:
        return [
            ActionChip(id="chip_sync_now", label="Sync Now", action=ActionType.SYNC_NOW),
            ActionChip(id="chip_preview", label="Preview Changes (Dry Run)", action=ActionType.PREVIEW_CHANGES),
            ActionChip(id="chip_compare", label="Compare Contacts", action=ActionType.COMPARE_CONTACTS),
            ActionChip(id="chip_schedule", label="Configure Schedule", action=ActionType.CONFIGURE_SCHEDULE),
            ActionChip(id="chip_conflicts", label="View Unresolved Conflicts", action=ActionType.VIEW_CONFLICTS),
        ]

    def get_welcome_prompt(self, user_id: str, user_name: str = "John Doe") -> AgentResponse:
        """Renders the standard Section 3.1 Welcome Prompt."""
        self.store.ensure_user(user_id, display_name=user_name)
        msg = f"Hello **{user_name}**. Your Google Contacts and Microsoft Contacts are linked. How would you like to proceed?"
        return AgentResponse(
            message=msg,
            action_chips=self._get_main_menu_chips(),
        )

    def _render_conflict_card(self, conflict_dict: dict[str, Any]) -> UIConflictCard:
        c_id = conflict_dict["id"]
        c_name = conflict_dict.get("contact_name", "Unknown Contact")
        c_fields = conflict_dict.get("conflicting_fields", {})

        diffs: list[UIFieldDiff] = []
        for fname, vals in c_fields.items():
            if fname == "identity_match":
                diffs.append(
                    UIFieldDiff(
                        field_name="Identity Match",
                        google_value=str(vals.get("google")),
                        microsoft_value=str(vals.get("microsoft")),
                        base_value=f"Score: {vals.get('score')} ({vals.get('reason')})",
                    )
                )
            else:
                diffs.append(
                    UIFieldDiff(
                        field_name=fname.replace("_", " ").title(),
                        google_value=str(vals.get("google") or "(empty)"),
                        microsoft_value=str(vals.get("microsoft") or "(empty)"),
                        base_value=str(vals.get("base") or "(empty)"),
                    )
                )

        buttons = [
            ActionChip(
                id=f"btn_keep_{c_id}",
                label="Keep Both (Store one as Secondary)",
                action=ActionType.RESOLVE_CONFLICT,
                payload={"conflict_id": c_id, "resolution_choice": "keep_both"},
            ),
            ActionChip(
                id=f"btn_goog_{c_id}",
                label="Use Google Info",
                action=ActionType.RESOLVE_CONFLICT,
                payload={"conflict_id": c_id, "resolution_choice": "use_google"},
            ),
            ActionChip(
                id=f"btn_ms_{c_id}",
                label="Use Microsoft Info",
                action=ActionType.RESOLVE_CONFLICT,
                payload={"conflict_id": c_id, "resolution_choice": "use_microsoft"},
            ),
            ActionChip(
                id=f"btn_skip_{c_id}",
                label="Skip / Leave Unchanged",
                action=ActionType.RESOLVE_CONFLICT,
                payload={"conflict_id": c_id, "resolution_choice": "skip"},
            ),
        ]

        return UIConflictCard(
            conflict_id=c_id,
            contact_name=c_name,
            field_diffs=diffs,
            action_buttons=buttons,
        )

    def handle_action(
        self,
        user_id: str,
        action: str | ActionType,
        payload: Optional[dict[str, Any]] = None,
        db_session: Optional[Session] = None,
    ) -> AgentResponse:
        """Processes structured user actions and returns formatted guided responses."""
        act_enum = ActionType(action) if isinstance(action, str) else action
        p = payload or {}

        # ---------------------------------------------------------------------
        # 1. Main Menu
        # ---------------------------------------------------------------------
        if act_enum == ActionType.MAIN_MENU:
            user_info = self.store.user_profiles.get(user_id, {})
            name = user_info.get("name", "User")
            return self.get_welcome_prompt(user_id, user_name=name)

        # ---------------------------------------------------------------------
        # 2. Preview Changes (Workflow B)
        # ---------------------------------------------------------------------
        elif act_enum == ActionType.PREVIEW_CHANGES:
            preview = tools.preview_sync(user_id=user_id, db_session=db_session, store=self.store)
            summary = preview["summary"]
            conflicts = preview["details"]["conflicts"]

            msg_lines = [
                "### 🔍 Synchronization Preview (Dry Run)",
                "",
                f"- **Contacts to create in Google:** {summary['to_create_in_google']}",
                f"- **Contacts to create in Microsoft:** {summary['to_create_in_ms']}",
                f"- **Contacts to update in Google:** {summary['to_update_in_google']}",
                f"- **Contacts to update in Microsoft:** {summary['to_update_in_ms']}",
                f"- **Unchanged contacts:** {summary['unchanged_records']}",
                f"- **Conflicts detected:** {summary['conflicts_detected']}",
            ]

            chips = [
                ActionChip(id="chip_apply_safe", label="Apply All Safe Changes", action=ActionType.APPLY_SAFE_CHANGES),
                ActionChip(id="chip_main_menu", label="Main Menu", action=ActionType.MAIN_MENU),
            ]

            cards = [self._render_conflict_card(c) for c in conflicts]

            if cards:
                msg_lines.append("")
                msg_lines.append("⚠️ **Conflicts require your resolution before they can be synchronized:**")

            return AgentResponse(
                message="\n".join(msg_lines),
                action_chips=chips,
                conflict_cards=cards,
                data=preview,
            )

        # ---------------------------------------------------------------------
        # 3. Apply Safe Changes / Sync Now (Workflow A)
        # ---------------------------------------------------------------------
        elif act_enum in (ActionType.SYNC_NOW, ActionType.APPLY_SAFE_CHANGES):
            res = tools.execute_sync(user_id=user_id, db_session=db_session, store=self.store)

            if res.get("message") and "not fully linked" in res.get("message"):
                return AgentResponse(
                    message="⚠️ **Accounts Not Connected:** Please click **Connect** on Google Contacts and Microsoft Contacts at the top of the dashboard to authenticate your accounts before syncing.",
                    action_chips=[ActionChip(id="chip_main_menu", label="Main Menu", action=ActionType.MAIN_MENU)],
                    data=res,
                )

            pending_conflicts = tools.get_pending_conflicts(user_id=user_id, db_session=db_session, store=self.store)

            msg_lines = [
                "### ✅ Synchronization Executed",
                "",
                f"- **Created in Google:** {res.get('created_google', 0)}",
                f"- **Created in Microsoft:** {res.get('created_ms', 0)}",
                f"- **Updated in Google:** {res.get('updated_google', 0)}",
                f"- **Updated in Microsoft:** {res.get('updated_ms', 0)}",
            ]

            if res.get("deleted_google", 0) > 0 or res.get("deleted_ms", 0) > 0:
                msg_lines.append(f"- **Deleted in Google:** {res.get('deleted_google', 0)}")
                msg_lines.append(f"- **Deleted in Microsoft:** {res.get('deleted_ms', 0)}")

            msg_lines.append(f"- **Unchanged:** {res.get('unchanged', 0)}")

            cards = [self._render_conflict_card(c) for c in pending_conflicts]
            if cards:
                msg_lines.append(f"- **Conflicts held for review:** {len(cards)}")
                msg_lines.append("")
                msg_lines.append("Please resolve the pending conflicts below:")

            chips = [
                ActionChip(id="chip_main_menu", label="Back to Main Menu", action=ActionType.MAIN_MENU),
            ]

            return AgentResponse(
                message="\n".join(msg_lines),
                action_chips=chips,
                conflict_cards=cards,
                data=res,
            )

        # ---------------------------------------------------------------------
        # 4. Compare Contacts (Workflow C)
        # ---------------------------------------------------------------------
        elif act_enum == ActionType.COMPARE_CONTACTS:
            comp = tools.compare_contacts(user_id=user_id, db_session=db_session, store=self.store)
            if comp.get("total_google") == 0 and comp.get("total_microsoft") == 0 and db_session:
                repo = SyncRepository(db_session)
                user = repo.get_user(user_id)
                if user and (not user.google_refresh_token_enc or not user.ms_refresh_token_enc):
                    return AgentResponse(
                        message="⚠️ **Accounts Not Connected:** Please click **Connect** on Google Contacts and Microsoft Contacts above before comparing stores.",
                        action_chips=[ActionChip(id="chip_main_menu", label="Main Menu", action=ActionType.MAIN_MENU)],
                        data=comp,
                    )

            msg = (
                f"### 📊 Contact Store Comparison\n\n"
                f"- **Total in Google:** {comp['total_google']}\n"
                f"- **Total in Microsoft:** {comp['total_microsoft']}\n"
                f"- **Synchronized Matches:** {comp['definite_matches']}\n"
                f"- **Google-Only Records:** {comp['google_only']}\n"
                f"- **Microsoft-Only Records:** {comp['microsoft_only']}\n"
                f"- **Potential Duplicates:** {comp['potential_duplicates']}\n"
            )
            chips = [
                ActionChip(id="chip_sync_now", label="Sync Now", action=ActionType.SYNC_NOW),
                ActionChip(id="chip_main_menu", label="Main Menu", action=ActionType.MAIN_MENU),
            ]
            return AgentResponse(message=msg, action_chips=chips, data=comp)

        # ---------------------------------------------------------------------
        # 5. View Conflicts (Section 3.2)
        # ---------------------------------------------------------------------
        elif act_enum == ActionType.VIEW_CONFLICTS:
            pending = tools.get_pending_conflicts(user_id=user_id, db_session=db_session, store=self.store)
            if not pending:
                return AgentResponse(
                    message="🎉 **No pending conflicts!** All your contacts are fully aligned.",
                    action_chips=[ActionChip(id="chip_main", label="Main Menu", action=ActionType.MAIN_MENU)],
                )

            cards = [self._render_conflict_card(c) for c in pending]
            return AgentResponse(
                message=f"Found **{len(cards)}** contact conflict(s) requiring resolution:",
                action_chips=[ActionChip(id="chip_main", label="Main Menu", action=ActionType.MAIN_MENU)],
                conflict_cards=cards,
                data={"conflicts_count": len(cards)},
            )

        # ---------------------------------------------------------------------
        # 6. Resolve Conflict Item
        # ---------------------------------------------------------------------
        elif act_enum == ActionType.RESOLVE_CONFLICT:
            c_id = p.get("conflict_id", "")
            choice = p.get("resolution_choice", "skip")
            res = tools.resolve_conflict_item(user_id=user_id, conflict_id=c_id, resolution_choice=choice, db_session=db_session, store=self.store)

            pending = tools.get_pending_conflicts(user_id=user_id, db_session=db_session, store=self.store)
            cards = [self._render_conflict_card(c) for c in pending]

            msg = f"✅ {res.get('message', 'Resolution updated.')}"
            if cards:
                msg += f"\n\n**{len(cards)}** remaining conflict(s) to review."

            chips = [
                ActionChip(id="chip_sync", label="Sync Now", action=ActionType.SYNC_NOW),
                ActionChip(id="chip_main", label="Main Menu", action=ActionType.MAIN_MENU),
            ]
            return AgentResponse(
                message=msg,
                action_chips=chips,
                conflict_cards=cards,
                data=res,
            )

        # ---------------------------------------------------------------------
        # 7. Configure Schedule
        # ---------------------------------------------------------------------
        elif act_enum == ActionType.CONFIGURE_SCHEDULE:
            current_sched = self.store.schedule_config.get(user_id, {"frequency": "daily", "enabled": False})
            status_text = "ENABLED" if current_sched.get("enabled") else "DISABLED"
            freq_text = current_sched.get("frequency", "daily")

            msg = (
                f"### ⏱️ Automated Background Sync Schedule\n\n"
                f"- **Current Status:** `{status_text}`\n"
                f"- **Current Frequency:** `{freq_text}`\n\n"
                f"Select an option below to update your background sync:"
            )

            chips = [
                ActionChip(
                    id="sched_daily",
                    label="Enable: Daily",
                    action=ActionType.SET_SCHEDULE,
                    payload={"frequency": "daily", "enabled": True},
                ),
                ActionChip(
                    id="sched_weekly",
                    label="Enable: Weekly",
                    action=ActionType.SET_SCHEDULE,
                    payload={"frequency": "weekly", "enabled": True},
                ),
                ActionChip(
                    id="sched_monthly",
                    label="Enable: Monthly",
                    action=ActionType.SET_SCHEDULE,
                    payload={"frequency": "monthly", "enabled": True},
                ),
                ActionChip(
                    id="sched_disable",
                    label="Disable Schedule (Off)",
                    action=ActionType.SET_SCHEDULE,
                    payload={"frequency": freq_text, "enabled": False},
                ),
                ActionChip(id="chip_main", label="Back to Main Menu", action=ActionType.MAIN_MENU),
            ]

            return AgentResponse(message=msg, action_chips=chips, data=current_sched)

        # ---------------------------------------------------------------------
        # 8. Set Schedule
        # ---------------------------------------------------------------------
        elif act_enum == ActionType.SET_SCHEDULE:
            frequency = p.get("frequency", "daily")
            enabled = p.get("enabled", True)
            cron_expr = p.get("cron_expr")
            res = tools.configure_schedule(
                user_id=user_id,
                frequency=frequency,
                enabled=enabled,
                cron_expr=cron_expr,
                db_session=db_session,
                store=self.store,
            )

            chips = [
                ActionChip(id="chip_schedule", label="Schedule Settings", action=ActionType.CONFIGURE_SCHEDULE),
                ActionChip(id="chip_main", label="Main Menu", action=ActionType.MAIN_MENU),
            ]
            return AgentResponse(
                message=f"✅ {res['message']}",
                action_chips=chips,
                data=res,
            )

        # ---------------------------------------------------------------------
        # 9. Set Conflict Policy
        # ---------------------------------------------------------------------
        elif act_enum == ActionType.SET_POLICY:
            pol = p.get("policy", "last_writer_wins")
            res = tools.configure_policy(user_id=user_id, sync_policy=pol, db_session=db_session, store=self.store)
            policy_labels = {
                "last_writer_wins": "Most Recent Activity (Last-Writer-Wins)",
                "ask": "Ask Every Time (Review Conflicts)",
                "google_wins": "Prioritize Google",
                "microsoft_wins": "Prioritize Microsoft",
                "keep_both": "Keep Both (Secondary Store)",
                "skip": "Skip Conflicting Records",
            }
            label = policy_labels.get(pol, pol)
            chips = [
                ActionChip(id="chip_sync", label="Sync Now", action=ActionType.SYNC_NOW),
                ActionChip(id="chip_main", label="Main Menu", action=ActionType.MAIN_MENU),
            ]
            return AgentResponse(
                message=f"✅ Conflict resolution preference updated to: **{label}**.",
                action_chips=chips,
                data=res,
            )

        # Fallback
        return self.get_welcome_prompt(user_id)
