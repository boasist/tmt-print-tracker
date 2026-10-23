# TMT Print Tracker

Tracks the 3D-printed parts of Toddler Mobility Trainer (TMT) builds from your Bambu Lab printers, keeps a print
queue per printer, and plans whole builds.

## Getting started

1. Add your printers to Home Assistant with the [Bambu Lab integration](https://github.com/greghesp/ha-bambulab).
2. Start this app and open **TMT** in the sidebar (turn on **Show in sidebar** on the app's page).
3. Under **Files**, upload the TMT design files (`.3mf`). A notice at the top lists any file the build profile still
   needs. Files are matched by the **Title inside the 3MF**, so their file names don't matter.
4. Print as usual. Each finished plate's parts are added to the count.

There's nothing to set up first: the app creates its `files` folder itself, and large files (the ALL PARTS file is
about 96 MB) upload in pieces. You can also copy the 3MF files into this app's folder yourself:
`/addon_configs/<id>_tmt_tracker/files/` (shown as `app_configs` on newer Home Assistant versions), then press
**Rescan**.

## How a print is recognised

In this order:

1. A plate you assigned by hand before (remembered by job name + plate number).
2. The job name equals a file's Title and the plate number exists in that file. This is the case for prints sent
   from Bambu Studio.
3. The same plate as the printer's previous job (reprints).
4. The printer's plate preview image compared with every plate preview in your files: the outlines must overlap by
   at least 80% and clearly beat the runner-up. Colour is ignored. This catches prints sent from MakerWorld or Bambu
   Handy, whose job name is the print profile's name.

If a print isn't recognised, the printer card and the history row get a **which plate is this?** menu. Your choice is
counted and remembered for that printer (by job name and plate number). A recognised plate has a **change** button on
its printer card. Later, when the printer's preview clearly shows a different plate than the one remembered, the
picture wins.

### Custom plates

For a plate you combined or changed, for example two wheel rims and a bolt cap on one plate, choose **custom plate:
pick the parts…** in the plate menu and give the parts and how many. The print counts exactly those parts. The custom
plate is remembered for reprints (by job name and plate number, and by the printer's preview picture of it), shows up
in the menus as **Custom: …**, and the queue and planner use it like any plate, on the printer it was printed on. A
custom plate that makes more of a part than any of the design file's plates is the one queued for that part.

## The dashboard

- **Printers**: live status, the plate being printed, progress, time left, the next plate, and after a print
  **Plate is clear**. Click a printer for its details (below).
- **Print queue**: one card per printer (below).
- **TMTs**: one card per planned TMT (set how many with − / +). A TMT's pieces fill in order: TMT #2 counts the parts
  printed beyond what TMT #1 needs. **Mark assembled** takes one TMT's parts out of the count.
- **Parts**: every part by group, with ✓ / partial per TMT, what's printing now, and − / + to correct counts (for
  example parts printed before you started using this app, or a part that failed on a plate).
- **Recent prints**: counted, failed, not recognised (assign) or not a TMT plate; **undo** takes a print back out.
- **Files**: the uploaded 3MF files, what each is used as, and the build profile in use.

## Print queue

Each printer has a list of plates to print next; the first is **next**.

- The plates the planned TMTs still need are queued automatically, in the profile's order, each on the printer with
  the shortest queue that has the plate's filament loaded (AMS trays with a spool, plus the external spools' set
  filament). A plate printing now isn't queued again, and a plate limited to some printer models (the single-piece
  handle needs an H2) stays on those.
- The queues follow what happens: a printed plate comes off, a failed one goes back to the front of its printer for a
  retry, and correcting counts or changing the number of planned TMTs re-plans them. Queues are kept balanced.
- On each card: **↑ ↓** to reorder, **→ printer** to move a plate (it then stays there), **✕** to skip a plate (it's
  not queued again until you **restore** it below the queues), **+ add a plate** for anything else, the filament tags
  to say what the printer can print (click to change; **auto** follows what's loaded), and **on** to keep a printer
  out of the queue.
- Times: a plate's time on that printer, from its own prints only (≈ when it's an estimate or from the plate's mirror
  twin). The card shows the queue's total.

The app can't start prints itself. Bambu's current firmware only takes print commands from other software in
Developer Mode, which needs LAN Only Mode, and the printer must have the sliced plate file. You start the next plate
from Bambu Studio, MakerWorld or Bambu Handy as usual; the queue tells you which.

## Phone alerts

Add your notify services under **notify** (for example `notify.mobile_app_my_phone`; one per line). When a print
ends, each gets an alert with the result, the TMT's progress and the printer's next plate, and a **Plate is clear**
button. Tapping the button marks the plate cleared and replaces the alert with "ready, start … next". Tapping the alert
opens this app's page (change it with **alert_url**). A printer's newer alert replaces its older one.

## Printer details and camera

Click a printer card for everything Home Assistant knows about it, refreshed every 2 seconds: printer alerts (HMS),
the print (plate, progress, layers, time left, done at, speed, filament used, build plate), temperatures of each
nozzle, bed and chamber, fans, filament (the spool in use, each AMS unit's humidity, temperature and drying, and its
spools) and the nozzles.

The camera comes from the printer's camera entity: live video where the browser plays it (WebRTC or HLS; Safari and
the Companion app play the Bambu printers' HLS stream), otherwise a fresh picture every 2 seconds. Bambu printers only
stream when **LAN Only Liveview** is turned on in their network settings (on the printer's screen); until then the
pop-up says so.

## Print times

Every finished print is recorded for its printer: the slicer's estimate at the start, the real printing time (time
paused, say after a filament runout at night, is left out), the filament and the nozzle it printed with. A plate's
time on a printer is the median of that printer's prints of it; before it's been printed there, the estimate of the
same plate printing now, or the time of its mirror twin (WING LEFT for WING RIGHT). Times are never taken from another
printer, because the same plate can take twice as long on one as on another.

On the planner page, **Print times** lists every sample: **edit** one that's off (for example a print that sat waiting
before pauses were tracked), **exclude** it, delete it, or **add a time you know** (from Bambu Studio or MakerWorld).

## Build planner

**Build planner →** next to the queue. It plans every plate the planned TMTs still need (raise the number of planned
TMTs to plan the next one from the start) across your printers.

- **Setups**: each printer works through its setups in order. A setup is a nozzle size plus the filament and part
  groups it prints (none picked = any). For example a printer can print TPU on a 0.6, then the straps on a 0.4, then
  PETG on the 0.6; a change of nozzle size between setups is a swap the plan schedules at a plate change. Printers
  start with one setup that prints everything on the nozzle fitted now.
- **Your hours**: when you can clear plates (6 AM to 9 PM by default), how long a plate change and a nozzle swap take,
  and a time to assume for a plate with no data. A print only starts while you're around; one that ends overnight waits
  until morning.
- **Make the plan** orders and assigns the plates to finish soonest and keep the printers busy: shorter plates through
  the day and a long one started in the evening to run through the night. You get the build's finish time, each
  printer's waiting time, a timeline (night shaded, nozzle swaps marked, guessed times striped) and **When you're
  needed**, a list of what to do when.
- Times come from your print times. A plate a printer hasn't printed gets a rough guess from the plate's size and that
  printer's pace, clearly marked.
- **Use this plan** sets the queues to the plan's order, with each plate's setup and nozzle; the alert then says when
  to swap the nozzle first. **Stop following it** hands the queues back to automatic filling.

## Options

| Option | |
|---|---|
| `profile` | `makegood_tmt_rev_c` (built in), or your own profile file in this app's folder, e.g. `profiles/my_build.yaml`. |
| `printers` | Leave empty to use every Bambu Lab printer. Or list `name` + `entity_prefix` (for `sensor.my_x1c_print_status` the prefix is `my_x1c`). |
| `notify` | Notify services for phone alerts, e.g. `notify.mobile_app_my_phone`. Empty: no alerts. |
| `alert_url` | Where tapping an alert goes. Empty: this app's page. |
| `log_level` | `debug`, `info`, `warning` or `error`. |

## Changing the build: profiles

On first start the built-in profiles are copied to `profiles/example_*.yaml` in this app's folder. Copy one to
`profiles/my_build.yaml`, edit it, set the `profile` option to `profiles/my_build.yaml` and restart the app.

- `files`: the design files, by the Title inside each 3MF.
- `plates`: which plates count toward a TMT, each with a `group`, and optionally `material`, `optional: true` (not
  needed for a TMT to count as complete), `plan: true` (an optional plate the queue and planner still plan, like the
  accessories), `together` (plates that only make these parts as a set, e.g. `{tablet-holder: 1}` for a tablet holder
  split over two plates: it counts once each plate has printed) and `printer_models` (printers whose Home Assistant
  device model starts with one of these, e.g. `[H2]`). Plates not
  listed are still recognised when printed, and logged as "not a TMT plate".
- `quantities`: pieces per TMT where it isn't simply one plate's worth (for example 16 tires from an 8-tire plate).
- `alternatives`: one requirement any one option satisfies (for example a single-piece part or a set of smaller
  parts).
- `part_rules`: turn object names inside the files into part names; anything else is cleaned up automatically. A
  rule with `file` only applies to that file, for designs whose objects have generic names ("Part Studio 2 - Part 4").

Part counts are kept by part id, so changing the profile or replacing a file keeps the counts.

## Moving counts in from elsewhere

`POST api/action` with `{"cmd": "import_counts", "counts": {"<part id>": n, ...}, "replace": false}` adds counts (the
part ids are shown in the profile's catalog). The app answers with any ids it didn't know.

## Data

Everything stays in Home Assistant: counts, history, queues, print times and plans in the app's private data
(included in Home Assistant backups), your 3MF files in the app's folder. The app talks to Home Assistant through the
Supervisor; no token is needed and none is stored. It reads your printers' entities and camera, and only calls the
notify services you list.
