# brn. pipeline: agentic sales pipeline

An AI sales agent for brn dynamics. It finds leads, researches and scores them, runs a multi-channel
sequence (email + AI phone calls + follow-ups), reads replies, books meetings, and logs every action
to a dashboard. Starts in **dry-run**: nothing is sent until you switch modes.

## Run it

```bash
pip install -r requirements.txt
python seed.py                       # optional: 13 fictional sample leads
uvicorn app.main:app --port 8000     # open http://localhost:8000
```
Click **Simulate 14 days** to watch a full sequence play out on sample data.

To connect your mailbox locally, copy `.env.example` to `.env` and fill it in (the app loads it on start).

## Mailbox

The **Mailbox** page is a mail client for the connected account, so you don't need to open webmail:

* **Inbox, Sent, Drafts, Spam, Trash, Archive** and any other folders, read live from the mail server over IMAP.
  Nothing is copied into the database, so webmail and this page always show the same thing.
* **Compose, reply, reply all, forward**, with attachments. Drafts save themselves to the Drafts folder as you type.
  These emails are sent straight away as plain emails, whatever the send mode.
* Search, unread and starred filters, mark read/unread, star, archive, spam, delete (select several at once).
* Every email sent from here or by the agent is copied to the Sent folder.
* **Lead conversations**: every email sent to a lead and every reply from them as one thread. Replies are picked up
  every 5 minutes (or with **Check mail**), including ones already opened in webmail and ones sent from a different
  address in answer to the agent's email. The agent reading a reply does not mark it as read.

It needs `IMAP_HOST` (plus `SMTP_USER` / `SMTP_PASSWORD`, or `IMAP_USER` / `IMAP_PASSWORD`). On a public server the
folders stay locked until `BRN_ADMIN_PASSWORD` is set: without a password, anyone with the link could read and send
your mail.

## How the agent works

```
Lead sources ──► Research + score (Claude) ──► Qualified? ──no──► disqualified
 (CSV, Google                                      │yes
  Places, Apollo)                                  ▼
                 Day 0 email ► Day 3 follow-up ► Day 5 AI call ► Day 9 email ► Day 14 last note
                        │ every step passes the compliance guard first │
                        ▼                                               ▼
              Reply / call outcome ──► Claude classifies ──► meeting_booked · replied (hand to human)
                                                             nurture · lost · opted_out (suppressed)
```

* `app/agent.py`: orchestrator and state machine (runs every minute)
* `app/compliance.py`: opt-out list, lawful basis, local business hours (Sun–Thu KSA/Kuwait, calls 10–17), daily caps
* `app/providers/llm.py`: Claude for research, scoring, emails, call briefs, reply triage (templates if no key)
* `app/providers/channels.py`: email from your own mailbox (SMTP) or Resend, Vapi voice calls
* `app/providers/inbox.py`: reads replies from your mailbox (IMAP) every 5 minutes, records them once and lets the agent act on them
* `app/providers/mailbox.py`: the Mailbox page's IMAP client (folders, reading, drafts, moving, Sent copies)
* `app/providers/sources.py`: CSV import, Google Places search
* `app/static/index.html`: dashboard (overview, leads, approvals, activity log, settings)

## Send modes

| Mode | What happens |
|---|---|
| dry_run | Everything is drafted and logged; nothing leaves the system |
| approval | Every email and call waits in **Approvals** for a human click (edit before sending) |
| live | Sends automatically within daily caps. Locked until the server sets `BRN_ALLOW_LIVE=1` |

## Going live checklist

1. Sending domain separate from brndynamics.com (e.g. `outreach.brndynamics.com`) with SPF, DKIM, DMARC; warm it up for 2–3 weeks at low volume.
2. Vapi account, a KSA/Kuwait-capable caller number, an assistant with an Arabic + English voice, server URL set to `/webhooks/voice`.
3. Legal review for KSA (PDPL, CST telemarketing rules), Kuwait (CITRA) and GDPR for any EU contacts. Record a lawful basis per lead.
4. Run 2 weeks in **approval** mode, review every draft, then raise caps and move to **live**.
5. Set `BRN_ADMIN_PASSWORD` and deploy behind HTTPS (Docker image included). For scale, move SQLite to Postgres.

## Roadmap
Inbound email sync (Gmail/IMAP) to auto-ingest replies · WhatsApp Business follow-ups · Apollo enrichment for decision-makers ·
calendar booking (Cal.com) · CRM sync (HubSpot) · multi-tenant workspaces to sell it as a product.
