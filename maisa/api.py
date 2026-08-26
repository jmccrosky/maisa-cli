"""High-level Maisa (Epic MyChart) API, built on the reverse-engineered endpoints.

Everything here runs over plain httpx via MaisaClient — no browser. Endpoints
were mapped from a live session (see NOTES.md). Read methods are safe; write
methods (send_message, archive) change server state and are called only on
explicit user action.

ID note: Epic record IDs are opaque URL-safe-base64 tokens like
"WP-24...-3D-3D-24...-3D". Pass them back verbatim; never decode/modify them.
"""

from __future__ import annotations

import html
import re
from typing import Any

from .client import MaisaClient
from .session import load_or_die

# Message folder tags (from GetFoldersList).
FOLDER_INBOX = 1
FOLDER_BOOKMARKED = 3
FOLDER_AUTOMATED = 6
FOLDER_ARCHIVE = 2
FOLDER_NAMES = {1: "Conversations", 2: "Archive", 3: "Bookmarked", 6: "Automated"}

_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    s = s.replace("</p>", "\n").replace("</div>", "\n")
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


class MaisaAPI:
    def __init__(self, client: MaisaClient | None = None):
        self.c = client or MaisaClient(load_or_die())

    def close(self) -> None:
        self.c.close()

    def __enter__(self) -> "MaisaAPI":
        return self

    def __exit__(self, *exc: object) -> None:
        self.c.persist()
        self.close()

    # -- identity / account ----------------------------------------------

    def accounts(self) -> dict:
        """ProxySwitch: the self account + any proxy (family) accounts."""
        return self.c.get_json("ProxySwitch")

    def whoami(self) -> dict:
        data = self.accounts()
        subjects = data.get("ProxySubjectList", [])
        me = next((s for s in subjects if s.get("IsSelf")), subjects[0] if subjects else {})
        return {
            "name": me.get("DisplayName"),
            "id": me.get("Id"),
            "proxies": [s.get("DisplayName") for s in subjects if not s.get("IsSelf")],
        }

    def menu(self) -> dict:
        return self.c.post_form("api/search/LoadMenuInfo").json()

    # -- messages ---------------------------------------------------------

    def message_folders(self) -> list[dict]:
        folders = self.c.json_api("api/conversations/GetFoldersList").get("folders", [])
        for f in folders:
            f["name"] = FOLDER_NAMES.get(f.get("tag"), str(f.get("tag")))
        return folders

    def messages(self, folder: int = FOLDER_INBOX, search: str = "", limit: int = 0) -> list[dict]:
        payload = {
            "tag": folder,
            "localLoadParams": {"loadStartInstantISO": "", "loadEndInstantISO": "", "pagingInfo": 1},
            "externalLoadParams": {},
            "searchQuery": search,
        }
        convs = self.c.json_api("api/conversations/GetConversationList", payload).get("conversations", [])
        out = []
        for cv in convs:
            out.append({
                "id": cv.get("id") or cv.get("hthId"),
                "subject": cv.get("subject"),
                "preview": html_to_text(cv.get("previewText"))[:200],
                "date": cv.get("dateDisplayText") or cv.get("lastMessageInstantISO"),
                "unread": cv.get("numUnread", 0),
                "hasAttachments": cv.get("hasAttachments", False),
            })
        return out[:limit] if limit else out

    def conversation(self, conv_id: str) -> dict:
        d = self.c.json_api("api/conversations/GetConversationDetails",
                            {"id": conv_id, "messageId": "", "organizationId": ""})
        users = d.get("users", {})

        def uname(author):
            # Provider messages carry empKey (-> users map); the patient's own
            # messages carry wprKey and an empty displayName.
            if isinstance(author, dict):
                if author.get("displayName"):
                    return author["displayName"]
                emp = author.get("empKey")
                if emp:
                    u = users.get(emp) or {}
                    return u.get("name") or u.get("displayName") or "Care team"
                if author.get("wprKey"):
                    return "You"
            return "You"

        messages = []
        for m in d.get("messages", []):
            messages.append({
                "from": uname(m.get("author", "")),
                "date": m.get("deliveryInstantISO"),
                "unread": m.get("isUnread", False),
                "body": html_to_text(m.get("body")),
                "attachments": [a.get("name") for a in m.get("attachments", []) if isinstance(a, dict)],
            })
        return {
            "id": conv_id,
            "subject": d.get("subject"),
            "totalMessages": d.get("totalMessages"),
            "canReply": (d.get("replyFlags") or {}).get("canReply", False),
            "messages": messages,
        }

    def compose_topics(self) -> list[dict]:
        """The 'Send a message' subject options (menuItemId + label)."""
        j = self.c.json_api("api/conversations/GetMessageMenuSettings", {"organizationId": ""})
        return [{"menuItemId": t.get("menuItemId"), "name": t.get("displayName"),
                 "subtext": t.get("subtext"), "type": t.get("type")}
                for t in j.get("topicList", [])]

    def compose_limits(self, menu_item_id: str) -> dict:
        cid = self.c.json_api("api/conversations/GetComposeId", {"menuItemId": menu_item_id})
        settings = self.c.json_api("api/conversations/GetComposeSettings", {"composeId": cid})
        return {"composeId": cid, **settings}

    def send_message(self, menu_item_id: str, subject: str, body: str,
                     organization_id: str = "") -> dict:
        """Send a NEW message under a topic. STATE-CHANGING: goes to providers.

        Flow: GetComposeId(topic) -> SendReply({conversationId=composeId,...}).
        """
        cid = self.c.json_api("api/conversations/GetComposeId", {"menuItemId": menu_item_id})
        limits = self.c.json_api("api/conversations/GetComposeSettings", {"composeId": cid})
        maxsub = limits.get("maxSubjectLength", 254)
        maxbody = limits.get("maxMessageLength", 2000)
        if len(subject) > maxsub:
            raise ValueError(f"subject too long (max {maxsub})")
        if len(body) > maxbody:
            raise ValueError(f"body too long (max {maxbody})")
        payload = {
            "conversationId": cid,
            "organizationId": organization_id,
            "subject": subject,
            "messageBody": body,
            "attachments": [],
        }
        result = self.c.json_api("api/conversations/SendReply", payload)
        return {"sent": True, "composeId": cid, "result": result}

    def archive_conversations(self, ids: list[str]) -> Any:
        """BulkConversationAction — archive. STATE-CHANGING."""
        return self.c.json_api("api/conversations/BulkConversationAction",
                               {"conversationIds": ids, "action": "Archive"})

    # -- test results -----------------------------------------------------

    def test_results(self, max_results: int = 0) -> list[dict]:
        j = self.c.json_api("api/test-results/GetList",
                            {"groupType": "UNINITIALIZED", "searchString": "",
                             "maxResults": max_results, "isCurAdmFilterEnabled": False})
        # Group lists hold result-id strings; details live in `newResults`,
        # keyed by the id (sometimes with a trailing "^").
        details = j.get("newResults", {}) or {}

        def lookup(rid):
            return details.get(rid) or details.get(rid + "^") or {}

        out = []
        for g in j.get("newResultGroups", []):
            for rid in g.get("resultList", []):
                det = lookup(rid)
                meta = det.get("orderMetadata", {}) or {}
                out.append({
                    "csn": rid,
                    "name": det.get("name"),
                    "date": g.get("formattedDate") or det.get("instantOfInterestDisplayText"),
                    "provider": meta.get("authorizingProviderName") or meta.get("orderProviderName"),
                    "organizationID": g.get("organizationID", ""),
                })
        return out

    def test_result(self, csn: str, organization_id: str = "") -> dict:
        return self.c.json_api("api/test-results/GetDetailsByCSN",
                               {"organizationID": organization_id, "csn": csn})

    # -- letters ----------------------------------------------------------

    def letters(self) -> list[dict]:
        j = self.c.json_api("api/letters/GetLettersList", {})
        users = j.get("users", {}) if isinstance(j, dict) else {}
        lst = j.get("letters", []) if isinstance(j, dict) else j
        out = []
        for l in lst:
            emp = l.get("empId", "")
            sender = (users.get(emp) or {}).get("name") if isinstance(users, dict) else None
            out.append({
                "hnoId": l.get("hnoId"),
                "csn": l.get("csn", ""),
                "title": l.get("reason"),
                "date": l.get("dateISO"),
                "viewed": l.get("viewed"),
                "from": sender,
            })
        return out

    def letter(self, hno_id: str, csn: str = "") -> dict:
        return self.c.json_api("api/letters/GetLetterDetails", {"hnoId": hno_id, "csn": csn})

    # -- health record ----------------------------------------------------

    def health_summary(self) -> dict:
        return self.c.json_api("api/health-summary/FetchHealthSummary", {})

    def allergies(self) -> dict:
        return self.c.json_api("api/allergies/LoadAllergies", {"isHealthSummary": True})

    def health_issues(self) -> dict:
        return self.c.json_api("api/HealthIssues/LoadHealthIssuesData", {"isHealthSummary": True})

    def immunizations(self) -> dict:
        return self.c.json_api("api/immunizations/LoadImmunizations", {})

    def medications(self) -> dict:
        return self.c.json_api("api/medications/LoadMedicationsPage", {"context": 2})

    def care_team(self) -> dict:
        return self.c.post_form("Clinical/CareTeam/Load").json()

    # -- appointments / visits -------------------------------------------

    def _visit(self, v: dict) -> dict:
        dept = v.get("PrimaryDepartment") or {}
        providers = v.get("Providers") or []
        provider = v.get("PrimaryProviderName")
        if not provider and providers:
            provider = providers[0].get("Name") or providers[0].get("ProviderName")
        return {
            "csn": v.get("CsnForECheckIn") or v.get("Csn"),
            "type": v.get("VisitTypeName"),
            "date": v.get("Date"),
            "time": v.get("Time"),
            "primaryDate": v.get("PrimaryDate"),
            "provider": provider,
            "department": dept.get("Name") if isinstance(dept, dict) else dept,
            "address": dept.get("Address") if isinstance(dept, dict) else None,
            "phone": dept.get("PhoneNumber") if isinstance(dept, dict) else None,
            "instructions": html_to_text(v.get("PatientNextStepInstructions")) or None,
        }

    def appointments(self) -> list[dict]:
        """Upcoming appointments (Visits/VisitsList/LoadUpcoming)."""
        j = self.c.post_form("Visits/VisitsList/LoadUpcoming").json()
        visits = []
        for bucket in ("InProgressVisits", "NextNDaysVisits", "LaterVisitsList"):
            for v in j.get(bucket) or []:
                visits.append(self._visit(v))
        return visits

    def past_visits(self, index: int = 0) -> list[dict]:
        j = self.c.post_form("Visits/VisitsList/LoadPast",
                             data={"serializedIndex": str(index)}).json()
        out = []
        for bucket in ("PastVisitsList", "EarlierVisitsList", "LaterVisitsList"):
            for v in j.get(bucket) or []:
                out.append(self._visit(v))
        return out
