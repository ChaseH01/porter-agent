# Porter — Airtable → HubSpot Migration Agent

Porter is Relay's CRM migration agent. It takes a raw Airtable contact export and transforms it into two clean, import-ready HubSpot files — one for contacts, one for deals — so your team can switch CRMs without spending days cleaning spreadsheets.

---

## What Porter does

| Task | How |
|---|---|
| Normalizes phone numbers | Converts 55+ formats to E.164 standard (`+14155550182`) |
| Parses addresses | Extracts street, city, state, and zip from freeform text |
| Standardizes dates | Handles any date format via `python-dateutil` |
| Maps lifecycle stages | Converts Airtable Status → HubSpot `lifecyclestage` and `hs_lead_status` |
| Classifies deal stages | Keyword match on rep notes, with Claude Haiku as a fallback |
| Detects email opt-outs | Keyword match + LLM for natural language phrasings |
| Infers contact countries | Phone prefix via `libphonenumber`; LLM fallback for contacts with no phone |
| Flags anomalies | Surfaces typos, missing values, and judgment calls for human review |

**Contacts:** Every row in the input produces a contact row, except rows with no email (HubSpot requires email as a primary key).

**Deals:** Contacts with Status `In Progress`, `Closed Won`, or `Closed Lost` automatically produce a deal row.  `Nurturing` and `Hot Lead` contacts only generate HubSpot Deals if there is a dollar amount attached to their Airtable data. `Cold` contacts are tracked as contacts only always.

---

## Prerequisites

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/) (used only for LLM fallbacks — not required for the majority of records)

---

## Setup

**1. Clone the repo**

```bash
git clone https://github.com/ChaseH01/porter-agent.git
cd porter-agent
```

**2. Install dependencies**

```bash
pip install anthropic phonenumbers python-dateutil usaddress
```

**3. Add your API key**

```bash
cp .env.example .env
```

Open `.env` and replace `your-api-key-here` with your Anthropic API key. Then export it before running:

```bash
export ANTHROPIC_API_KEY=$(grep ANTHROPIC_API_KEY .env | cut -d '=' -f2)
```

---

## Output files

| File | Contents |
|---|---|
| `hubspot_contacts.csv` | One row per contact, ready for HubSpot's contact import |
| `hubspot_deals.csv` | One row per active deal, ready for HubSpot's deal import |

### Contact columns

`email`, `firstname`, `lastname`, `phone`, `company`, `jobtitle`, `lifecyclestage`, `hs_lead_status`, `address`, `city`, `state`, `zip`, `country`, `notes_last_contacted`, `hs_analytics_source`, `hs_email_optout`

### Deal columns

`dealname`, `amount`, `dealstage`, `closedate`, `pipeline`, `hubspot_owner_id`, `hs_deal_stage_probability`, `associated_contact_email`

---

## Using Porter as a Claude Code skill

Porter is packaged as an invocable Claude Code skill. Once the repo is in your project's `.claude/skills/` directory, type `/migrate` in Claude Code to run the full migration with an audit report and anomaly review.

---

## Security

- **Never commit your `.env` file.** It is listed in `.gitignore`.
- The `.env.example` file is safe to commit — it contains no real keys.
- Input CSV files (which may contain PII) are also excluded from git via `.gitignore`.
- The LLM is used only as a fallback for three specific fields. All other transforms are fully deterministic and run locally.
