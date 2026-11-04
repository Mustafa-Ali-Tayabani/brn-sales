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
* `app/providers/channels.py`: Resend email, Vapi voice calls
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
