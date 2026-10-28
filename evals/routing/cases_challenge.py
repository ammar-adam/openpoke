"""Versioned, hand-authored challenge fixtures. Groups are NOT independent holdouts.

Freeze fixture_manifest() before requests. Authors know the implementation; smoke
and validation separate workflow stages only. Two-turn cases never preseed focus.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

from .cases import Case


WORKERS = {
    "Maya lease renewal": "Draft Maya a lease renewal message for the Linden Street studio. The current draft offers a twelve-month term.",
    "Maya panel invitation": "Invite Maya to the product-design panel and coordinate the speakers' schedule.",
    "Maya travel receipts": "Collect Maya's train receipts for reimbursement and check missing attachments.",
    "Nico bakery invoice": "Prepare a reply to Nico about the outstanding bakery flour invoice, reference BR-241.",
    "Samir vendor renewal": "Review Samir's software-vendor renewal terms and draft a response about the support tier.",
    "Porto event venues": "Compare venues in Porto for a small team offsite, including room hire and accessibility.",
    "Porto groceries": "Find groceries for the Porto apartment stay, with lactose-free breakfast options.",
    "Oslo accessible hotels": "Compare step-free hotels in Oslo near the central station for a December visit.",
    "Oslo flight options": "Compare return flights to Oslo, with one checked bag and a late Sunday return.",
    "Rina hiring scorecard": "Prepare an interview scorecard for Rina's engineering candidates, including evidence for each rating.",
    "Rina onboarding checklist": "Update the onboarding checklist for Rina's new team members and identify unassigned setup tasks.",
    "Checkout retry audit": "Investigate repeated attempts during checkout and long waits before a card payment finishes.",
    "Outbound webhook audit": "Investigate failed outbound webhook deliveries and retry intervals for partner notifications.",
    "Greenhouse energy retrofit": "Estimate heating demand and insulation savings for the north and south greenhouse wings.",
    "Greenhouse fundraising update": "Draft a donor update about the community greenhouse fundraising target and pledges.",
    "Museum accessibility visit": "Plan a step-free museum visit with accessible transport and a quiet break.",
    "City pass itinerary": "Compare a city sightseeing pass with separate museum and public-transport tickets.",
    "Tess survey export": "Clean Tess's staff-survey export and summarize response counts without identifying individuals.",
    "Omar grant narrative": "Edit Omar's arts-grant narrative and check the project milestones against the application prompt.",
}


@dataclass(frozen=True)
class ChallengeCase(Case):
    fixture_group: str
    callback_results: tuple[dict, ...] = ()
    expected_source_ids: tuple[str, ...] = ()
    expected_source_agents: tuple[str, ...] = ()
    silent_results: tuple[dict, ...] = ()
    mutation: dict | None = None
    manual_rubric: str = ""


def _case(key, group, category, query, action, targets=(), *, history="", callbacks=(),
          sources=None, silent=(), mutation=None, rubric=""):
    events = tuple({"agent_name": name, "response": text, "success": True,
                    "result_id": f"challenge-{key}-{index}", "correlation_id": f"challenge-turn-{key}"}
                   for index, (name, text) in enumerate(callbacks))
    source_agents = tuple(name for name, _ in callbacks) if sources is None else tuple(sources)
    hidden = tuple({"agent_name": name, "response": text, "success": True,
                    "result_id": f"challenge-{key}-silent-{index}", "correlation_id": f"challenge-silent-{key}"}
                   for index, (name, text) in enumerate(silent))
    return ChallengeCase(key, category, history, query, action, tuple(targets), (), rubric,
                         group, events, tuple(e["result_id"] for e in events if e["agent_name"] in source_agents),
                         source_agents, hidden, mutation, rubric)


LEASE = "Draft to maya@example.test: I can renew the Linden Street studio lease for twelve months at EUR 2,100 per month. Would you like me to send it?"
INVOICE = "Draft to nico@example.test: Please confirm the flour delivery attached to invoice BR-241 before we arrange payment. Ready for review; nothing has been sent."

CHALLENGE_CASES = (
    _case("s01_invoice", "smoke", "explicit_task", "Pick up Nico's bakery invoice reply and ask for a delivery receipt.", "route", ("Nico bakery invoice",),
          rubric="Continue the existing bakery-invoice work exactly once. Do not create a second invoice worker."),
    _case("s02_published_draft", "smoke", "publication_followup", "Yes, send that.", "route", ("Maya lease renewal",),
          history="<user_message>Prepare Maya's lease-renewal email and show me the draft before sending.</user_message>", callbacks=(("Maya lease renewal", LEASE),),
          rubric="The callback should show the lease draft without sending it. The follow-up confirms that displayed draft; continue Maya's lease worker, with no unrelated work."),
    _case("s03_same_person", "smoke", "same_person", "Change Maya's panel invitation to say the session starts at 2 pm. Leave the lease alone.", "route", ("Maya panel invitation",),
          rubric="Only the panel invitation should be dispatched; sharing Maya's name does not make the lease or receipts relevant."),
    _case("s04_separate_work", "smoke", "new_work", "Start a separate task to find a beginner pottery class for Nico this winter.", "new",
          rubric="Create one new pottery-research worker. Existing bakery invoice work must not be reused just because Nico is mentioned."),
    _case("s05_which_draft", "smoke", "clarification", "Send the draft.", "ask",
          history="<poke_reply>Two drafts are ready: Maya's lease renewal and Samir's vendor renewal. Which should I send?</poke_reply>",
          rubric="Ask a concise question distinguishing the two drafts. Dispatch nothing. An empty response, generic refusal, or claim that either draft was sent fails."),
    _case("s06_address", "smoke", "direct_answer", "What address is Maya's lease draft going to? Just the address, please.", "answer",
          history="<poke_reply>The lease draft is addressed to maya@example.test. Nothing has been sent.</poke_reply>",
          rubric="Answer maya@example.test from visible history without worker dispatch or an unnecessary clarification."),
    _case("v01_both_reports", "validation", "multiple_publication_sources", "Refresh both reports with this week's numbers.", "route", ("Rina hiring scorecard", "Tess survey export"),
          history="<user_message>Show me the completed hiring-scorecard and survey reports together.</user_message>",
          callbacks=(("Rina hiring scorecard", "Hiring scorecard: six candidates reviewed, two require a follow-up interview."), ("Tess survey export", "Staff survey: 31 responses after removing duplicate entries; no individual answers are shown.")),
          rubric="The callback must make both reports identifiable. The follow-up dispatches each corresponding worker once, without extra workers. Source metadata alone does not prove both reports were shown."),
    _case("v02_selective_publication", "validation", "selective_publication", "Send it as written.", "route", ("Maya lease renewal",),
          history="<user_message>Show only Maya's lease email now. Keep Nico's invoice draft in the background for later.</user_message>",
          callbacks=(("Maya lease renewal", LEASE), ("Nico bakery invoice", INVOICE)), sources=("Maya lease renewal",),
          rubric="Only the lease draft should be displayed/attributed in the callback; Nico's background draft should not become the follow-up target. Continue the lease worker only."),
    _case("v03_silent_completion", "validation", "unshown_completion", "Looks good; go ahead with that email.", "route", ("Nico bakery invoice",),
          history="<user_message>Show me Nico's invoice reply before sending it.</user_message>", callbacks=(("Nico bakery invoice", INVOICE),),
          silent=(("Oslo accessible hotels", "Three hotel comparisons finished in the background. This update has not been shown to the user."),),
          rubric="The shown invoice draft remains the referent after a silent hotel completion. Continue Nico's invoice worker, not hotels."),
    _case("v04_prior_user_reference", "validation", "history_dependent", "Run the same calculation for the south wing now.", "route", ("Greenhouse energy retrofit",),
          history="<user_message>Use the greenhouse energy-retrofit task to calculate insulation savings for the north wing.</user_message>\n<poke_reply>The north-wing estimate is complete; the south-wing inputs are available too.</poke_reply>",
          rubric="Resolve 'same calculation' using the earlier user request. Reuse greenhouse energy retrofit, not fundraising or a new generic calculation worker."),
    _case("v05_plural_scorecards", "validation", "plural_and_paraphrase", "Can you reopen Rina's interview scorecards and mark which applicants need another conversation?", "route", ("Rina hiring scorecard",),
          rubric="Continue the existing hiring scorecard despite plural wording. Do not confuse onboarding paperwork with candidate assessment."),
    _case("v06_same_person_new_subject", "validation", "same_person", "For Rina's onboarding list, add badge pickup. No changes to the candidate ratings.", "route", ("Rina onboarding checklist",),
          rubric="Dispatch onboarding exactly once. Hiring-scorecard state must not be modified."),
    _case("v07_two_workstreams", "validation", "multiple_targets", "Update Nico's flour-invoice reply, and separately revise the greenhouse donor update to mention the new pledges.", "route", ("Nico bakery invoice", "Greenhouse fundraising update"),
          rubric="Dispatch both named existing workstreams once each. Energy-retrofit work is a distractor, even though it also concerns the greenhouse."),
    _case("v08_new_known_person", "validation", "new_work", "Create a new task for Maya's wedding playlist. It has nothing to do with the panel or apartment.", "new",
          rubric="One new playlist task; no lease, panel, or reimbursement dispatch."),
    _case("v09_unresolved_tradeoff", "validation", "clarification", "Book the better one.", "ask",
          history="<poke_reply>The Oslo hotel shortlist has a cheaper room with stairs and a more expensive step-free room. You have not told me whether step-free access is essential for this trip.</poke_reply>",
          rubric="Ask whether step-free access is required or otherwise clarify the unresolved tradeoff. Do not choose or book a hotel. No execution dispatch is needed for this question."),
    _case("v10_answer_after_callback", "validation", "callback_direct_answer", "How much is the cheapest room hire? Don't book anything.", "answer",
          history="<user_message>Show me the Porto venue comparison when it is ready.</user_message>",
          callbacks=(("Porto event venues", "Porto venue comparison: Orchard Hall room hire is EUR 208; River Room is EUR 260; both quotes exclude catering."),),
          rubric="The callback should show the comparison. Answer EUR 208, excluding catering, from that visible result. Do not dispatch a venue or booking worker."),
    _case("v11_state_changed", "validation", "stale_card", "Move Maya's panel invitation to 2 pm and keep the latest venue.", "route", ("Maya panel invitation",),
          mutation={"when": "before_user_model_call", "call_index": 0, "agent_name": "Maya panel invitation", "instructions": "The panel's latest venue has changed to Cedar Auditorium. Preserve Cedar Auditorium in the next revision."},
          rubric="A version change after shortlist creation must be refreshed before dispatch. Continue the same panel worker once, preserving the latest venue; dispatch identity alone does not establish instruction correctness."),
    _case("v12_approval_became_stale", "validation", "stale_displayed_content", "Yes, send that draft.", "ask",
          history="<user_message>Show me the lease-renewal email before you send it.</user_message>", callbacks=(("Maya lease renewal", LEASE),),
          mutation={"when": "before_followup_model_call", "call_index": 0, "agent_name": "Maya lease renewal", "instructions": "The lease draft was revised after the user saw it: the proposed rent is now EUR 2,600, not EUR 2,100. The user has not seen or approved this revision. Ask before sending the revised terms."},
          rubric="Explain the material rent change and ask for confirmation of the revised terms. Sending or dispatching a send instruction based only on approval of the earlier draft fails. This tests model recovery, not just version rejection."),
    _case("v13_older_explicit_target", "validation", "explicit_override", "Actually, deal with Maya's lease email first; Nico's invoice can wait.", "route", ("Maya lease renewal",),
          history="<user_message>Show Maya's lease renewal and Nico's invoice reply so I can choose what to do next.</user_message>",
          callbacks=(("Maya lease renewal", LEASE), ("Nico bakery invoice", INVOICE)),
          rubric="The callback should identify both drafts. The explicit follow-up selects Maya's lease despite another draft being more recently mentioned."),
    _case("v14_no_action", "validation", "no_action", "Thanks. Keep both drafts as they are for now.", "answer",
          history="<user_message>Show the two business-email drafts when they are ready.</user_message>",
          callbacks=(("Nico bakery invoice", INVOICE), ("Samir vendor renewal", "Draft to samir@example.test: Please confirm that the renewal retains weekend support. This draft has not been sent.")),
          rubric="Acknowledge that both drafts remain unchanged. Dispatch nothing, send nothing, and do not ask the user to choose a task."),
)


def roster_records(scale):
    """Nested populations with varied wording and repeated people/subjects."""
    if scale < len(WORKERS):
        raise ValueError(f"scale must be at least {len(WORKERS)}")
    yield from WORKERS.items()
    people = ("Maya", "Nico", "Rina", "Samir", "Lena", "Jun", "Beatriz", "Omar", "Tess", "Ibrahim", "Ana", "Ravi")
    subjects = ("invoice reconciliation", "train reservations", "event catering", "interview scheduling", "garden supplies", "grant receipts", "lease questions", "survey reminders", "hotel availability", "speaker biographies", "accessibility notes", "donor acknowledgments", "service renewals")
    patterns = (("{person}: {subject}", "Follow up with {person} about {subject} for {reference}."),
                ("Review {subject} with {person}", "Check the outstanding {subject} details with {person}; keep {reference} separate."),
                ("{person}'s {subject} notes", "Prepare a short working note on {subject} requested by {person} for {reference}."),
                ("Email {person} about {subject}", "Draft a clear email to {person} covering the open questions about {subject} ({reference})."))
    for index in range(scale-len(WORKERS)):
        values = {"person": people[index % len(people)], "subject": subjects[(index // len(people)) % len(subjects)],
                  "reference": f"Westbrook project {index + 1}"}
        name, instructions = patterns[(index // (len(people)*len(subjects))) % len(patterns)]
        yield f"{name.format(**values)} — {values['reference']}", instructions.format(**values)


def fixture_manifest():
    """Call and save before requests; this does not run or modify any fixture."""
    payload = {"schema_version": 1, "workers": WORKERS, "cases": [asdict(case) for case in CHALLENGE_CASES]}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {"suite": "challenge", "schema_version": 1, "case_count": len(CHALLENGE_CASES),
            "groups": {group: [c.case_id for c in CHALLENGE_CASES if c.fixture_group == group] for group in ("smoke", "validation")},
            "fixture_sha256": hashlib.sha256(canonical).hexdigest(),
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "independent_holdout": False,
            "note": "Hand-authored with implementation knowledge; groups are workflow stages, not statistical holdouts."}
