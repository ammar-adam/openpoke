"""Hand-authored routing cases and deterministic distractors.

These cases are a small development suite, not a held-out benchmark. Keep
natural names and topic words: removing them would bias lexical retrieval.
"""

from dataclasses import dataclass


BASELINE_COMMIT = "5b5f635935a64ab37884c025d70abb0ed731c094"

WORKERS = {
    "Email to Alice Q3 budget": "Draft Alice a Q3 budget review email. Draft alice-q3-v2 replaces alice-q3-v1; recipient alice@example.test.",
    "Email to Alice Q4 forecast": "Prepare Alice's Q4 revenue forecast email. This is separate from the Q3 budget review.",
    "Email to Bob supplier contract": "Draft a supplier contract reply to Bob at bob@example.test. Existing draft bob-contract-v1.",
    "Tokyo restaurant search": "Find vegetarian restaurants in Tokyo near Shinjuku for the November trip.",
    "Kyoto hotel search": "Compare Kyoto hotels for the November trip, prioritizing quiet rooms.",
    "API latency investigation": "Investigate slow API responses and p95 request latency on the payments endpoint.",
    "Quarterly board slides": "Prepare quarterly board presentation slides about costs and revenue.",
    "Email to Sarah partnership": "Draft Sarah's partnership terms email; preserve thread sarah-partnership.",
    "Email to Jordan recruiting": "Follow up with Jordan about the recruiting interview schedule.",
    "Email to Alice travel": "Discuss Alice's train travel arrangements for December, separate from budget work.",
}

# These words/IDs deliberately occur only in completed work, not names or requests.
CURRENT_RESULTS = {
    "Email to Alice Q3 budget": "The Helios allocation reconciliation is ready for review.",
    "API latency investigation": "The Borealis retry storm explains the response delay.",
}
OBSERVED_DRAFT_ID = "8f0a7c92"


@dataclass(frozen=True)
class Case:
    case_id: str
    category: str
    history: str
    query: str
    expected_action: str  # route, new, ask, answer
    expected_agents: tuple[str, ...]
    published_agents: tuple[str, ...]
    evidence: str


ALICE_DRAFT = (
    "<user_message>Draft Alice the Q3 budget email.</user_message>\n"
    "<poke_reply>To: alice@example.test\nSubject: Q3 budget review\n"
    "Please review the revised Q3 budget. This is the updated draft. Shall I send it?</poke_reply>"
)

CASES = (
    Case("direct_old", "direct_reference", "", "Continue Tokyo restaurant search and narrow it to vegetarian places near Shinjuku.", "route", ("Tokyo restaurant search",), (), "The user explicitly identifies the existing work by name and topic."),
    Case("published_followup", "continuation", ALICE_DRAFT, "send it", "route", ("Email to Alice Q3 budget",), ("Email to Alice Q3 budget",), "The displayed draft and immediate confirmation refer to Alice's Q3 email."),
    Case("unpublished_interruption", "background_interruption", ALICE_DRAFT + "\n<agent_message>[SUCCESS] Kyoto hotel search: Hotel comparison finished in the background. This result has not been shown.</agent_message>", "yes, send that email", "route", ("Email to Alice Q3 budget",), ("Email to Alice Q3 budget",), "An unshown hotel result does not change which draft the user is confirming."),
    Case("two_alice_tasks", "same_person", "<poke_reply>There are separate Q3 budget and Q4 forecast drafts for Alice.</poke_reply>", "Update Alice's Q4 forecast email to include the revised revenue estimate.", "route", ("Email to Alice Q4 forecast",), ("Email to Alice Q3 budget", "Email to Alice Q4 forecast"), "Q4 forecast distinguishes this request from other Alice work."),
    Case("explicit_older_draft", "focus_override", ALICE_DRAFT + "\n<poke_reply>To: bob@example.test\nSubject: Supplier contract\nHere is Bob's draft.</poke_reply>", "Actually send Alice's Q3 budget email first.", "route", ("Email to Alice Q3 budget",), ("Email to Alice Q3 budget", "Email to Bob supplier contract"), "An explicit older reference overrides recency."),
    Case("cold_topic", "cold_reference", "", "Reopen the payments endpoint latency investigation and check the p95 results.", "route", ("API latency investigation",), (), "The request gives the distinctive subject of an existing investigation."),
    Case("paraphrase", "paraphrase", "", "Diagnose sluggish checkout roundtrips again.", "route", ("API latency investigation",), (), "This paraphrases slow payment requests without shared topic words; lexical discovery may fail and must report that failure."),
    Case("new_known_person", "new_work", "<poke_reply>Alice's existing work covers Q3 budget, Q4 forecast, and train travel.</poke_reply>", "Start a separate task to research birthday gift ideas for Alice. This is unrelated to our existing work.", "new", (), (), "The user explicitly requests separate work about a known person."),
    Case("ambiguous_drafts", "ambiguity", "<poke_reply>I have two drafts ready: Alice's Q3 budget review and Bob's supplier contract. Which one would you like to send?</poke_reply>", "send the email", "ask", (), ("Email to Alice Q3 budget", "Email to Bob supplier contract"), "The user has not resolved the two alternatives; guessing either target is incorrect."),
    Case("multiple_tasks", "multiple_dispatches", "", "Update Alice's Q4 revenue forecast email and separately refresh the Kyoto hotel comparison.", "route", ("Email to Alice Q4 forecast", "Kyoto hotel search"), (), "Two independent existing tasks are explicitly requested; both must be dispatched, without extras."),
    Case("answer_from_history", "no_execution", "<poke_reply>The draft is addressed to alice@example.test with the subject Q3 budget review.</poke_reply>", "What email address is that draft going to? Just tell me; don't send or change anything.", "answer", (), ("Email to Alice Q3 budget",), "The address is already visible; no worker execution is required."),
    Case("different_city", "near_match", "", "Resume the Kyoto hotel search, not the Tokyo restaurant list.", "route", ("Kyoto hotel search",), (), "City and task type distinguish two similar travel workstreams."),
    Case("current_result_words", "current_work", "", "Resume Borealis retry-storm analysis.", "route", ("API latency investigation",), (), "Only the latest completed result contains Borealis and retry storm; names and earlier requests do not."),
    Case("same_person_current_work", "current_work", "", "Continue Helios allocation reconciliation.", "route", ("Email to Alice Q3 budget",), (), "The latest result distinguishes one Alice workstream without relying on Alice's name."),
    Case("exact_observed_reference", "object_reference", "", f"Reopen {OBSERVED_DRAFT_ID}.", "route", ("Email to Alice Q3 budget",), (), "The opaque draft ID comes from a successful structured tool result, not a guessed owner."),
)


def roster_records(scale: int):
    """Nested populations keep targets fixed while adding similar distractors."""
    if scale < len(WORKERS):
        raise ValueError(f"scale must be at least {len(WORKERS)}")
    yield from WORKERS.items()
    topics = ("budget", "forecast", "travel", "supplier contract", "restaurant", "latency")
    for index in range(scale - len(WORKERS)):
        topic = topics[index % len(topics)]
        if index % 5 == 0:
            # Repeated people and task types prevent person/category lookup from
            # pretending to solve the hundreds-of-email-workers problem.
            yield f"Email to Alice project {index:05d}", f"Prepare Alice's {topic} email draft for project {index:05d}."
            continue
        person = f"Contact {index:05d}"
        yield f"{person} {topic} project", f"Handle {topic} work for {person}; project reference project-{index:05d}."
