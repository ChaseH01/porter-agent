---
name: migrate
description: Transform any Airtable CRM export CSV into two HubSpot-ready files — hubspot_contacts.csv and hubspot_deals.csv. Invoke when the user wants to run the CRM migration or convert Airtable data to HubSpot format.
---

# Migrate Skill — Airtable → HubSpot

When invoked, run the migration script against the user's Airtable export and report results.

## Steps

0. **Print Porter's intro immediately — before doing anything else.** Output this exact text as your own response (not inside a code block, not as bash output):

```
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║      ██████╗  ██████╗ ██████╗ ████████╗███████╗██████╗        ║
║      ██╔══██╗██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝██╔══██╗       ║
║      ██████╔╝██║   ██║██████╔╝   ██║   █████╗  ██████╔╝       ║
║      ██╔═══╝ ██║   ██║██╔══██╗   ██║   ██╔══╝  ██╔══██╗       ║
║      ██║     ╚██████╔╝██║  ██║   ██║   ███████╗██║  ██║       ║
║      ╚═╝      ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝       ║
║                                                               ║
║                Relay's CRM Migration Agent                    ║
╚═══════════════════════════════════════════════════════════════╝

                         ▗▄▄▄▄▄▄▄▄▄▖
                         ▐▛█████████▜▌
                         ▐█████████████▌
                         ▐██▙▄▖ ▗▄▟██▌
                         ▝▜█████████▛▘
                         ▘▘█▘ ▘█▘
 
  Hey, I'm Porter.

  I take your messy Airtable CRM exports and transform them into
  clean, HubSpot-ready data — so your team can hit the ground
  running instead of spending hours cleaning spreadsheets.

  Here's what I'll do for you today:

    ✦  Normalize 55+ phone formats → E.164 standard
    ✦  Parse and standardize dates across any format
    ✦  Extract address components (street, city, state, zip)
    ✦  Detect email opt-outs so you stay compliant
    ✦  Map every contact to the right lifecycle stage
    ✦  Classify deal stages from your reps' notes
    ✦  Infer contact countries across 250+ country codes
    ✦  Flag data anomalies so nothing slips through

  Let's get your data ready for HubSpot. Starting now...

═══════════════════════════════════════════════════════════════

```

1. **Identify the input file.** Look for a CSV file in the working directory to use as input:
   - If the user specifies a file, use that.
   - If not, look for any `.csv` file in the current directory. If there is only one, use it automatically. If there are multiple, ask the user which one to use.
   - Do not assume a specific filename.

2. **Run the transform:**
   ```
   python3 .claude/skills/migrate/transform.py <input_file> [--output-dir DIR]
   ```

3. **Report the audit summary** from the script output:
   - Rows processed
   - Phones reformatted
   - Dates missing
   - Emails flagged invalid
   - Opt-outs detected
   - Skipped rows (and why)

4. **Confirm both output files were created:**
   - `hubspot_contacts.csv`
   - `hubspot_deals.csv`

5. **Show a preview of each output file.** Read the first 10 data rows of each file and display them as clean markdown tables — one for contacts, one for deals. Include every column from the file — do not omit or truncate any columns, including `hubspot_owner_id`. This lets the user verify the conversion at a glance without opening any files.


6. **Surface items for human review.** After the migration completes, print
   a review queue of any records where Porter made an assumption or lacked
   enough information to act with confidence. These are not errors — they
   are judgment calls that a human should sign off on before importing.

   Flag the following:
   - **Skipped contacts** — rows with no email (cannot be imported without 
     a primary key). A human needs to decide: find the email, or drop them?
   - **Country defaulted to US** — no phone prefix or address to infer from. 
     Is this actually a US contact?
   - **Deal value looks like a typo** — amount is far outside the range of 
     all other deals. Did someone enter $47 instead of $47,000?
   - **Closed Won/Lost with no deal value** — the deal closed but no amount 
     was recorded. Worth pulling from the source before importing.
   - **Closed Won/Lost with no date** — closedate will be blank. Should this 
     block the import or be estimated?
   - **Duplicate emails** — two records share the same email. Which one is 
     the real contact? Should they be merged?

   For each flagged item, print the Record ID, contact name, the issue, and
   what Porter did (defaulted, skipped, estimated). The human decides whether
   to accept Porter's call or correct it before running the import.

   If nothing needs review, print:
   `✓  All records processed with confidence. Ready to import.`

7. **FINAL PRINT STATEMENT: AFTER THE 'ANOMALY REPORT' PRINT OUT OUR PORTER MASCOT TO INDICATE THE END OF A MIGRATION.** The following Porter Mascot should be the very last thing printed after all other processes are complete. Output this exact text as your own response (not inside a code block, not as bash output):
```

                         ▗▄▄▄▄▄▄▄▄▄▖
                         ▐▛█████████▜▌
                         ▐█████████████▌   'Porter out'
                         ▐██▙▄▖ ▗▄▟██▌
                         ▝▜█████████▛▘
                         ▘▘█▘ ▘█▘

```

## Output rules

**Contacts:** A contact row is written for every row in the input CSV, regardless of Status — with one exception: rows with no email address are skipped entirely and listed in the audit summary.

**Deals:** A deal row is only written for contacts whose Status is `In Progress`, `Closed Won`, or `Closed Lost`. Contacts with Status `Cold`, `Nurturing`, or `Hot Lead` produce a contact row only — they do not yet have an active deal to track.

## Requirements

- `pip install anthropic phonenumbers python-dateutil usaddress`
- `ANTHROPIC_API_KEY` must be set in the environment (used only for country inference when a contact has no phone number)

## LLM usage

LLM calls are minimal and targeted — keyword matching is always tried first, and the LLM is only called when no keyword match is found:

| Field | When LLM is called |
|---|---|
| `hs_email_optout` | Notes are non-blank and no opt-out keyword matched |
| `dealstage` | Status is "In Progress" and no deal stage keyword matched in Notes |
| `country` | Phone is blank and country cannot be inferred from address or notes deterministically |
 
All other fields use deterministic logic: `phonenumbers` for country-from-phone, `usaddress` for address parsing, `dateutil` for dates.
