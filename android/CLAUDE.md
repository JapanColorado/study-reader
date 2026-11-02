# CLAUDE.md

Study Review (`dev.russell.studyreader`): the Android companion to the Study Reader desktop app (the
parent directory) for reviewing its cards. See README.md for what it does and how syncing is set up.

## Commands

All through pixi (the system JDK is incomplete; never run `./gradlew` directly):

```sh
pixi run test         # ./gradlew :core:test (JVM, no device)
pixi run build        # release APK; build-debug for the .debug appId
pixi run install      # install the release build on the adb device; run = install + launch
pixi run logs         # logcat for the running app
```

Android SDK: `~/Android/Sdk`. Parity fixtures are made by the desktop's Python, run directly
(running pixi in the repo root would build the desktop env):
`PYTHONPATH=../src /home/russell/Projects/study-reader/.pixi/envs/default/bin/python scripts/<name>.py`
(`fsrs_fixtures.py`, `grading_fixtures.py`, `queue_fixtures.py`).

## Architecture

- `core/` is plain Kotlin/JVM (no Android), so it is unit-tested with sqlite-jdbc (`JdbcSql` in tests). It
  talks to SQLite through the `Sql` interface. Each file ports a desktop module and is tested against
  what that module returns for the same input:
  - `Fsrs.kt` ports py-fsrs 6.3.2. Times are kept as µs Longs, card JSON matches `Card.to_json`, and
    `FsrsTest` replays py's random draws to check the fuzzing too.
  - `Grading.kt` (and the calculator/LaTeX ports beside it) ports `quiz.grade` (`GradingTest`).
  - `Srs.kt` and `Gamify.kt` port `srs.py`'s queue, counts, sections and previews, and `gamify.py`
    (`QueueTest`, over `queue_snapshot.db`, a real export).
  - `ReviewController.kt` ports `review.ReviewPanel` (all-books mode). It drives the desktop's own
    `review.html` through `js` callbacks (`showOverview`, `showCard`, `celebrate`, `inputError`).
  - `SyncStore.kt` is the phone's half of the sync format: the `pending` event queue, batches,
    `adopt` (take over a new snapshot, drop acknowledged events, replay the rest) and the local
    tables (`pending`, `buried`, `local`). The desktop half and the format itself are in
    `../src/study_reader/phone_sync.py`; keep the two in step.
- `app/`:
  - `MainActivity` is a Compose shell (app bar with sync and settings) around one WebView loading
    `review.html` through `WebViewAssetLoader`. Assets are copied from the desktop at build time
    (`copyWebAssets`), and `/media/` is served from `filesDir/media`.
  - `ReviewBridge` is `window.ReviewApp`. It has the desktop web channel's method names, and calls
    run on `Store.worker`, a single thread for all database work.
  - `Store` holds `cards.db` (snapshot copy plus local tables, behind `SwappableSql` so a new
    snapshot can replace it) and does SAF I/O on the shared folder: `flush` writes batches as
    `.syncthing.*.tmp` then renames them, `takeSnapshot` copies the snapshot and adopts it, and
    `syncMedia` mirrors the media.
  - `Reminders` is an inexact daily alarm that syncs, counts what's waiting and notifies.
- No bury on the desktop: the phone hides buried cards itself (`buried`, until the next 4 am),
  and the `bury` event moves a scheduled card's due date to tomorrow on the desktop.
