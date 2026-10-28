"""Additional authored stress cases, frozen separately after the smoke review.

These are not independent holdouts. Reuse the existing workers and population
generator; do not alter the original challenge requests or their review labels.
"""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from .cases_challenge import ChallengeCase, WORKERS, roster_records


def _case(key, category, query, action, targets=(), *, history="", callbacks=(), sources=None, rubric):
    events = tuple({"agent_name": name, "response": text, "success": True,
                    "result_id": f"extension-{key}-{index}", "correlation_id": f"extension-turn-{key}"}
                   for index, (name, text) in enumerate(callbacks))
    source_agents = tuple(name for name, _ in callbacks) if sources is None else tuple(sources)
    return ChallengeCase(
        case_id=key, category=category, history=history, query=query,
        expected_action=action, expected_agents=tuple(targets), published_agents=(),
        evidence=rubric, fixture_group="extension", callback_results=events,
        expected_source_ids=tuple(event["result_id"] for event in events if event["agent_name"] in source_agents),
        expected_source_agents=source_agents, manual_rubric=rubric,
    )


def _draft(to, subject, body):
    # The email body is an explicit field. Status/approval are never email text.
    return json.dumps({"artifact_type": "email_draft", "to": to, "subject": subject,
                       "body": body, "status": "not_sent", "user_confirmation_required": True},
                      ensure_ascii=False, sort_keys=True)


VENDOR_DRAFT = _draft("samir@example.test", "Renewal support coverage",
                     "Hi Samir, please confirm whether the renewal includes weekday support from 09:00 to 17:00.")
PANEL_DRAFT = _draft("maya@example.test", "Design panel invitation",
                    "Hi Maya, we would be delighted if you joined our product-design panel on Thursday at 15:00. The session lasts 45 minutes. Please let us know whether you can attend.")
LEASE_DRAFT = _draft("maya@example.test", "Linden Street renewal",
                    "Hi Maya, I would like to renew the Linden Street studio lease for twelve months at EUR 2,250 per month.")
INVOICE_DRAFT = _draft("nico@example.test", "Delivery documentation for BR-241",
                      "Hi Nico, please attach the signed delivery note for flour invoice BR-241 so we can reconcile the quantities.")


EXTENSION_CASES = (
    _case("x01_flour_quantities", "explicit_existing_work",
          "On Nico's flour invoice reply, ask him to break out the delivered quantities by bag size. Keep the rest of the reply as it is.",
          "route", ("Nico bakery invoice",),
          rubric="Continue the existing invoice worker once. Add the bag-size quantity request while preserving the rest; do not create a fresh invoice task or claim an email was sent."),
    _case("x02_maya_receipts", "same_person_different_subject",
          "Maya's train reimbursement is still missing an attachment. Pick that up and find out which receipt is absent; her apartment and speaking invitation can wait.",
          "route", ("Maya travel receipts",),
          rubric="Dispatch only Maya travel receipts to identify the missing attachment. Do not modify the lease or panel work, and do not invent which receipt is absent."),
    _case("x03_grant_milestones", "cold_existing_work",
          "Return to Omar's arts application. Check whether each promised milestone actually answers the funder's prompt, and flag any gaps without inventing achievements.",
          "route", ("Omar grant narrative",),
          rubric="Reuse the grant narrative worker to compare milestones with the application prompt. Preserve the instruction not to invent achievements; no new application worker."),
    _case("x04_checkout_spinner", "difficult_paraphrase",
          "The till screen keeps spinning after customers press Pay, then some of them tap it again. Reopen our investigation into that problem.",
          "route", ("Checkout retry audit",),
          rubric="Continue Checkout retry audit: this describes delayed payment completion and repeat attempts. Outbound webhook delivery is a different investigation. Lexical failure is an allowed measured failure, not grounds to alter this case."),
    _case("x05_partner_pings", "paraphrase_near_neighbor",
          "Partners say our outgoing integration pings arrive late or not at all. Go back to the delivery investigation and inspect the resend timing, rather than the shopper-payment issue.",
          "route", ("Outbound webhook audit",),
          rubric="Only Outbound webhook audit should investigate partner delivery and resend intervals. Do not route to Checkout retry audit or silently create a duplicate investigation."),
    _case("x06_short_continuation", "short_user_history",
          "Make it more concise, please.", "route", ("Greenhouse fundraising update",),
          history="<user_message>Revise our community greenhouse donor update to thank people for the latest pledges.</user_message><poke_reply>The donor update is ready for a wording pass; no message has been sent.</poke_reply>",
          rubric="Use the short history to continue the fundraising update and shorten its wording. Do not alter the energy-retrofit calculation or claim a donor email has already been sent."),
    _case("x07_two_city_plans", "multiple_workstreams",
          "For the museum visit, add a quiet break after lunch. Separately, rerun the city-pass comparison assuming we buy two transport tickets a day.",
          "route", ("Museum accessibility visit", "City pass itinerary"),
          rubric="Dispatch each named workstream once, keeping the quiet-break requirement with the museum visit and the two-daily-ticket assumption with the pass comparison. No extra workers."),
    _case("x08_flights_override", "explicit_override",
          "Leave those rooms alone for now. It is the Oslo flights I want changed: keep the checked bag, but make the return Monday morning instead of Sunday night.",
          "route", ("Oslo flight options",),
          history="<user_message>Compare the step-free Oslo hotels near the station.</user_message><poke_reply>The hotel comparison is ready: Harbor House and Station Lodge are the leading options.</poke_reply>",
          rubric="The current explicit flight request overrides the prior hotel topic. Dispatch Oslo flight options once with the checked bag and Monday-morning return; no hotel changes."),
    _case("x09_vendor_draft_revision", "callback_draft_revision",
          "Change it to ask about weekend cover too, but do not send it yet.",
          "route", ("Samir vendor renewal",),
          history="<user_message>Show me the support-coverage reply for Samir's vendor renewal before anything is sent.</user_message>",
          callbacks=(("Samir vendor renewal", VENDOR_DRAFT),),
          rubric="The callback must show Samir's recipient, support-coverage subject and exact body content as a draft, keeping status/confirmation fields outside the email. The follow-up revises the same draft to ask about weekend coverage and explicitly does not send it."),
    _case("x10_panel_only", "selective_callback_publication",
          "Make it more concise.", "route", ("Maya panel invitation",),
          history="<user_message>Show me only Maya's panel invitation now. Keep the lease renewal out of this reply; I will review that separately.</user_message>",
          callbacks=(("Maya panel invitation", PANEL_DRAFT), ("Maya lease renewal", LEASE_DRAFT)),
          sources=("Maya panel invitation",),
          rubric="Publish only the panel draft with its Thursday 15:00 start and 45-minute duration; do not expose the lease terms or present status fields as body text. The follow-up shortens the panel invitation while preserving those facts, with no lease dispatch."),
    _case("x11_survey_no_action", "callback_no_action",
          "Thanks, that is enough. Leave the export as it stands.", "answer",
          history="<user_message>Show me the aggregate result of Tess's staff-survey cleanup, without identifying individual respondents.</user_message>",
          callbacks=(("Tess survey export", json.dumps({"artifact_type": "aggregate_report", "valid_responses": 47, "duplicate_rows_removed": 3, "individual_identities": "not included"}, sort_keys=True)),),
          rubric="The callback conveys 47 valid responses and three duplicate rows removed, without identities or invented findings. The follow-up simply acknowledges leaving the export unchanged: no dispatch, no question, and no claim of additional edits."),
    _case("x12_venue_total", "callback_direct_answer",
          "What is the room hire plus the mandatory cleaning fee for the cheaper venue? Give me the total, and do not reserve anything.", "answer",
          history="<user_message>Show me the final Porto offsite venue quotes, including any mandatory fees.</user_message>",
          callbacks=(("Porto event venues", json.dumps({"artifact_type": "venue_comparison", "currency": "EUR", "venues": [{"name": "Garden Room", "room_hire": 180, "mandatory_cleaning": 35}, {"name": "Bridge Loft", "room_hire": 240, "mandatory_cleaning": 20}], "booking_status": "nothing reserved"}, sort_keys=True)),),
          rubric="Display both quotes and mandatory fees accurately. Answer EUR 215 for Garden Room from the shown figures, with no booking/worker dispatch or unnecessary question. Do not omit the cleaning fee or invent taxes."),
    _case("x13_two_replies", "callback_ambiguous_confirmation",
          "Yes, send the reply.", "ask",
          history="<user_message>Show me both Nico's invoice reply and Samir's support-coverage reply so I can choose which one to send.</user_message>",
          callbacks=(("Nico bakery invoice", INVOICE_DRAFT), ("Samir vendor renewal", VENDOR_DRAFT)),
          rubric="Display both drafts with distinct recipients and correct body content; keep status/confirmation metadata out of their bodies. The follow-up does not identify one, so ask which reply and dispatch nothing. Do not infer a choice from display order."),
    _case("x14_maya_saxophone", "new_subject_known_person",
          "Start a separate search for an adult beginner saxophone teacher for Maya. This is personal, not part of her lease or panel invitation.", "new",
          rubric="Create one separate saxophone-teacher research worker. Do not reuse any Maya lease, panel or receipts worker. Ask for missing location only if necessary during research; do not pretend lessons were booked."),
    _case("x15_omar_budget", "new_work_related_person",
          "Set up a new task for Omar's household moving budget. Keep it separate from the arts-grant writing; I want a list of likely costs before we choose a mover.", "new",
          rubric="Create exactly one moving-budget worker, separate from Omar grant narrative. Request likely cost categories and needed assumptions; no grant rewrite, mover booking or invented confirmed prices."),
    _case("x16_oslo_dinner", "new_subject_existing_city",
          "Open a separate task for a quiet anniversary dinner in Oslo. The flight and hotel plans are already in progress; do not repurpose either of them.", "new",
          rubric="Create one separate anniversary-dinner research worker. Do not reuse flights or hotels, and do not make a restaurant reservation or imply one exists."),
    _case("x17_her_document", "unresolved_person_reference",
          "Send her the updated document.", "ask",
          history="<poke_reply>Rina's onboarding checklist and Tess's survey summary are both ready. Neither has been shared.</poke_reply>",
          rubric="Ask whether the user means Rina's checklist or Tess's survey summary. Do not choose a recipient, dispatch a send instruction, or claim either document was shared."),
    _case("x18_access_tradeoff", "unresolved_access_requirement",
          "Choose whichever museum route works best.", "ask",
          history="<poke_reply>The direct route uses a staircase. The step-free route adds 25 minutes. I do not yet know whether anyone in your group must avoid stairs, and no route has been selected.</poke_reply>",
          rubric="Clarify whether step-free access is required before choosing. Do not infer accessibility needs, dispatch a selection, or silently prefer speed over access."),
    _case("x19_receipt_fact", "answer_visible_fact",
          "Which of Maya's receipts is still missing? Just remind me; do not chase her again.", "answer",
          history="<poke_reply>Maya's reimbursement file has the outbound train receipt. The return train receipt from Sunday 14 December is still missing; she has already been asked for it.</poke_reply>",
          rubric="Answer that the return train receipt for Sunday 14 December is missing. No worker dispatch, renewed reminder, invented amount or unnecessary clarification."),
    _case("x20_return_to_invoice", "explicit_older_topic",
          "Back to BR-241: ask Nico whether the credit for the torn flour sacks appears on that invoice. The supplier software renewal can wait.",
          "route", ("Nico bakery invoice",),
          history="<user_message>Check Samir's software-vendor renewal terms.</user_message><poke_reply>The support-tier questions for Samir are ready for your review.</poke_reply>",
          rubric="Continue only the bakery invoice worker to ask about the torn-sack credit on BR-241. Do not confuse supplier software renewal with the flour invoice or assert the credit already exists."),
)


def fixture_manifest():
    payload = {"schema_version": 1, "workers": WORKERS, "cases": [asdict(case) for case in EXTENSION_CASES]}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {"suite": "extension", "schema_version": 1, "case_count": len(EXTENSION_CASES),
            "groups": {"extension": [case.case_id for case in EXTENSION_CASES]},
            "fixture_sha256": hashlib.sha256(canonical).hexdigest(),
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "independent_holdout": False,
            "note": "Additional hand-authored stress cases after smoke review, using known implementation and existing workers; not an independent holdout."}
