import datetime
import re

from openai import RateLimitError

from cs329z_hw1.llm import LM, BudgetExceeded, OutputTruncated
from cs329z_hw1.types import Email, LMCallable

SYSTEM_PROMPT = """You triage email for its recipient, at the time it was sent.
Labels:
urgent: asks the recipient to act, or reports a live problem they own, needed
  today, next working day, or by a deadline at most two working days away.
normal: written for this recipient or a small group, no deadline within two
  working days (updates, questions, documents, scheduling, personal notes).
ignore: advertising, newsletters, large-list announcements, automated notices
  that ask nothing of the recipient.
Wording does not decide the label: a mass mailing saying "act now" is ignore;
"URGENT" in the subject with no request is not urgent; an automated notice that
asks the recipient to act is normal, or urgent if the deadline is close.
Reply in exactly this format:
LABEL: urgent|normal|ignore
REASON: one sentence"""

# each category has to be a single word
_LABEL = re.compile(r"\b(urgent|normal|ignore)\b")


def keep_ends(s: str, maxlen: int = 4001) -> str:
    n = len(s)

    if maxlen >= n:
        return s

    left = maxlen // 2
    right = maxlen - left

    return s[:left] + "\n[... truncated ...]\n"  + s[-right:]

def day_of_week(iso):
    try:
        dow= datetime.date.fromisoformat(iso[:10]).strftime("%A")
    except (TypeError, ValueError):
        dow = ''
    return f'{dow}, {iso}'
    
def build_user_prompt(email):
    # truncate body if too long
    email_body = email.get("body") or ""
    email_body = keep_ends(email_body)

    email_date =day_of_week(email.get('date'))
    user_prompt = (
        f"From: {email.get('from')}\n"
            f"To: {', '.join(email.get('to') or [])}\n"
            f"Date: {email_date}\n"
            f"Subject: {email.get('subject') or '(none)'}\n\n"
            f"{email_body}")
    return user_prompt

import threading, time, random
# from cs329z_hw1.llm import LM, BudgetExceeded, OutputTruncated

class _Pacer:
    def __init__(self, per_minute):
        self.gap = 60.0 / per_minute
        self.lock = threading.Lock()
        self.next = 0.0
    def wait(self):
        with self.lock:                       # reserve a slot under the lock...
            now = time.monotonic()
            start = max(now, self.next)
            self.next = start + self.gap
        time.sleep(max(0.0, start - now))     # ...sleep outside it

_PACER = _Pacer(per_minute=18)                # a little under 20

def call_model(lm: LMCallable, messages: list[dict], max_retries: int = 3) -> str:
    for attempt in range(max_retries + 1):
        if isinstance(lm, LM):
            _PACER.wait()
        try:
            return lm(messages) or ""
        except RateLimitError as e:
            if attempt == max_retries:
                raise e
            print(f"Rate limited (attempt {attempt + 1}), sleeping: {str(e)[:120]}")
            time.sleep(20 + random.random() * 10)
    return "unreachable"

def error_reason(desc, email, ex):
    detail = " ".join(str(ex).split())[:200]   # collapse newlines so the reason stays one line
    return f'{desc}, email={email.get("id")}, {type(ex).__name__}: {detail}'

def prioritize(email: Email, lm: LMCallable):
    try:
        content = build_user_prompt(email)
    except Exception as e:
        return {"category": "unknown", "reason": error_reason('Prompt build failed', email, e)}
    
    try:
        reply = call_model(
            lm,  [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content} ]
        ) 
    
    except BudgetExceeded as e:
        reason = error_reason(f'Budget error - adjust CS329Z_BUDGET_USD', email, e)
        print(reason)
        raise e
    
    except OutputTruncated as e:
        return {"category": "unknown", "reason": error_reason(f"Max tokens exceeded &&{e.text}&&", email, e)}
    
    
    except Exception as e:
        return {"category": "unknown", "reason": error_reason("Model call failed", email, e)}

    # will search for label burried in text in first non-empty line
    first_line = next((l for l in reply.splitlines() if l.strip()), "")

    # we need the 'or' part for case llm ignores our format instruction
    m = re.search(r"label:\s*(urgent|normal|ignore)", reply, re.IGNORECASE) or _LABEL.search(first_line.lower())
    cat = m.group(1).lower() if m else "unknown"

    r = re.search(r"reason:\s*(.+)", reply, re.IGNORECASE | re.S)
    reason = (r.group(1) if r else reply).strip() or "No reason given."

    return {"category": cat, "reason": reason}