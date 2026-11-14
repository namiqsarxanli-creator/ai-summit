# Event-day post-mortem: AI Summit, 13 November 2026

**Scope:** the live competition at the AI Summit in Baku. It covered panels, masterclasses, the AI Playground, networking connections and the Grand Final.
**Format:** a blameless review. It describes systems and decisions, not people.

## Summary

The platform stayed up all day. All 250 five-minute health checks over the event day passed. No data was lost and nothing was deleted, and the final result was published.

The one problem was at the masterclass doors: the volunteers did not scan attendees in and out. The masterclass points were therefore restored by hand from the booking list, 196 entries in total. This was done with scripts that are dry-run by default, idempotent and audited. None of the entries double-paid anyone or broke a ledger limit.

## The day in numbers

| | |
|---|---|
| Registered attendees | 314 (277 activated) |
| Panel QR scans | 23 (Panel 1), 63 (Panel 2) |
| Grand Final check-ins | 46 |
| Playground sessions scored | 188, of which 83 were walk-ins without a booking |
| Paid networking connections | 159 |
| Beyond Now Founders (all four zones + Grand Final) | 13 |
| Masterclass door scans | 10 entries, 5 exits, across 3 blocks and ~180 seats |
| Ledger at close | 1,317 entries, 8,744 points |
| Masterclass points restored by hand | 196 entries: 66 (block 1), 20 (moved masterclass), 95 (block 2), 15 (waitlist) |
| Pre-event load test (500 virtual users, production-size copy) | 0 errors, p95 1.0-1.6 s |

## Timeline (Baku time)

- **Morning: Playground booths.** Attendees with bookings did not turn up. People standing at the booth could not play: the app enforced queue order, a 3-minute hold for the booked person, and a database trigger capping each 15-minute session at 3 places.
  - **Fix, deployed live:** booth staff admit anyone booked or queued, in any order. The end-of-session scan also scores a walk-in who never booked, creating the booking on the spot.
  - **Database cap:** first, places held by people who had not arrived were released on demand. Then migration `0028_playground_places` raised the per-session cap in the trigger from 3 to 50, as a safety net only.
- **Morning: panel QR.** An attendee who scanned the panel's QR with the phone camera while signed out lost the code during sign-in. The code is now kept in the tab for up to two hours and sent automatically once they are signed in (`lib/pendingPanelCode.ts`).
- **11:30-13:30: masterclass block 1.** Entry is only accepted in the first 10 minutes of a block and exit in the last 10. The server logs show the door phone trying masterclass after masterclass (IDs 11 to 22) and getting `409` refusals. 5 people out of about 66 were scanned.
  - The block's windows were moved four times during the session. It did not help.
  - At 13:30 the organiser decided that everyone holding a seat would get the 12 points. That came to 66 people, including 26 seats the system had already released as no-shows.
- **13:00: a room change.** One masterclass moved from 11:30 in one hall to 14:00 in another room.
  - A masterclass cannot change block while it has reservations, because of a composite foreign key. So a new block, room and copy of the masterclass were created, and all 28 live reservations were re-pointed in a single `UPDATE`. Seat and queue order were kept and the old row was unpublished.
  - 24 attendees were emailed through the app's own mail provider. Each email had a per-person idempotency key, so a re-run would not send twice.
- **15:19-15:30: the moved masterclass.** Its scan window was reopened for 11 minutes with entry and exit both open. The logs show no scan attempt at all.
- **16:20: masterclass block 2.** The entry window was reopened (16:20-16:30), but the doors were again not scanned. Every booking got the 12 points by hand: 95 in block 2 and the moved masterclass's 20. The waitlist got them too (15).
- **17:30: Grand Final.** 46 people checked in and 13 became Founders automatically.
- **Evening: publishing.** Publishing the final result was refused. Four offline door scans from a 07:17 pre-event test were still waiting for review, and the server will not publish while any are pending. They were rejected through the normal review service (status and reason recorded, audited, no points). After that, a new snapshot was taken and published.

## What went well

- **No outage and no lost data.** The capacity was sized from a load test on the same code and an identical 2 vCPU copy, run the day before.
- **The ledger was never edited.** Every correction is a new entry with a reason and an admin audit row. The day's corrections can be reconstructed line by line.
- **No correction could pay twice.** Each hand award carries a stable command id, so re-running a script adds nothing. The bulk awards were re-run as a dry run afterwards to prove it.
- **No double payment from a late scan.** The block 2 masterclass awards used the same deduplication key that a door exit scan uses. A scan after the hand award reports "already paid".
- **Production changes followed one routine:**
  1. Back up the affected tables.
  2. Run a dry run inside a transaction that is rolled back.
  3. Assert row counts before and after.
  4. Apply.
  5. Re-run as a dry run and expect no changes.
- **Rules were changed as code with tests, not as database edits.** The walk-in change shipped with its tests and a reversible migration.

## What went wrong, and why

1. **The volunteers did not scan the masterclass doors.** Across three blocks there were 10 entry scans and 5 exit scans for about 180 seats. At the moved masterclass there was not a single attempt. Where volunteers did try, the scan depended on choosing the right room and phase inside a 10-minute window, from one shared login that held many masterclasses. Once the window had passed, every attempt was refused.
2. **Anti-fraud windows were tuned for an ideal schedule.** Sessions ran late, rooms changed and people arrived when they could. Strict windows turned normal delays into lost points.
3. **Schedule changes needed a developer.** Moving a block's window or a masterclass's room was done by SSH and one-off scripts. That was safe, but it put an engineer on the critical path for every change on the floor.
4. **Test data was left in production.** Four test scans from the morning were still in the offline review queue in the evening and blocked publication. The blocker was shown on the page, but it was easy to miss.
5. **The booking model assumed people come on time.** The Playground's hard cap and queue order did not match how a booth actually runs, which is first come, first served.

## Action items

| # | Action | Addresses |
|---|---|---|
| 1 | Bind each door phone to one room. The scan screen then shows only that room's code, with one button that records entry or exit depending on the time. | 1 |
| 2 | Let a late scan be recorded and queued for review instead of refused. The organiser approves or rejects it in bulk. | 1, 2 |
| 3 | Make scan windows and room moves editable in the admin console, with the same audit trail the scripts write. | 3 |
| 4 | Add a pre-launch "clean production" check that lists and clears test scans, test accounts and draft results. | 4 |
| 5 | Show publish blockers as a checklist at the top of the final page, with a button next to each one. | 4 |
| 6 | Rehearse with the real volunteers on their real phones, including a late session and a room change. | 1, 2 |
| 7 | Make the Playground booth flow first-come by default. Bookings give priority, not exclusivity. | 5 |

## Lessons

- A scan that is never made costs more than a fraudulent one. Design the floor flow for the volunteer under pressure first, and add anti-fraud checks after that.
- Make every correction idempotent and auditable from the start. On the day, the decisions changed hour by hour, and the ledger stayed correct only because every operation could be re-run safely.
