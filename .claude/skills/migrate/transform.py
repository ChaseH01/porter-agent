#!/usr/bin/env python3
"""
Airtable → HubSpot migration transform.
Usage: python transform.py [input.csv] [--output-dir DIR]

Address parsing uses usaddress. Country-from-phone uses phonenumbers.
LLM (claude-haiku) is used as a fallback for opt-out detection, deal stage
classification, and country inference when deterministic methods don't match.
"""

import argparse
import csv
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import os

import anthropic
import phonenumbers
import usaddress
from dateutil import parser as dateutil_parser

# ── Config ────────────────────────────────────────────────────────────────────

INPUT_FILE   = "Airtable_relay_export.csv"
CONTACTS_OUT = "hubspot_contacts.csv"
DEALS_OUT    = "hubspot_deals.csv"

DEAL_STAGE_PROBABILITY = {
    "appointmentscheduled":   0.10,
    "qualifiedtobuy":         0.20,
    "presentationscheduled":  0.40,
    "decisionmakerbroughtin": 0.60,
    "contractsent":           0.80,
    "closedwon":              1.00,
    "closedlost":             0.00,
}

# ── Matrix Maps ───────────────────────────────────────────────────────────────

def get_lifecyclestage(status: str, lead_source: str) -> str:
    s  = status.strip()
    ls = lead_source.strip()
    if s == "Cold":
        return "subscriber" if ls == "Email Newsletter" else "lead"
    if s == "Nurturing":   return "marketingqualifiedlead"
    if s == "Hot Lead":    return "salesqualifiedlead"
    if s == "In Progress": return "opportunity"
    if s == "Closed Won":  return "evangelist" if ls == "Referral" else "customer"
    if s == "Closed Lost": return "other"
    return "lead"

def get_hs_lead_status(status: str) -> str:
    return {
        "Cold":        "OPEN",
        "Nurturing":   "IN_PROGRESS",
        "Hot Lead":    "CONNECTED",
        "In Progress": "OPEN_DEAL",
        "Closed Lost": "UNQUALIFIED",
        "Closed Won":  "",
        "":            "NEW",
    }.get(status.strip(), "NEW")

def get_hs_analytics_source(lead_source: str) -> str:
    return {
        "Outbound":         "OFFLINE",
        "Referral":         "REFERRALS",
        "LinkedIn":         "SOCIAL_MEDIA",
        "Conference":       "OFFLINE",
        "Event":            "OFFLINE",
        "Email Newsletter": "EMAIL_MARKETING",
        "Email Campaign":   "EMAIL_MARKETING",
        "Inbound":          "DIRECT_TRAFFIC",
    }.get(lead_source.strip(), "")

# ── Name ──────────────────────────────────────────────────────────────────────

def split_name(full_name: str) -> tuple[str, str]:
    if not full_name or not full_name.strip():
        return "", ""
    normalized = full_name.strip().title()
    parts = normalized.rsplit(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")

# ── Email ─────────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

def validate_email(raw: str) -> tuple[str, bool]:
    if not raw or not raw.strip():
        return "", False
    email = raw.strip().lower()
    return email, bool(_EMAIL_RE.match(email))

# ── Phone ─────────────────────────────────────────────────────────────────────

def normalize_phone(raw: str) -> tuple[str, bool]:
    if not raw or not raw.strip():
        return "", False
    original = raw.strip()
    has_plus = original.startswith("+")
    digits   = re.sub(r'\D', '', original)
    if not digits:
        return "", False
    if has_plus:
        e164 = "+" + digits
    elif len(digits) == 10:
        e164 = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        e164 = "+" + digits
    else:
        e164 = "+" + digits
    return e164, (e164 != original)

def country_from_phone(phone_e164: str) -> str:
    """ISO 3166-1 alpha-2 country from E.164 number via Google's libphonenumber."""
    if not phone_e164:
        return ""
    try:
        parsed = phonenumbers.parse(phone_e164)
        return phonenumbers.region_code_for_number(parsed) or ""
    except phonenumbers.NumberParseException:
        return ""

# ── Country inference fallback (LLM) ─────────────────────────────────────────

def infer_country_from_text(address: str, notes: str) -> str:
    """
    Called only when phone is blank. Uses an LLM to infer ISO 3166-1 alpha-2
    country from address and/or notes. Defaults to US if key is missing or
    the model can't determine the country.
    """
    context = f"Address: {address}\nNotes: {notes}".strip()
    if not context or context == "Address: \nNotes:":
        return ""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("\n  WARNING: ANTHROPIC_API_KEY not set — cannot infer country for "
              f"contact with no phone. Leaving blank. Context: {context[:80]}")
        return ""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content":
                f"Based on the following CRM contact details, what country is this "
                f"person from? Reply with ONLY the ISO 3166-1 alpha-2 country code "
                f"(e.g. US, GB, DE). If you cannot determine the country with "
                f"reasonable confidence, reply with exactly: UNKNOWN\n\n{context}"
            }],
        )
        code = response.content[0].text.strip().upper()
        if re.match(r'^[A-Z]{2}$', code) and code != "UN":
            return code
    except Exception:
        pass
    return ""

# ── Date ──────────────────────────────────────────────────────────────────────

def parse_date(raw: str) -> str:
    if not raw or not raw.strip():
        return ""
    try:
        return dateutil_parser.parse(raw.strip(), dayfirst=False).strftime("%Y-%m-%d")
    except Exception:
        return ""

# ── Deal Value ────────────────────────────────────────────────────────────────

def parse_amount(raw: str) -> float | None:
    if not raw or not raw.strip():
        return None
    cleaned = re.sub(r'[$,\s]', '', raw.strip())
    try:
        return float(cleaned)
    except ValueError:
        return None

# ── Address parsing (usaddress, no LLM) ──────────────────────────────────────

def parse_address(raw: str) -> dict:
    """
    Parse a raw address string into components using usaddress.
    Falls back to a simple comma-split for addresses that confuse the tagger
    (common with international formats).
    """
    empty = {"street": "", "city": "", "state": "", "zip": ""}
    if not raw or not raw.strip():
        return empty

    try:
        tagged, _ = usaddress.tag(raw.strip())
        street = " ".join(filter(None, [
            tagged.get("AddressNumber", ""),
            tagged.get("StreetNamePreDirectional", ""),
            tagged.get("StreetName", ""),
            tagged.get("StreetNamePostType", ""),
            tagged.get("StreetNamePostDirectional", ""),
        ])).strip()
        return {
            "street": street,
            "city":   tagged.get("PlaceName", ""),
            "state":  tagged.get("StateName", ""),
            "zip":    tagged.get("ZipCode", ""),
        }
    except usaddress.RepeatedLabelError:
        pass
    except Exception:
        pass

    # Simple comma-split fallback (works reasonably for international addresses)
    parts = [p.strip() for p in raw.split(",")]
    return {
        "street": parts[0] if len(parts) > 0 else "",
        "city":   parts[1] if len(parts) > 1 else "",
        "state":  "",
        "zip":    "",
    }

# ── Shared LLM helper ────────────────────────────────────────────────────────

def _llm(prompt: str, max_tokens: int = 20) -> str | None:
    """
    Single-use LLM call via Haiku. Returns None if API key is missing or
    call fails — callers should handle None as 'no signal from LLM'.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception:
        return None

# ── Opt-out detection (keyword first, LLM fallback) ──────────────────────────

_OPTOUT_RE = re.compile(
    r'\b(bounced|unsubscribed?|opt[\s-]?out|opted[\s-]?out'
    r'|do not contact|remove from list)\b',
    re.IGNORECASE,
)

def detect_optout(notes: str) -> bool:
    """
    Returns True if the contact has opted out or their email has bounced.
    Keyword match is tried first. If no match, the LLM reads the note for
    intent — catching phrasings like 'please stop reaching out' that keywords
    would miss. Defaults to False if notes are blank or LLM is unavailable.
    """
    if not notes or not notes.strip():
        return False
    if _OPTOUT_RE.search(notes):
        return True

    result = _llm(
        f"Does this CRM note indicate the contact has opted out of emails, "
        f"asked not to be contacted, or that their email has bounced? "
        f"Reply with only 'true' or 'false'.\n\nNote: \"{notes}\""
    )
    return result is not None and result.lower() == "true"

# ── Deal stage classification (keyword first, LLM fallback) ───────────────────

_STAGE_KEYWORDS: list[tuple[list[str], str]] = [
    (["contract", "proposal", "dpa", "legal review"], "contractsent"),
    (["trial", "poc", "pilot", "evaluat", "testing"], "decisionmakerbroughtin"),
    (["demo", "presentation"],                         "presentationscheduled"),
    (["schedule", "meeting"],                          "appointmentscheduled"),
]

_VALID_STAGES = set(DEAL_STAGE_PROBABILITY.keys()) - {"closedwon", "closedlost"}

def get_dealstage(notes: str) -> str:
    """
    Classifies an In Progress deal into a HubSpot deal stage.
    Keyword match is tried first. If no match, the LLM interprets the note
    for intent — catching phrasings keyword lists would miss.
    Falls back to 'qualifiedtobuy' if notes are blank or LLM is unavailable.
    """
    if not notes or not notes.strip():
        return "qualifiedtobuy"

    lower = notes.lower()
    for keywords, stage in _STAGE_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return stage

    result = _llm(
        f"Classify this CRM deal note into exactly one of these HubSpot deal stages:\n"
        f"- appointmentscheduled (scheduling a meeting)\n"
        f"- presentationscheduled (demo or presentation)\n"
        f"- decisionmakerbroughtin (trial, POC, pilot, evaluation)\n"
        f"- contractsent (contract, proposal, legal review, procurement)\n"
        f"- qualifiedtobuy (general progress or unclear)\n\n"
        f"Note: \"{notes}\"\n\n"
        f"Reply with only the stage name.",
        max_tokens=30,
    )
    if result and result.lower() in _VALID_STAGES:
        return result.lower()
    return "qualifiedtobuy"

# ── Main Transform ────────────────────────────────────────────────────────────

DEAL_STATUSES = {"In Progress", "Closed Won", "Closed Lost"}
EARLY_STAGE_DEAL_STATUSES = {"Hot Lead", "Nurturing"}
EARLY_STAGE_DEALSTAGE = {
    "Hot Lead":  "qualifiedtobuy",
    "Nurturing": "appointmentscheduled",
}

def transform(input_path: str = INPUT_FILE, output_dir: str = ".") -> None:
    out = Path(output_dir)

    rows_processed     = 0
    phones_reformatted = 0
    dates_missing      = 0
    emails_flagged     = 0
    optouts_detected   = 0
    skipped:  list[str] = []

    contacts: list[dict] = []
    deals:    list[dict] = []

    with open(input_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    print(f"Processing {total} rows from {input_path}...")

    for i, row in enumerate(rows, 1):
        print(f"  [{i}/{total}] {row.get('Full Name', '').strip():<30}", end="\r", flush=True)
        rows_processed += 1

        raw_name    = row.get("Full Name", "")
        raw_email   = row.get("Email", "")
        raw_phone   = row.get("Phone", "")
        raw_company = row.get("Company", "")
        raw_title   = row.get("Title", "")
        raw_status  = row.get("Status", "")
        raw_date    = row.get("Last Contact Date", "")
        raw_value   = row.get("Deal Value", "")
        raw_source  = row.get("Lead Source", "")
        raw_address = row.get("Address", "")
        raw_tags    = row.get("Tags", "").strip().strip('"')
        raw_notes   = row.get("Notes", "")

        firstname, lastname = split_name(raw_name)

        email, email_ok = validate_email(raw_email)
        if not email:
            skipped.append(f"{raw_name.strip() or '(no name)'} — missing email")
            continue
        if not email_ok:
            emails_flagged += 1

        phone_e164, reformatted = normalize_phone(raw_phone)
        if reformatted:
            phones_reformatted += 1

        last_contacted = parse_date(raw_date)
        if not last_contacted:
            dates_missing += 1

        amount = parse_amount(raw_value)

        status         = raw_status.strip()
        lifecyclestage = get_lifecyclestage(status, raw_source)
        hs_lead_status = get_hs_lead_status(status)
        analytics_src  = get_hs_analytics_source(raw_source)

        addr = parse_address(raw_address)

        country = country_from_phone(phone_e164)
        if not country:
            country = infer_country_from_text(raw_address, raw_notes)

        optout = detect_optout(raw_notes)
        if optout:
            optouts_detected += 1

        contacts.append({
            "email":                email,
            "firstname":            firstname,
            "lastname":             lastname,
            "phone":                phone_e164,
            "company":              raw_company.strip(),
            "jobtitle":             raw_title.strip(),
            "lifecyclestage":       lifecyclestage,
            "hs_lead_status":       hs_lead_status,
            "address":              addr["street"],
            "city":                 addr["city"],
            "state":                addr["state"],
            "zip":                  addr["zip"],
            "country":              country,
            "notes_last_contacted": last_contacted,
            "hs_analytics_source":  analytics_src,
            "hs_email_optout":      "TRUE" if optout else "FALSE",
        })

        makes_deal = (status in DEAL_STATUSES) or (
            status in EARLY_STAGE_DEAL_STATUSES and amount is not None and amount > 0
        )

        if makes_deal:
            if status == "Closed Won":
                dealstage = "closedwon"
            elif status == "Closed Lost":
                dealstage = "closedlost"
            elif status in EARLY_STAGE_DEAL_STATUSES:
                dealstage = EARLY_STAGE_DEALSTAGE[status]
            else:
                dealstage = get_dealstage(raw_notes)

            if status in ("Closed Won", "Closed Lost"):
                closedate = last_contacted
            elif last_contacted:
                base      = datetime.strptime(last_contacted, "%Y-%m-%d")
                closedate = (base + timedelta(days=90)).strftime("%Y-%m-%d")
            else:
                closedate = ""

            year     = last_contacted[:4] if last_contacted else "Unknown"
            dealname = f"{raw_company.strip()} — {raw_tags} — {year} — {lastname}"

            deals.append({
                "dealname":                  dealname,
                "amount":                    "" if amount is None else amount,
                "dealstage":                 dealstage,
                "closedate":                 closedate,
                "pipeline":                  "default",
                "hubspot_owner_id":          "99999",
                "hs_deal_stage_probability": DEAL_STAGE_PROBABILITY.get(dealstage, ""),
                "associated_contact_email":  email,
            })

    print()

    if not contacts:
        print("WARNING: No contact rows produced. Check your input file.", file=sys.stderr)
        sys.exit(1)
    if not deals:
        print("WARNING: No deal rows produced. No rows matched In Progress / Closed Won / Closed Lost.")

    contacts_path = out / CONTACTS_OUT
    deals_path    = out / DEALS_OUT

    with open(contacts_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=contacts[0].keys())
        w.writeheader()
        w.writerows(contacts)

    if deals:
        with open(deals_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=deals[0].keys())
            w.writeheader()
            w.writerows(deals)

    print()
    print("=" * 52)
    print("  MIGRATION AUDIT SUMMARY")
    print("=" * 52)
    print(f"  Rows processed        {rows_processed}")
    print(f"  Phones reformatted    {phones_reformatted}")
    print(f"  Dates missing         {dates_missing}")
    print(f"  Emails flagged        {emails_flagged}")
    print(f"  Opt-outs detected     {optouts_detected}")
    print(f"  Skipped (no email)    {len(skipped)}")
    print("=" * 52)
    if skipped:
        print("  SKIPPED ROWS:")
        for s in skipped:
            print(f"    • {s}")
    print(f"  → {contacts_path}  ({len(contacts)} contacts)")
    if deals:
        print(f"  → {deals_path}  ({len(deals)} deals)")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Airtable → HubSpot CRM migration")
    ap.add_argument("input", nargs="?", default=INPUT_FILE)
    ap.add_argument("--output-dir", default=".")
    args = ap.parse_args()
    transform(args.input, args.output_dir)
