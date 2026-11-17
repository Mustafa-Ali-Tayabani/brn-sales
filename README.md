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

## People and roles

Settings > **People & roles**. Until the first account exists nothing changes (open, or the `BRN_ADMIN_PASSWORD` prompt).
Once you add yourself as the first admin, everyone signs in at `/login` with their own email and password:

| Role | Can do |
|---|---|
| Admin | Everything, including people, roles and settings |
| Manager | Leads (incl. delete and export), approvals, mailbox, meetings, start/stop the agent. No settings or people |
| Sales rep | Add and edit leads, approve emails, mailbox, book meetings. No delete, export or agent control |
| Viewer | Read only: dashboard, leads, pipelines, calendar, activity log. No mailbox |

The table shows the starting point. Under Settings > People & roles you can tick what each role may do and add roles of
your own; admin always has everything.

Every request is checked on the server (`app/auth.py`), the activity log records who did what, and passwords are stored
only as salted hashes. `BRN_ADMIN_PASSWORD` keeps working as a recovery password for admin accounts.

## Finding leads who need it now

A business that fits a profile is not a lead; one with a reason to talk today is. Each lead carries a **buying signal**:

| Signal | Where it comes from |
|---|---|
| Asked for it | Claude searches the web for people who said in public that they need what a pipeline sells. Each result is checked: the page is fetched and the quoted words must be on it, or it is dropped. Reddit posts count too. |
| Wrote to us | Your website's contact form (`POST /api/inbound`, see Settings > Connections) and the booking page. |
| Hiring for it | Companies advertising a job for the skill, read from public job boards (Remotive, RemoteOK, Arbeitnow). |
| Shows the problem | A website scan found faults, or a business has no website. |

Set the searches per pipeline in Settings > Pipelines ("What a buyer says when they need this", "Jobs a company advertises");
**Suggest searches** drafts them from the pitch. The Leads page filters by signal, a lead's page shows the quote and the
link, and Reports compares reply rates by signal.

Two things keep poor leads out. The rule **Skip companies that sell services like ours** reads each company's own home
page and name, and drops software houses, agencies and IT firms (pipelines marked as selling to tech companies are
exempt). And Settings > Agent & sending > **Who the agent contacts** can be set to leads with a signal only: profile
matches from map and web search are then kept under Nurture instead of being emailed. Scoring also now rewards
evidence of a current need over a profile match. Set `BRN_SITE_CHECK=0` to switch off the home-page read.

## Lead quality

Every lead has a **grade**, A to C, from three things shown on its page: is there a sign of need (asked, wrote to us,
hiring, a problem found), is there a named person with their own address, and how well it fits. The agent works the best
grades first, and Settings > Agent & sending can stop it contacting grades below a level.

* **Named contacts.** Before emailing a shared mailbox like info@, the agent looks for the owner or a manager at the
  company and writes to them instead (the shared address is kept). Needs `APOLLO_API_KEY` or `HUNTER_API_KEY`; a daily
  limit protects your credits. Also a button on each lead and a bulk action. Job titles to look for are set in Settings
  and per pipeline.
* **Mailbox check.** With `HUNTER_API_KEY`, the address is verified before the first email; a mailbox that does not
  exist waits under Needs contact instead of bouncing.
* **Clean up leads** (Leads page). Checks every lead the agent could still contact against the rules and for companies
  entered twice, shows what it found, and sets those aside when you agree. Nothing is deleted.
* **Good lead / Bad lead** on each lead. A bad mark sets the lead aside with a reason; when a pattern shows, Settings >
  Rules suggests a rule. A good mark raises the grade, and **Find similar companies** searches for more like it.
* **Reviews as a reason to write.** A pipeline can list words (booking, waiting, phone); map searches then read each
  business's Google reviews and a poor review mentioning one becomes the reason to write. A low rating over many reviews
  counts too.
* **Not now.** A lead who says the timing is wrong gets a check-in drafted for approval after a set number of days.
* **LinkedIn posts.** A pipeline can also search public LinkedIn posts for people asking for what it sells. LinkedIn shows
  no contact details and may not be read by machine, so the author is matched to a contact through Apollo.
* Map results now also get the email each business publishes on its own website.

**Every pipeline searches for the need.** Each built-in pipeline comes with searches for people asking for what it sells
("need an ERP for my business") and, where it fits, for companies hiring for it ("Odoo developer"). A pipeline you saved
earlier gets them only where it had no such list at all; searches you edited or emptied are left alone. Map searches stay
for pipelines that sell to a kind of business (gyms, restaurants): they give the full list, and Find owners gives the
person. The searches use Claude's web search and stop at the daily Claude budget.

**No email to a shared inbox.** Switch on "Hold shared mailboxes for review" under Settings > Rules. The agent then looks
for a named person at every lead that only has info@, sales@ and the like, before the rule is applied. A lead that gets a
person's own address goes ahead; one that does not is paused for you instead of being emailed. It is off by default,
because many small businesses publish nothing but a shared address.

**Claude credit.** Anthropic gives an app no way to read the credit balance, so enter it once under Settings > AI & cost
(and again after a top-up). What is left (the balance minus what the agent has spent since) shows at the bottom of the menu
on every page and turns red under $2.

**Countries.** A lead's country is what is known about it: the country given with it, else its phone number's country
code, else the ending of its web or email address (.pk, .co.uk), else the country a map search was limited to. When
nothing says where it is, it shows as "Country unknown" instead of an assumed one. The Leads table shows the country with
its flag, the Country filter includes Unknown, and Reports has a "By country" table (leads, contacted, replied, meetings,
won). Under Settings > Rules, "Only bring in leads from these countries" takes names or codes ("Pakistan, UAE, GB"):
searches look there first, a lead found elsewhere is not added, and one already in the list is skipped. Leads of unknown
country are let through, and so is any lead a person added by hand. "Never contact leads in these countries" is the
opposite list.

**Where a lead came from.** Each lead carries a "Found via" tag (Google Maps, Asked online, LinkedIn post, LinkedIn
profile, Job ad, Reddit post, Imported file, Added by hand, Website enquiry, Booking page) and, when its contact details
were looked up, a second tag naming who supplied them: Apollo, Hunter or Public pages. The Source filter uses the same
names.

## Apollo as a lead source

With `APOLLO_API_KEY` set (a master key), each pipeline can find its leads in Apollo. Pipelines > Edit > **Apollo search**
holds the filters: industries (Apollo's own names), company size ranges, countries, technologies, keywords, industries to
leave out, the job-ad words that mean a company needs this, and two daily limits. "Preview 10 results" runs the search as
typed without saving a lead. The daily run then, per active pipeline:

1. searches for companies that match (1 credit), and again for the ones with an open job ad matching the hiring words (1 credit);
2. finds the best person at each company by the pipeline's job titles, most wanted first (free);
3. skips companies already in the list or on the do-not-contact list, by web address;
4. fetches the matching job ad for up to five hiring companies (1 credit each): the ad's title and link are the
   "Hiring for it" evidence on the lead, and the email writer can refer to it.

A lead starts with the company, a first name and a job title, because that is all Apollo's free people search returns.
**The email costs a credit and is only fetched when the lead has earned it**: it passes every rule, and would be grade B
or better with the email. Only an address Apollo marks *verified* is used; "guessed" or "unavailable" counts as no email
and the lead waits under Needs contact. A verified address is not checked a second time with Hunter. The reveal also
brings the full name, LinkedIn profile, industry, staff count and country.

**Fit score.** Apollo leads are scored from facts: industry, staff count against the size ranges, country, technology,
a hiring or funding signal, and how good the job title is. A fact Apollo returned counts in full, one only implied by
the search that found the lead counts a little less, and a mismatch costs points, so scores spread out.

**Agencies.** When Apollo knows a company's industry (IT services, software development, marketing & advertising, design
and the like), that decides whether it sells services like ours; the guess from its name or website is only used when
Apollo has no data. The reason is written on the lead.

**Credits.** Every call that costs a credit is counted. Settings > Agent & sending has "Apollo credits per day" (30 by
default, 0 for no limit) across searches, job ads and reveals; each pipeline has its own "emails revealed per day". When a
cap is reached, leads waiting for their email are tried again the next day. Settings > AI & cost shows credits used today
and this month by kind. On an Apollo plan without company search (the free plan), people are searched for directly
instead, which is free; the company's web address then arrives with the reveal.

**Active pipelines.** A pipeline can be switched off (Pipelines > Edit): no new leads are looked for, its leads stay.
Web & mobile apps, New websites, AI & automation, CRM / ERP, Security & compliance and Custom software & AI start
switched off. Job titles: a pipeline's own titles are used first, then the titles that came with its Apollo search,
then the general list in Settings.

Other changes that came with it: addresses scraped from a page's code or examples (bootstrap@5.3.0, you@company.com) are
no longer taken, and existing ones are moved to Needs contact once; when Claude is set up but not answering or over
budget, no outreach email is drafted from the built-in wording (the lead waits, the banner says how many), while answers
to replies still get a short holding draft; out-of-office, ticket, survey and "passed to another department" messages are
filed as automatic messages and not as replies; Reports > By person names who approved each email.

Tests: `python -m unittest discover -s tests -t . -v` (no outside service is called; Apollo is replaced by a stand-in).

## Phones and tablets

The dashboard adapts to the screen. Under 900px wide the menu slides in from a button in a top bar (with the number of
drafts waiting), pop-ups and a lead's page fill the screen, the Leads filters fold behind a Filters button, wide tables
scroll sideways inside their card, and on a phone the Leads table keeps company, score and status only.

## Deleting leads, your own statuses, switching pipelines and web search off

- **Delete**: a lead's page has "Delete lead"; in the Leads list, tick several and press Delete. The lead goes with its
  timeline and drafts; tasks and coming meetings stay, without the lead. Needs the "Delete leads" permission.
- **Statuses of your own**: on a pipeline board, "+ New status" adds a column (for example Proposal sent). Move a lead
  there by dragging it, from "Move to…" on its page, or with "Set status" in the Leads list. A lead in one of your
  statuses waits for you: the agent sends it nothing and does not move it, though a reply from the lead still shows up.
  "edit" on the column renames, recolours or removes it (only when no lead is in it).
- **Pipeline on / off**: each pipeline card has a switch. Off means no new leads are looked for and the agent does no
  research or outreach for that pipeline's leads; they are kept, replies are still read, and drafts already waiting can
  still be approved. Switching it on again carries on where it stopped.
- **Clean-up per pipeline**: "Clean up leads" on a board checks only that pipeline's leads. On the Approvals page,
  drafts can be filtered by pipeline, step and words, and Clean up then covers the pipeline chosen.
- **Web search leads on / off**: Settings > Agent & sending > "Find leads by web search". Off stops the searches for
  people asking online, business web searches and LinkedIn posts; Apollo, Google Maps and job ads carry on.

## Owners and decision-makers

A company with only info@ is half a lead. Each pipeline card shows how many of its live leads have a named person and how
many of those can be reached directly. **Find owners** looks up the owner or the person who runs each company that has
nobody named yet, best leads first: the people finder first (Apollo or Hunter, when connected), then public pages through
Claude's web search (the company site, Google, social profiles, business registers), using the pipeline's job titles. A
name is kept only when a page names that person in that role; when the page can be read and does not show the name, it is
dropped. No email address is ever guessed: the person's own address is used when a page shows it or the people finder has
it, otherwise the company address is kept and the email is addressed to the person. A company where nobody was found is
not searched again for 30 days. With "find a named contact automatically" on in Settings, the agent does the same for good
leads as they come in. Leads can be filtered by "Named person".

## LinkedIn

The **LinkedIn** page holds every lead that came from LinkedIn (people added from their profile, and people found through
a public post saying they need what a pipeline sells) with its own funnel, drop-out reasons, the post and profile links,
and whether contact details were found. "Search LinkedIn posts" runs that search for one pipeline, with its own phrases or
one you type. LinkedIn is never logged into or scraped: only public posts that web search can see are found, and contact
details come from Apollo, since LinkedIn shows none.

## AI phone calls

Settings > Connections > AI phone calls. With `VAPI_API_KEY` set, **Set up the AI caller** creates the caller in your
Vapi account (script, the result it reports, and where it reports to); you choose which of your Vapi numbers it calls from
and can have it phone you to hear it. Calls are placed as the phone step of a sequence, within calling hours and the
daily limit, and after your approval in approval mode. Without it, the agent leaves a call brief for you to phone by hand.

**Call budget**: Settings > "AI call budget per month ($)", 30 by default, 0 for no limit. The month's spend is read from
your Vapi account (all calls on it, test calls included; the call reports are the fallback) and shown on the AI phone
calls card. A call is only placed while at least $1 of the budget is left, with room held for calls still running; after
that, phone steps become call briefs until next month or until you raise the figure. It counts what Vapi bills. A number
imported from Twilio or Telnyx is billed for line time by that carrier, outside this figure. For a hard stop on Vapi's
side as well, buy credit in fixed amounts and leave auto-reload off.

## Email coach

The **Email coach** page is where you tell the agent how your emails should be written, the way you would brief a new
salesperson: instructions for every email, extra ones per pipeline, and up to five emails you like as examples. They are
given to Claude with every outreach email, answer and follow-up. On the same page: **Try it** writes a sample with the
instructions as they stand; **What has worked** lists the subject lines and emails that got answers (one click makes
an email an example); **Ideas from the AI** has Claude read what was sent and how people answered, and propose
instructions to add. In Approvals, each draft has a box to say what to change ("shorter, mention their Google rating"),
with an option to remember it for future emails. All of this needs Claude; built-in wording ignores the instructions.

## Calendar and meetings

The calendar lives in this app, not in Google. The **Calendar** page shows every meeting; book one there, from a lead
(**Schedule meeting**), from a reply waiting in Approvals, or by asking the assistant. Booking for a lead moves it to
"Meeting booked".

**Google Meet without signing in to Google.** Google only lets an app create Meet links after an account is connected, so
instead you paste one or more permanent links in Settings > Meetings & booking (meet.google.com > New meeting > Create a
meeting for later). Each meeting gets one that no other meeting is using at that time. With a single link, meetings at
the same time share a room.

**Invitations** are emailed from your own mailbox with a calendar file (.ics), which Gmail, Outlook and Apple Mail show as
an invitation to add to the calendar. Moving or cancelling a meeting emails an update. Replies to an invitation arrive in
the mailbox like any other email; acceptances are not tracked.

## Rules

Settings > **Rules** decides which leads the agent may work on. Ready-made rules are switched on and off: wrong email
addresses (including typos like gmial.com), domains that cannot receive email, no-reply addresses, throwaway inboxes,
shared mailboxes, free providers, one person per company, missing contact name, blocked countries, blocked words, and a
cap on emails without a reply. You can add your own ("when job title contains student, skip the lead"). Rules are checked
before a lead is researched and before every email. A lead that breaks one waits for a fix, is skipped, or is paused, and
its timeline says why. "Check current leads" shows what the rules would stop right now. Set `BRN_DNS_CHECK=0` to switch
off the domain lookup.

**Companies that sell what we sell never get a customer email.** Agencies, software houses and consultancies are spotted
from their name, website and research. The rule "Never pitch companies that sell services like ours as customers" has two
settings: move them to the partner pipeline (default) or skip them. It is checked when a lead is researched, before every
email, and again when you approve a draft, so a customer pitch written earlier is not sent either.

## Partners (white label)

The **White-label partner (agencies)** pipeline is for agencies and tech partners we work behind: they keep the client,
we deliver under their brand. It has its own pitch, ideal customer and search ("agency looking for a white label
development partner"), and agencies are welcome there instead of being filtered out. An agency found by a customer
pipeline is moved here and starts fresh with the partnership pitch. If it already received a customer email it is moved
but paused, so it does not get a second, different pitch unless you start it by hand. In a pipeline's settings, Kind >
"Partner" makes any pipeline behave this way.

## When the agent runs

Settings > Agent & sending. The agent is on or off, and can be limited to working days and hours in a time zone you pick.
Outside those hours it rests (no research, drafting, follow-ups, inbox reading or lead searches); meeting and task
reminders still go out. "Run one pass now" always runs.

**Time zone**: one setting for the whole system (Settings > Agent & sending > Time zone, Pakistan by default). It decides
the agent's working hours, the hours offered on the booking page, the times the assistant reads and writes, and the zone
of a meeting made without one. The dashboard itself shows times in the clock of the computer you are using.

**Clean up Approvals**: the Clean up button on the Approvals page checks every waiting draft and lists the ones that
should not go out as they are: the lead is closed or on the do-not-contact list, a lead rule now stops it (agencies move
to the partner pipeline), the lead already replied, a newer draft for the same lead is waiting, or there is no address to
send to. Drafts older than 14 days can be taken out too, and are then written again. You choose which groups to take out.
Nothing is deleted: a draft is marked skipped and the lead's timeline says why.

## The assistant

The chat assistant answers questions and acts on what it is asked: move or edit leads, set deal values, add and tick off
tasks, book, move and cancel meetings, add notes, stop contacting someone, start or stop the agent, and report results.
It cannot send outreach emails or approve drafts, only emails a meeting invitation when asked to, and can do nothing the
signed-in person's role does not allow.

## Reports, forecast and deals

**Reports** shows what outreach produces over 7, 30, 90 or 365 days: emails sent, replies, positive replies, meetings,
wins and bounces, broken down by pipeline, by email step, by subject-line version and by person. Rates are by cohort:
of the emails sent in the period, how many were answered. Give a lead a **deal value** and **expected close** and the
forecast shows open value, a likely figure (value x a chance for the lead's stage) and the months it falls in.

The **funnel** at the top counts every lead through the steps: found, worth contacting, contacted, replied, replied
positively, meeting, won, each with the share that made it from the step before, for all pipelines or one. Next to it,
"Where leads drop out" lists the reasons: stopped by which rule, scored too low, no contact details, no reply, said no.

**Subject line test**: each outreach email gets one of two subject lines, split evenly across leads; the report compares
their reply rates. In Approvals you can switch a draft to the other version. Turn it off in Settings.

## Tasks, reminders and follow-ups

**Tasks** is a to-do list with due dates, optionally tied to a lead and assigned to a person; the assignee gets an email
when one falls due. Around meetings the app also, by itself: emails a brief to whoever booked the meeting an hour before
(the lead's research and last reply included); and afterwards marks it done, adds a task to record the outcome, and drafts
a follow-up email that waits under Approvals > Replies & follow-ups. These run even when the agent is stopped.

## Booking page

A page where a lead picks a free time. It offers only times inside the hours set in Settings > Meetings & booking that are
free in the calendar, books the meeting, emails the invitation, and creates or updates the lead.

**On your own website.** Paste the two lines shown in Settings into a page of your site (for example
`brndynamics.com/book`); the booking page appears inside it and sizes itself. Enter that page's address in Settings and
emails link to it, each with a personal link that already knows the lead. The same page is also available directly at
`/book` on this app, and its API (`/api/book/info`, `/api/book`) may be called from another site if you prefer to build
the page yourself.

## Bounces

An email that cannot be delivered stops all email to that address: the address goes on the do-not-contact list, pending
drafts are dropped and the lead waits under "Needs contact" for a working address. Bounce notices are read from the mailbox
(IMAP). When sending through Resend, add a webhook in Resend for `email.bounced` and `email.complained` pointing at
`/webhooks/resend` and set `RESEND_WEBHOOK_SECRET` to its signing secret; unsigned calls are refused.

## Email templates

Settings > Saved email templates. Insert one from the compose window or a lead conversation; `{{first_name}}`, `{{name}}`,
`{{company}}`, `{{sender_name}}` and `{{booking_link}}` are filled in when the email is to a lead.

## Postgres

SQLite (one file) is the default. Set `DATABASE_URL` to a `postgres://` address to use Postgres instead. On the first start
with an empty Postgres, everything is copied over from the SQLite file automatically, so switching loses nothing; the
SQLite file is left untouched as a backup. On Railway: add a Postgres service, reference its `DATABASE_URL` in this
service's variables, and redeploy.

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
* `app/auth.py`: accounts, sign-in sessions, roles and what each may do
* `app/quality.py`: grades, named contacts, clean-up, good/bad marks
* `app/providers/voice.py`: AI phone calls through Vapi
* `app/providers/intent.py`: buying signals (verified public requests, hiring ads)
* `app/invites.py`: Meet links and emailed meeting invitations (.ics)
* `app/rules.py`: lead rules
* `app/more.py`: reports and forecast, tasks, templates, the booking page, the Resend webhook
* `app/jobs.py`: meeting briefs, follow-up drafts and task reminders (run every minute)
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
5. Add your account in Settings > People (or set `BRN_ADMIN_PASSWORD`) and deploy behind HTTPS (Docker image included). For several people at once, set `DATABASE_URL` to use Postgres.

## Roadmap
Kept for later: **lead ownership** (assign each lead to a rep, with "my leads" views) · **WhatsApp Business follow-ups** ·
per-person Google calendars · Apollo search for decision-makers · CRM sync (HubSpot) · multi-tenant workspaces.
