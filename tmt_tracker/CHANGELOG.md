# Changelog

## 0.4.0

- **3MF files page** (**Manage 3MF files** at the top of the dashboard): every file with its title, designer, the
  printer, nozzle, layers and filaments it was set up for, and each plate with its picture, what it makes, group,
  volume, print time per printer and times printed.
  - Files the build profile doesn't know (your own sliced projects, other versions, accessories) show up as **new**:
    pick which plates count toward a TMT and in which group, their filament, the printers that may print them, plate
    names, which part each object is (an existing part, or a new one you name), and plates printed together.
  - Edit any file later, including the profile's; a profile file follows profile updates until you save it.
  - Groups (required / optional / optional but planned, in table order), part names and quantities per TMT.
  - The profile's design files still missing, with **I don't use this one** to drop one from the notice.
  - **Remove file** moves a file to `files/removed` instead of deleting it, and **Put back** returns it.
  - Uploads by button or by dropping files on the page, in pieces with progress.
- What you set there is kept in `build.json` in the app's folder; with no `build.json` the catalog is exactly as
  before. Plate ids, counts, history and queues are unchanged.
- The dashboard's notices link to the files page, including one for new files that need setting up; its Files section
  is a summary with the link.
- The first start after this update adds the new file details to the cache in a few seconds (the slow volume reading
  isn't repeated).

## 0.3.6

- Printer cards no longer overflow: long printer and file names wrap inside the card instead of running into the next
  one (with four printers side by side the status, details link and plate menu overlapped).
- Printers that still have the Bambu Lab integration's default name (`H2C_31B8AP…`, the model and serial number) show
  as their model, with the serial's last 4 digits when two printers are the same model. Rename the device in Home
  Assistant to pick your own name.
- Print queue: in a narrow card the buttons sit under the plate name, so names no longer squeeze into a thin column.

## 0.3.5

- Fix: uploading a 3MF file over 16 MB failed (Home Assistant's ingress refuses larger requests with "413"), so the
  ALL PARTS file and the tablet holder couldn't be added from the page. Files now go up in 8 MB pieces, with progress.
- What the app reads from each 3MF is kept (`files_cache.json` in its storage), so it starts in seconds and adding a
  file only reads that file. Before, every start and every file change re-read all of them: about 4 minutes for the
  ALL PARTS file on a small x86 machine, with the page frozen meanwhile. Reading now happens in the background, and
  big part files are streamed (about half the memory).
- The plate pictures used to recognise a print are prepared in the background and kept across file changes. Before,
  the first picture match after a start or a file change froze the page for up to a minute and a half.
- The first start after this update reads every file once more (a few minutes for the ALL PARTS file).

## 0.3.4

- The tablet holder is one item, like the cup holder. Its file splits it over two plates; the tablet holder counts
  once both plates have printed, and the queue and planner plan whichever plate is still missing. A plate that holds the
  whole tablet holder (a custom plate) counts it at once. The parts table shows "plate 1 of 2 printed" in between.
- Profiles: `together: {part: n}` on a plate rule for plates that only make a part as a set.
- A custom plate whose parts are no longer in the build (after a profile change) is ignored instead of planned.

## 0.3.3

- The accessories (cup holder, tablet holder) are optional: a TMT counts as complete without them. They're still
  queued and planned for each TMT, and **Mark assembled** takes the ones you have along with the TMT.
- Profiles: `plan: true` on an optional plate keeps it in the queue and planner. The headrest stays optional and
  unplanned.

## 0.3.2

- **Accessories** in the default build: one cup holder (any of v1, v2 or v3, right or left, from the 3D TMT Cup Holder
  file) and one tablet holder (both plates of the 3DTMT Tablet ACC Holder file) per TMT. Upload the two files to add
  them; until then the page lists them as missing.
- When two plates make the same number of a part, the queue now plans the one with more pieces on it, so a custom plate
  that combines the cup and tablet holders is printed once instead of three separate plates.
- Profiles: a part rule can name a `file`, so it only applies to that file's objects.

## 0.3.1

- **Custom plates**: for a plate you combined or changed (say two wheel rims and a bolt cap on one plate), choose
  **custom plate: pick the parts…** in a plate menu, or **change** on a printer card, and give the parts and how many.
  The print counts those parts, the plate is remembered for reprints (and recognised by its preview picture), and the
  queue and planner use it, on the printer it was printed on.
- Fix: a remembered plate is now per printer. Print profiles (MakerWorld's especially) can share a name across
  printers while numbering their plates differently, so one printer's plate 15 could show up as another printer's.
  Existing remembered plates move to the printer they came from.
- A job recognised by a remembered plate or as a reprint is checked again when the printer's preview arrives; if the
  picture clearly shows a different plate, it follows the picture. A stale preview (the previous job's picture at the
  start of a print) is recognised by the picture itself and ignored.

## 0.3.0

- **Print queue per printer**: the plates the planned TMTs still need, each on a printer that has the plate's filament
  loaded, kept balanced and re-planned as prints finish, fail or change. Reorder, move, skip, restore and add plates;
  set what filament a printer can print; turn a printer's queue off. A failed plate is retried first.
- **Phone alerts** (option `notify`): when a print ends, the result, the TMT's progress and the next plate, with a
  **Plate is clear** button. Option `alert_url` for where tapping it goes.
- **Printer details**: click a printer for its camera (WebRTC or HLS through Home Assistant, else a picture every
  2 seconds), printer alerts, the print, temperatures, fans, filament and AMS units, and nozzles.
- **Print times per printer**: every print's slicer estimate and real printing time (paused time left out), with the
  filament and nozzle. Shown on the queue with totals; edit, exclude, delete or add samples on the planner page.
- **Build planner** (new page): each printer's setups in order (nozzle size, filament, part groups), your hours, and a
  plan that keeps the printers busy, with a timeline and a "when you're needed" list. **Use this plan** sets the
  queues, including when to swap the nozzle.
- Profiles: `printer_models` on a plate rule keeps a plate on matching printers (the single-piece push handle on H2
  printers). The catalog records each plate's model volume for rough time guesses.
- Wide tables scroll on a phone instead of widening the page.


## 0.1.2

- Printer cards show when the print will be done ("Done at 4:49 PM", with the weekday if it isn't today) instead of
  how the plate was recognised. Finished prints show "Finished at …"; plates outside the build keep a "not a TMT plate" tag.

## 0.1.1

- Fix: a new print could be recognised as the previous plate. When a job starts, the printer updates its preview
  image's timestamp before the new picture arrives; a match to the previous job's plate on a different plate number is
  now ignored until the real picture comes.
- A newer preview during a job that was recognised by image is checked again, and the job follows it if it clearly
  shows a different plate.

## 0.1.0

First release.

- Finds Bambu Lab printers in Home Assistant automatically.
- Builds the TMT parts list from your uploaded 3MF files and a build profile (MakeGood TMT Rev C built in).
- Recognises the plate being printed by job name + plate number, or by the plate preview image.
- Counts parts when a print finishes; failed prints don't count. Manual corrections, undo, assign, import.
- Per-TMT progress and "mark assembled".
- Dashboard in the sidebar through ingress.
