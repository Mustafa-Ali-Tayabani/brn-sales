# brn. pipeline: agentic sales pipeline

An AI sales agent for brn dynamics. It finds leads, researches and scores them, runs a multi-channel
sequence (email + AI phone calls + follow-ups), reads replies, books meetings, and logs every action
to a dashboard. Starts in **approval** mode: nothing is sent until you approve it.

## Run it

```bash
pip install -r requirements.txt
python seed.py                       # optional: 13 fictional sample leads
uvicorn app.main:app --port 8000     # open http://localhost:8000
```

To connect your mailbox locally, copy `.env.example` to `.env` and fill it in (the app loads it on start).

## Mailbox

The **Mailbox** page is a mail client for the connected account, so you don't need to open webmail:

* **Inbox, Sent, Drafts, Spam, Trash, Archive** and any other folders, read live from the mail server over IMAP.
  Nothing is copied into the database, so webmail and this page always show the same thing.
* **Compose, reply, reply all, forward**, with attachments. Drafts save themselves to the Drafts folder as you type.
  These emails are sent straight away as plain emails, whatever the send mode.
* A forwarded email keeps its original look (the branded template, pictures and all); your note goes above it.
* Search, unread and starred filters, mark read/unread, star, archive, spam, delete (select several at once).
* Every email sent from here or by the agent is copied to the Sent folder.
* **Lead conversations**: every email sent to a lead and every reply from them as one thread. Replies are picked up
  every 5 minutes (or with **Check mail**), including ones already opened in webmail and ones sent from a different
  address in answer to the agent's email. The agent reading a reply does not mark it as read.

**Answers to replies.** When a lead replies, the agent drafts an answer (Claude, or a template without a key) and leaves
it under **Approvals > Replies to leads**. It is sent, as a plain email in the same thread, only when you approve it,
in either send mode. Replies asking to stop, or saying no, get no answer. A reply also cancels any follow-up of the
sequence that was still waiting for approval.

It needs `IMAP_HOST` (plus `SMTP_USER` / `SMTP_PASSWORD`, or `IMAP_USER` / `IMAP_PASSWORD`). On a public server the
folders stay locked until `BRN_ADMIN_PASSWORD` is set: without a password, anyone with the link could read and send
your mail.

## Logo in emails

Branded emails carry the brn dynamics logo, loaded from this server at `/brand/logo-light.png` (public, no login). The
server's address comes from `RAILWAY_PUBLIC_DOMAIN` (set by Railway) or `BRN_PUBLIC_URL`; Settings shows which address is
used, and takes a different logo address if you host the image elsewhere. With no public address, the name is written as text.

## Find a person (name + LinkedIn -> email and phone)

**Find a person** (top bar) takes a name and a LinkedIn profile link, one person per line, and adds each as a lead with
their work email and phone number. It uses Apollo's People Enrichment API (`APOLLO_API_KEY`); LinkedIn itself is never
opened or scraped. Emails come back at once; mobile numbers take Apollo a few minutes and are added to the lead when
they arrive. People already in the system are not looked up again. A lead with a name but no email or phone also gets
a **Find email & phone** button. Costs Apollo credits (about 1 per person found, about 8 more per mobile number).

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
* `app/providers/enrich.py`: contact finder (Apollo): name + LinkedIn profile to work email and phone
* `app/providers/sources.py`: CSV import, Google Places search
* **+ Add lead** (top bar) adds one lead by hand; **Import leads** takes a file; **Find a person** looks one up
* `app/static/index.html`: dashboard (overview, leads, approvals, activity log, settings)

## Send modes

| Mode | What happens |
|---|---|
| approval | Every email and call waits in **Approvals** for a human click (edit first, approve one by one or in bulk), then really goes out |
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
