# TMT Print Tracker

Tracks the 3D-printed parts of Toddler Mobility Trainer (TMT) builds from your Bambu Lab and Moonraker printers
(Snapmaker U1 and other Klipper printers).

## Getting started

1. Add your printers to Home Assistant with the [Bambu Lab integration](https://github.com/greghesp/ha-bambulab) or,
   for a Snapmaker U1 or other Klipper printer, the [Moonraker integration](https://github.com/marcolivierarsenault/moonraker)
   (both from HACS). Give each Moonraker printer its own name when you add it.
2. Start this app and open **TMT** in the sidebar (turn on **Show in sidebar** on the app's page).
3. Under **Files**, upload the TMT design files (`.3mf`). A notice at the top lists any file the build profile still
   needs. Files are matched by the **Title inside the 3MF**, so their file names don't matter.
4. Print as usual. Each finished plate's parts are added to the count.

You can also copy the 3MF files into this app's folder yourself: `/addon_configs/<id>_tmt_tracker/files/` (shown as
`app_configs` on newer Home Assistant versions), then press **Rescan**.

## How a print is recognised

In this order:

1. A plate you assigned by hand before (remembered by job name + plate number).
2. The job name equals a file's Title and the plate number exists in that file. This is the case for prints sent
   from Bambu Studio.
3. The job is named after the plate, or the one part on it. Orca-style slicers (Snapmaker Orca for the U1) name the
   file after it, e.g. `3DTMT F SEAT BACK CENTER TOP REV A_PETG_9h44m.gcode`; the filament and time at the end are
   ignored. If several plates fit, the plate preview decides between them.
4. The same plate as the printer's previous job (reprints).
5. The printer's plate preview image (a Moonraker printer's print thumbnail) compared with every plate preview in your files: the outlines must overlap by
   at least 80% and clearly beat the runner-up. Colour is ignored. This catches prints sent from MakerWorld or Bambu
   Handy, whose job name is the print profile's name.

If a print isn't recognised, the printer card and the history row get a **which plate is this?** menu. Your choice is
counted and remembered.

## The dashboard

- **Printers**: live status, the plate being printed, progress, time left.
- **TMTs**: one card per planned TMT (set how many with − / +). A TMT's pieces fill in order: TMT #2 counts the parts
  printed beyond what TMT #1 needs. **Mark assembled** takes one TMT's parts out of the count.
- **Parts**: every part by group, with ✓ / partial per TMT, what's printing now, and − / + to correct counts (for
  example parts printed before you started using this app, or a part that failed on a plate).
- **Projects**: a request, a chair not yet for a child, or a parts order (wheels for another maker, say), each counted
  separately. Create one under **New project**: a whole TMT, or a parts order where you set how many of each part (or
  fill in one TMT's worth of a group, like Wheels). Then pick it under **Printing for** on a printer card; that printer's
  finished plates count toward it until you change it. **use spare parts** moves parts the project still needs out of
  the spares, **mark done** closes it. Prints for no project go into the spare parts, shown as the planned TMTs below.
- **Not for a TMT**: under Printing for, for prints that aren't TMT parts. They're logged, never counted.
- **Recent prints**: counted, failed, not recognised (assign) or not a TMT plate; **undo** takes a print back out.
- **Files**: the uploaded 3MF files, what each is used as, and the build profile in use.

## Options

| Option | |
|---|---|
| `profile` | `makegood_tmt_rev_c` (built in), or your own profile file in this app's folder, e.g. `profiles/my_build.yaml`. |
| `printers` | Leave empty to use every Bambu Lab and Moonraker printer. Or list `name` + `entity_prefix` (for `sensor.my_x1c_print_status` the prefix is `my_x1c`) and, for a Moonraker printer, `type: moonraker` (for `sensor.my_u1_current_print_state` the prefix is `my_u1`). |
| `log_level` | `debug`, `info`, `warning` or `error`. |

## Changing the build: profiles

On first start the built-in profiles are copied to `profiles/example_*.yaml` in this app's folder. Copy one to
`profiles/my_build.yaml`, edit it, set the `profile` option to `profiles/my_build.yaml` and restart the app.

- `files`: the design files, by the Title inside each 3MF.
- `count_other_files` (default `true`): a plate of any other 3MF counts when every part on it is a part of the build,
  for example the same parts laid out for a Snapmaker U1.
- `plates`: which plates count toward a TMT, each with a `group`, and optionally `material` and `optional: true`.
  Plates not listed are still recognised when printed, and logged as "not a TMT plate".
- `quantities`: pieces per TMT where it isn't simply one plate's worth (for example 16 tires from an 8-tire plate).
- `alternatives`: one requirement any one option satisfies (for example a single-piece part or a set of smaller
  parts).
- `part_rules`: turn object names inside the files into part names; anything else is cleaned up automatically.

Part counts are kept by part id, so changing the profile or replacing a file keeps the counts.

## Moving counts in from elsewhere

`POST api/action` with `{"cmd": "import_counts", "counts": {"<part id>": n, ...}, "replace": false}` adds counts (the
part ids are shown in the profile's catalog). The app answers with any ids it didn't know.

## Data

Everything stays in Home Assistant: counts and history in the app's private data (included in Home Assistant backups),
your 3MF files in the app's folder. The app talks to Home Assistant through the Supervisor; no token is needed and none
is stored.
