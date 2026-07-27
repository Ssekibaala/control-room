# Email-Driven Client Feedback — Design

Status: **Built.** SMTP via the existing Teletrac mailbox (no new env vars -
reuses EMAIL_ADDRESS/EMAIL_PASSWORD), thread-per-case ledger in a new
"EmailThreads" Sheet tab, frozen subject `PLATE — GTL case N`, hidden
preheader, two pre-selecting response buttons, signed/expiring no-login
confirm page (POST-only mutation), unified feedback trail regardless of
channel. Verified end-to-end against the real mailbox and Sheet: sent mail
confirmed via IMAP fetch, thread headers (`In-Reply-To`/`References`)
confirmed chaining correctly across two real messages, case-close and
next-case-opens-fresh-thread both confirmed, token tamper/expiry/missing-field
rejections all tested. New module: `mailer.py`, `email_templates.py`,
`notifications.py`, `templates/respond.html`; new Sheet tab: `EmailThreads`
(see `sheets_store.py`'s `get_or_create_open_case`/`record_sent_message`/
`close_case`). Not yet built: inbound reply ingestion, per-vehicle-per-day
coalescing, and the clients registry (recipients currently come from
role-tagged user accounts, per user instruction, not a separate registry).

Below is the original exploration this was built from.

Status (original): **design agreed in principle, not yet built.**
Source: five independent design lenses (mail-protocol engineer, UX minimalist,
East-African field ops, trust & safety, wildcard) producing 30 ideas, plus
partial adversarial stress-testing. Captured here so the reasoning survives.

The goal that governs every decision below: **the client never logs in.**
They live in their inbox, often on a phone, on metered data, between other jobs.

---

## 1. Threading: subject is identity, preheader is news

All five lenses converged independently on the same resolution of the
"descriptive subject vs stable thread" tension, and it is better than putting
the news in the body:

- **Subject = identity only, byte-identical forever.** Gmail splits a thread when
  the normalised subject changes; Outlook derives its ConversationTopic from the
  subject. Any counter, status or date appended to the subject fragments the
  thread. Generate it from one function that takes no formatting arguments.
- **Preheader = the event.** The preheader is the hidden block at the top of the
  body that Gmail, Gmail mobile, Outlook mobile and Apple Mail all render as the
  snippet **directly beside the subject in the inbox list**, and for a multi-message
  thread Gmail shows the snippet of the *newest* message.

So the client gets a changing, descriptive line in their inbox **and** an unbroken
thread. The two goals were only ever in conflict because both were aimed at the
same 60 characters.

Inbox row ends up reading:

```
Teletrac   KDN480R — GTL case 7            (4)
           Feedback received — no follow-up needed
```

### Mechanics

| Header | Purpose |
|---|---|
| `Subject` | Frozen at case creation. Plate first so it survives truncation on a small screen. |
| `Message-ID` | Ours; the root is stored per case. |
| `In-Reply-To` | The case's root Message-ID. |
| `References` | Accumulated chain. |
| `Thread-Topic` / `Thread-Index` | Outlook desktop threads on these; synthesise them. |

The thread ledger (root Message-ID, References chain, subject string) **must live
in the Google Sheet**, not on disk — Render's filesystem is ephemeral and a
redeploy would otherwise orphan every live thread and silently start new ones.

---

## 2. Thread per CASE, not per vehicle forever

A per-vehicle-forever thread becomes an unreadable three-year scroll, and
"is there a thread?" stops meaning anything.

**An open thread means an open problem.** A case opens when a vehicle needs the
client's input, and closes when it is resolved. The next problem on the same
vehicle starts a new thread, with the plate still leading the subject so all of a
vehicle's history is one inbox search away.

That single equivalence — open thread = open problem — is worth more to the
client than "all history in one place".

## 3. The thread-pile problem (found by the lenses, not in the original plan)

Per-vehicle threading fixes message-pile-per-vehicle but **creates
thread-pile-per-fleet**: 40 offline vehicles = 40 threads on a phone.

Mitigations, in order of preference:

1. **Coalesce to at most one message per vehicle per day.** Assume conversation
   view is OFF (it is, for many Outlook users). Then the number of unread items
   *is* the number of trucks needing attention.
2. **A dedicated thread only where a decision is owed.** Routine detection stays
   in one collapsing digest; a thread is born when a human needs to answer.

---

## 4. Safety findings

### GET must be inert (confirmed independently)
Corporate mail scanners — Microsoft Safe Links, Mimecast, Proofpoint, Barracuda —
**prefetch every URL** to detonate malware. A link that performs the action on GET
will be fired by the scanner, marking a vehicle "no follow-up needed" before the
client has opened the mail.

**Rule: no GET ever changes state.** The email's buttons land on a page with that
answer **pre-selected**; a POST commits it. Two taps, not one, and no phantom
answers. This is the failure mode most likely to quietly destroy trust in the
whole feature.

### A client's "it's fine" must never silently clear a tamper flag
Marking `requiresFollowup=false` pulls an asset out of Critical Assets. It must
**not** also clear tampering evidence. The client is answering "is this vehicle a
problem for me?", not "is there evidence of device tampering?" — those are
different questions and only one of them is theirs to answer. Keep the tamper
signal independent of client feedback.

### Forwarding is a feature, not an abuse
The recipient often is not the person who knows the answer. Rather than trying to
prevent forwarding, **record who actually answered** — capture a name on submit
and attribute the trail entry to them.

---

## 5. Cold start is a decision point, not a polish issue

Render free tier sleeps; a tapped link can show a 30–60 second blank page. On
metered mobile data that reads as *broken*, and it is the single most likely
reason a client taps once and never again.

Options:
- Pre-warm the server when the email is *sent* / opened, not when tapped.
- Serve the client-facing page from Apps Script (always warm) and have it call
  back to Render.
- At minimum, an instant static shell that renders before any data loads.

---

## 6. Accept replies (reversal of the earlier position)

Earlier reasoning said "don't parse inbound email" to avoid subject tokens and
sender forgery. **With per-case threading that reverses**: a threaded reply
carries `In-Reply-To` pointing at a message we sent, so the case — and therefore
the vehicle — is identified for free.

More importantly: if it looks like a conversation, the client **will** hit Reply.
If we do not ingest replies, their answer lands in a mailbox nobody reads and they
believe they have responded. Silent failure is worse than not offering it.

- Primary path: the buttons.
- Also supported: plain reply, ingested into the same trail.
- Strip quoted history at an explicit sentinel we control (`--- reply above this line ---`),
  with heuristic fallbacks.
- Accept only from addresses we sent to; record `Authentication-Results`.

## 7. One trail, always
Technician actions, in-app client comments, button answers and email replies all
append to the **same** chronological trail per vehicle, distinguished only by
author and a channel label. Storage stays append-only.

**The client's own answer must appear back in the thread**, attributed to them, as
a receipt. It collapses "did my feedback register?" and "what did I actually say?"
into nothing.

## 8. Silence is a valid answer
Do not force a response. Say explicitly what happens if they do nothing, and do
not re-ask a question already answered. The burden of a notification is not
reading it — it is the decision it forces.

---

## 9. Ideas parked (good, not now)

| Idea | Why parked |
|---|---|
| `kdn480r@fleet.teletracfleets.com` — plate as address | Elegant; needs catch-all subdomain + DNS work. |
| WhatsApp for the decision, email for the record | Matches field reality best; needs WhatsApp Business API. |
| QR sticker on the truck → permanent vehicle URL | Cheap and durable; do once per-vehicle URLs exist. |
| `mailto:` buttons — answer with no signal | Genuinely works offline; revisit if connectivity bites. |
| Calendar invite with RSVP as the follow-up switch | Inventive; too surprising as the primary path. |
| Build the email to be screenshotted into WhatsApp | Costs nothing — just make it legible as an image. |

---

## 10. Build order

1. **Clients registry** — name + contact emails; email field on users; client-role
   users linked to a client. Nothing works without it.
2. **Outbound threaded email** — frozen subject, living preheader, thread ledger
   in the Sheet, two pre-selecting buttons → confirm page → POST.
3. **Outcome email into the same thread**, including the client's own words.
4. **Ingest replies** to the thread.
5. **Coalescing** — max one message per vehicle per day.

## 11. Open decisions

| Decision | Recommendation |
|---|---|
| SMTP via the existing Teletrac mailbox, or a provider? | Existing mailbox — no new vendor, replies land where we already read. |
| Subject format | `KDN480R — GTL case 7` — plate first, frozen at case creation. |
| Outcome email recipients | Client contacts + technicians + admins, same thread. |
| Ingest replies now or later? | **Now.** Threading invites replies; ignoring them loses messages silently. |
| Thread per case or per vehicle forever? | **Per case.** Open thread = open problem. |

---

*Note on provenance: 6 of the 30 stress-test agents ran without the safety
classifier available, and 24 stress-tests plus the synthesis step failed on a
usage limit. The ideas above are captured as generated; the synthesis and
recommendations in this document are the author's own, not an agent's.*
