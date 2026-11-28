from dataclasses import dataclass
import json
import re

from cs329z_hw1.email_priority import keep_ends, prioritize
from cs329z_hw1.types import Email, LMCallable

LIMIT = 300
TARGET = 240   # ~60 words of slack under 300
URGENT_CAP_MAX = 35
NORMAL_CAP = 12

SYSTEM_PROMPT = """You write the morning email digest for a busy employee.

INPUT
The user message starts with word-budget instructions, followed by email threads. Each thread begins with a line like:
[17] URGENT | Subject | 2 email(s)
followed by the messages in that thread, oldest first. URGENT threads need the employee's action soon. OTHER threads are routine.

TASK
Write one entry per thread, following the counts and word limits in the instructions at the top of the user message. 
Those limits are strict: a digest that runs over is a failure.

URGENT threads
- Write one entry for every URGENT thread, in input order. Never skip or merge any.
- Each entry states what is needed, from whom, and the deadline if there is one.
- Always name the company, project, site, or counterparty involved, exactly as written in the email. Use the thread subject if the message bodies do not name it.

OTHER threads
- Rank them: requests and decisions first, social notes and FYIs last.
- Write each as the user message instructs: a short one-line summary, a subject only, or omitted with a closing line.

STYLE AND ACCURACY
- Use only facts stated in the emails. Do not guess, infer, or add advice. Copy dates, numbers, and deadlines exactly.
- Write plain text. Do not mention the tags URGENT or OTHER, the bracketed thread numbers, or this task.
- Refer to people by the names or addresses given in the "From" lines.

OUTPUT
Return only a JSON object, with no code fences and no other text:
{"urgent_items": ["..."], "other_items": ["..."]}
urgent_items has one string per URGENT thread, in input order.
other_items has the OTHER entries in ranked order. If the instructions ask for a closing line about omitted updates, make it the last string.
Use an empty list when there are no threads of that kind."""

SUMMARY_SHARE = 0.6         # share of the leftover room spent on full summaries.. if close to 1 then most of it, and many will be dropped
SUBJECT_COST = 7            # typical subject length?
CLOSING_COST = 6            # "... and {dropped} other routine updates"

def budgets(n_urgent, n_normal):
    urgent_cap = min(URGENT_CAP_MAX, TARGET // max(n_urgent, 1))    # words per urgent ite
    room = TARGET - n_urgent * urgent_cap                           # how much room for non-urgent

    if n_normal * NORMAL_CAP <= room:                               # everything fits as a summary
        return urgent_cap, n_normal, 0, 0

    room = max(room - CLOSING_COST, 0)                              # else a closing line will be needed
    k = min(n_normal, int(SUMMARY_SHARE * room) // NORMAL_CAP)      # how many non-urgent items get full summary
    m = min(n_normal - k, (room - k * NORMAL_CAP) // SUBJECT_COST)  # how many non-urgent items get subject only
    dropped = n_normal - k - m                                      # how many non-urgent items get dropped
    return urgent_cap, k, m, dropped

# def _budgets(n_urgent):
#     urgent_cap = min(URGENT_CAP_MAX, TARGET // max(n_urgent, 1))    # words per urgent item
#     normal_cap = NORMAL_CAP                                         # words per other item
#     k = max(0, (TARGET - n_urgent * urgent_cap) // normal_cap)      # max other items

#     return urgent_cap, normal_cap, k

def augment_user_prompt(n_urgent, n_normal):
    urgent_cap, k, m, dropped = budgets(n_urgent, n_normal)
    parts = [f"Write one item per URGENT thread, at most {urgent_cap} words each."]
    if k > 0 or m > 0:
        parts.append(f"Rank the OTHER threads: requests and decisions first, social notes last. "
                     f"Write the top {k} as one line of at most {NORMAL_CAP} words each. "
                     f"Write the next {m} as the subject only of at most {SUBJECT_COST} words each.")
    if dropped > 0:
        parts.append(f'Omit the rest and end with exactly one line as follows: "... And [{dropped}] other routine updates".')
    msg = " ".join(parts)
    return msg

def clean_sender_name (e: Email):
    return (e.get('from') or '').strip()

@dataclass
class Thread:
    thread_id: str
    category: str #'urgent'| 'other'
    started_date: str # iso date of the the first email
    emails: list[Email]       # oldest first, ignore-labeled emails already removed

    def render_thread(self,) -> str:
        subject = self.clean_subject()          # strip RE:/FW:, fall back for blank
        lines = [f"[{self.thread_id}] {self.category.upper()} | {subject} | {len(self.emails)} email(s)"]
        for e in self.emails:
            body = keep_ends((e.get("body") or "").strip(), 600)
            lines.append(f"From: {clean_sender_name(e)}\n{body}")
        return "\n".join(lines)

    def clean_subject(self):
        # take subject from oldest email that has non-None, non-empty subject
        
        email_with_subject = next((e for e in self.emails if (e.get("subject", "") or '').strip()), None) # None is default value
        subject = email_with_subject.get('subject', '').strip() if email_with_subject is not None else ''

        clean_subject = re.sub(
            r'^(?:(?:RE|FW|FWD):\s*)+',
            '',
            subject,
            flags=re.IGNORECASE,
        )
        return clean_subject if clean_subject else '(No Subject)'

    
    def norm_id(self):                       # "01306", 1306 and " 1306 " all compare equal
        return str(self.thread_id).strip().lstrip("0")


def make_thread(raw_thread_id: str, category: str, related_emails: list[Email]):
    assert len(related_emails) > 0, 'Related emails len'
    assert raw_thread_id is not None and len(raw_thread_id) > 3, 'Thread id must be in form th_<number>'

    thread_id = raw_thread_id[3:]
    sorted_emails = sorted(related_emails, key=lambda x: x.get('date') or 'none')
    started_date = sorted_emails[0].get('date', 'none')

    t = Thread(thread_id, category, started_date, sorted_emails)
    return t

def prep_digest_inputs(emails: list[Email], categories: list):
    """
    First, drop all emails in category 'ignore' and categorize remaining emails as 'urgent'  (for category 'urgent') or 'non-urgent' (for category 'normal' and 'unknown').
    Then group by thread_id and create dict thread_id => list[Email] for the two groups
    If emails of the same thread have mixed category, then categorize them all as urgent
    
    """
    urgent_dd, normal_dd = {}, {}

    for e,c in zip(emails, categories):
        if c == 'ignore':
            continue
        thread_id = e.get('thread_id')
        dd = urgent_dd if c == 'urgent' else normal_dd

        related_emails = dd.get(thread_id, [])
        related_emails.append(e)
        dd[thread_id] = related_emails
       

    # if a thread has a mix of 'urgent' and 'non-urgent', then promote all to 'urgent'
    overlaps = [x for x in normal_dd.keys() if x in urgent_dd.keys()]
    for x in overlaps:
        wrongly_classified = normal_dd.get(x)
        urgent_dd[x] += wrongly_classified
        del normal_dd[x]
    urgents = [make_thread(th_id, 'urgent', emails) for th_id, emails in urgent_dd.items()]
    normals = [make_thread(th_id, 'normal', emails) for th_id, emails in normal_dd.items()]    
    return urgents + normals, len(urgents), len(normals)

def daily_digest(emails: list[Email], lm: LMCallable) :
    """Problem (daily_digest).

    ``emails`` is a list of emails from one calendar day (UTC date); it may
    be empty. Return the morning summary as a string.

    Rules the tests check:
    * Returns a string for every model reply, and for an empty day.
    * The function does not modify ``emails``.
    * Live (tests/fixtures/digest_days.json): the digest is not empty and
      at most 300 words (``len(digest.split())``) as the model wrote it; for
      each gold urgent email it contains the name of the company, project or
      site that email is about; it contains no term listed for the gold
      ignore emails.
    """
    categories = [prioritize(em, lm) for em in emails] 
    all_email_threads, n_urgent, n_normal = prep_digest_inputs(emails, [c.get('category') for c in categories])
    
    old_user_messages = [{'role': 'user', 'content': t.render_thread()} for t in all_email_threads]
    aug = augment_user_prompt(n_urgent, n_normal)
    if len(all_email_threads) > 0:
        usr_msg = {"role": "user", "content": aug + "\n\n" +
            "\n\n".join(t.render_thread() for t in all_email_threads)}
        sys_msg = {"role": "system", "content": SYSTEM_PROMPT}
        messages = [sys_msg, usr_msg]
        digest_json = lm(messages)
        words = digest_json.split()
        print(f' digest length equals {len(words)}')
        tup = parse_digest(digest_json, n_urgent)
        if len(words) > LIMIT:
            # TODO add nudge to the prompt, then retry 
            words = words[:LIMIT]
    else:
        digest_json = str({ 'urgent_items':[],  'other_items':[]})
    

    return str(digest_json)

def parse_json_reply(text: str):
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)   # opening fence, with optional language tag
    t = re.sub(r"\s*```$", "", t)            # closing fence
    return json.loads(t)


def assemble(urgent, others, urgent_text, other_text, m):
    lines = [urgent_text.get(t.norm_id()) or t.clean_subject() for t in urgent]
    
    picked = [t for t in others if t.norm_id() in other_text]       # input order
    lines += [other_text[t.norm_id()] for t in picked]
    rest = [t for t in others if t.norm_id() not in other_text]
    lines += [t.clean_subject() for t in rest[:m]]
    dropped = len(rest) - m
    if dropped > 0:
        lines.append(f"... and {dropped} other routine updates")
    return "\n".join(lines)

def parse_digest(text: str, n_urgent: int) -> tuple[list[str], list[str]] | None:
    """str -> (urgent_items, other_items), or None if the reply is unusable."""
    try:
        data = parse_json_reply(text)          # the fence-stripping helper
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    urgent, other = data.get("urgent_items"), data.get("other_items")
    if not (isinstance(urgent, list) and isinstance(other, list)):
        return None
    if len(urgent) != n_urgent or not all(isinstance(s, str) for s in urgent + other):
        return None
    return urgent, other


