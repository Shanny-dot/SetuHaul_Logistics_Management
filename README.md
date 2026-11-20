# SetuHaul — Logistic Management

Driver-Exception Agent for SetuHaul Logistics (FDE Academy challenge, Mini Project 2).
Drivers report delays on WhatsApp in English, Hinglish, Hindi, Gujarati, Marathi, Punjabi or Marwari; SetuHaul reads the message, checks every dock door against the rules, offers only slots that really work, and books the one they pick in a single checked transaction. Hard cases go to a coordinator with everything on one screen. MVP 2 adds a GPS control tower with live trip status and an automated WhatsApp message series.

**Live site:** https://sdaistudio.github.io/SetuHaul_Logistics_Management/

## The interactive prototype (repository root, served by GitHub Pages)

| Page | What it shows |
|---|---|
| `index.html` — Home | Overview, key results, links to every page |
| `brief.html` — Brief Coverage | Every expectation in the project brief and the 16 Nov solution design mapped to evidence (six hand-ins, scenario must-shows, 13 decisions, measures, rubric, boundaries, guardrails, evals) |
| `flow.html` — Flow & Architecture | Architecture with playable traces, five layers, one exception end to end, Life of a Driver and Life of a Coordinator (before / after) |
| `driver.html` — Driver Messages | The 13 WhatsApp conversations of 4 Aug 2026, interactive, with the tool trail behind every reply |
| `coordinator.html` — Day Board | Dock timeline replay 08:20 → 16:30, live feed, booking health monitor |
| `handoffs.html` — Hand-offs | Approvals with deadlines, safety, data conflict, the no-feasible-slot record |
| `planner.html` — Planner | Six scheduling policies on the 09:45 snapshot, animated |
| `insights.html` — Data Insights | Headline insights, 36 audit findings with SQL (run live in the browser), interactive ER diagram, proposed new tables |
| `integrity.html` — Integrity | Ten drivers, one slot: race simulator with negative control |
| `mvp2.html` — MVP2 · GPS Tower | Live map (OpenStreetMap), red / amber / green trip board, reschedule options, WhatsApp / call actions, 8-rule message series |
| `trucks.html`, `how.html` | All shipments; one turn in ten steps and the booking transaction |

Data files used by the pages: `data.js` (replay results), `er_data.js` (all supplied tables), `insights_data.js` (findings and headline queries), `db.js` (the supplied SQLite snapshot, base64, for live SQL).

External libraries loaded from cdnjs: Leaflet 1.9.4 (map), sql.js 1.10.3 (in-browser SQLite). Map tiles © OpenStreetMap contributors.

## Videos

- `videos/setuhaul_walkthrough.mp4` — the solution and the portal, 3:04, female voice-over (embedded on the Home page)
- `videos/setuhaul_data_insights.mp4` — Data Insights explained, 1:58 (embedded on Home and Data Insights)

## Repository layout

```
/                      interactive prototype (static HTML, no build step)
prototype/             Python prototype: workflow, NLU, feasibility, capacity layer, planner, escalation
  run_scenarios.py     deliverable 4: the scripted day, 13 cases
  run_integrity.py     deliverable 5: 12 tests + 50-race proof
  run_scheduler.py     facility plan, 6 policies, Gantt charts
  scripts/audit.py     deliverable 1: the 36 data checks
  out/                 results used by the deliverables and by this site
docs/deliverables/     00 Review Pack, 01–06 hand-ins, solution design (Word)
docs/images/           architecture (5 layers) and one-exception flow from the solution design
videos/                walkthrough and data-insights videos (MP4, 1080p)
data/                  data dictionary, database guide, schema & seed SQL, core ER diagram
```

## Run the Python prototype

Python 3.11+ and the standard library; matplotlib only for the Gantt charts.

```bash
cd prototype
python3 -I run_scenarios.py out/
python3 -I run_integrity.py out/
python3 -I run_scheduler.py out/
python3 -I scripts/audit.py data/setuhaul_freight_operations.db out/
```

Each run starts from a fresh copy of the supplied database. `run_scenarios.py` prints a byte-identical transcript on every run: its 09:36 two-driver race is released in a fixed, documented order (`SCENARIO_RACE=free` gives the free race). `run_integrity.py` keeps its races free, so which racer wins and the timings vary from run to run, while every invariant holds.

## Run the site locally

```bash
python3 -m http.server 8000
# open http://localhost:8000
```

## Notes

- The supplied data has no GPS feed. GPS positions on the MVP 2 page are simulated from each shipment's departure time and latest ETA in the snapshot; the Jaipur DC location (Sitapura) and road waypoints are approximate.
- Reschedules made on the MVP 2 page are kept in the page only.
- Phone numbers are the seed data's demo numbers.

## Solution design document

The detailed solution design is in [docs/solution_design/SetuHaul_Solution_Design.md](docs/solution_design/SetuHaul_Solution_Design.md) (Word version alongside).
