# Plan: live WhatsApp voice calls with an ElevenLabs voice agent

**Status (27 Nov 2026):** not built. Voice **notes** work today (`prototype/setuhaul/voice.py`). Live **calls** need a WhatsApp Business number first; this page lists what that takes. Items marked *verify* were not confirmed in official docs while writing this.

## What it would do

1. The driver taps the call button in the SetuHaul WhatsApp chat and speaks in their language.
2. An ElevenLabs voice agent listens and talks back.
3. Every decision is still made by the SetuHaul engine, just as for text: ETA, slot options, booking, safety hand-off. The agent only listens and speaks.

## Why it can't run on today's setup

- **Twilio WhatsApp Sandbox:** documented for messaging only. Its docs don't mention calls.
- **WhatsApp calling on Twilio:** generally available since July 2025 ([Twilio changelog](https://www.twilio.com/en-us/changelog/whatsapp_business_calling_available)). It's activated per **registered WhatsApp sender** by setting a TwiML application on that sender's voice configuration ([Twilio docs](https://www.twilio.com/docs/voice/whatsapp-business-calling)).
- **Meta's calling sandbox:** for Tech Partners only ([Meta docs](https://developers.facebook.com/documentation/business-messaging/whatsapp/calling/sandbox)).

## Steps

| # | Step | Who | Typical time |
|---|---|---|---|
| 1 | Get a phone number for the business (a Twilio number, or one you own that isn't already on WhatsApp) | you | 1 day |
| 2 | Register it as a **WhatsApp sender** in Twilio (Meta Business Portfolio, business verification, display name "SetuHaul") | you + Meta review | days to a few weeks (*verify*) |
| 3 | Move the app from the Sandbox to that sender: `TWILIO_WHATSAPP_FROM`, approved message templates (`prototype/templates/twilio_templates.yaml`) | me | 1 day after approval |
| 4 | Turn on **WhatsApp Business Calling** for the sender in Twilio (TwiML application on its voice configuration) | you + me | 1 day |
| 5 | Create an **ElevenLabs agent** with a low-latency multilingual voice model (`eleven_v4_turbo` per ElevenLabs' models page) | me | 1–2 days |
| 6 | Connect the call to the agent: Twilio Voice → ElevenLabs agent (Twilio integration or SIP; *verify* which ElevenLabs supports for WhatsApp-originated calls) | me | 1–2 days |
| 7 | Give the agent **SetuHaul tools**: one webhook it calls with the driver's words, which runs the engine turn and returns the reply to speak. Identity = the caller's WhatsApp number, never what the caller says | me | 2–3 days |
| 8 | Test the C03/C06/C12/C14 scenarios by voice, plus misheard numbers, noisy roads and a dropped call | you + me | 2 days |

## Design rules (same as text)

- **The model reads and speaks; code decides.** The agent never books, holds or releases on its own. It passes words to the engine and speaks the engine's guarded reply.
- **Spoken confirmation before booking:** "Option two, Dock D2 at 11:30. Shall I book it?" Then a yes.
- **Safety words end scheduling talk at once**, as in text: 112 advice, and the shift manager is paged.
- **Dropped call or low battery:** the same rules as `unreachable.py`. The slot is held and the coordinator is told.
- **Privacy (DPDP):** say at the start of the call that it is handled by an automated assistant and transcribed. Don't keep recordings unless needed, and never longer than the 90-day message rule.

## Costs to check before deciding (not estimated here)

- Twilio phone number and WhatsApp sender fees
- Meta's WhatsApp calling charges (*verify* current pricing for India)
- Twilio Voice per-minute charges for WhatsApp calls
- ElevenLabs agent per-minute charges on your plan

## Recommendation

Run voice notes with real drivers first. They need no new number or approvals and show whether drivers actually prefer speaking. Register the WhatsApp Business number in parallel, since that's also needed for production text messaging (templates, no 3-day sandbox join).
