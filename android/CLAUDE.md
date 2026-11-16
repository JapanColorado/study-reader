# CLAUDE.md

Study Practice (`dev.russell.studyreader`): the Android companion to the Study Reader desktop app (the
parent directory) for practicing its questions. See README.md for what it does and how syncing is set up.
Words follow the desktop's (its CLAUDE.md, Terminology): question, practice, attempt; py-fsrs's names
stay in `Fsrs.kt`.

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

@~/Projects/Android-Apps/CLAUDE.md

## Releases

Every push to `main` that passes `../.github/workflows/android.yml` (the same checks as `pixi run check`) publishes a signed APK as a GitHub release tagged `android-v<versionName>`. Obtainium on the phone installs it. Pushes that touch only docs don't release. `versionCode` is the commit count and `versionName` is `<base>.<count>`: bump the base in `app/build.gradle.kts` by hand, never hard-code `versionCode`, and never rewrite `main`'s history. CI signs with the keystore in the repo secrets, which must stay the key the installed app was signed with (the workflow's `EXPECTED_CERT_SHA256` enforces this). The shared change → emulator → push loop is in `~/Projects/Android-Apps/CLAUDE.md`.

## Architecture

- `core/` is plain Kotlin/JVM (no Android), so it is unit-tested with sqlite-jdbc (`JdbcSql` in tests). It
  talks to SQLite through the `Sql` interface. Each file ports a desktop module and is tested against
  what that module returns for the same input:
  - `Fsrs.kt` ports py-fsrs 6.3.2 (keeping its names). Times are kept as µs Longs, card JSON matches `Card.to_json`, and
    `FsrsTest` replays py's random draws to check the fuzzing too.
  - `Grading.kt` (and the calculator/LaTeX ports beside it) ports `quiz.grade` (`GradingTest`).
  - `Practice.kt` (class `Practice`) and `Gamify.kt` port `practice.py`'s queue, counts, sections
    and previews, and `gamify.py` (`QueueTest`, over `queue_snapshot.db`, a real export).
    `Practice.content` (the variant, else `ownContent`, the question's own data) reads kinds by
    their names before the rename (`Practice.KIND_ALIASES`: flashcard, numeric, written, "free", ...)
    in today's terms. `Gamify.kt` reads old achievement keys as the desktop's `ACHIEVEMENT_ALIASES` does.
  - `PracticeController.kt` ports `practice_view.PracticePanel`. It drives the desktop's own `practice.html`
    through `js` callbacks (`showOverview`, `showQuestion`, `celebrate`, `inputError`).
  - `SyncStore.kt` is the phone's half of the sync format: the `pending` event queue, batches
    (`FORMAT` 2: `attempt` events and the rest, each naming its `question`), `adopt` (take over a
    new snapshot, drop acknowledged events, replay the rest) and the local tables (`pending`,
    `buried`, `local`). A snapshot whose meta `schema` is below `SCHEMA` (2) is refused (`refusal`,
    `TOO_OLD`: update the desktop). `prepare` brings an older build's local state up to date:
    `buried (card_id)` becomes `buried (question_id)`, and queued format 1 events (`review`, `card`)
    are rewritten as format 2 (`upgraded`); batch files already written stay, since the desktop
    reads both. The desktop half and the format itself are in `../src/study_reader/phone_sync.py`;
    keep the two in step.
- `app/`:
  - `MainActivity` is a Compose shell (app bar with sync and settings) around one WebView loading
    `practice.html` through `WebViewAssetLoader`. Assets are copied from the desktop at build time
    (`copyWebAssets`), and `/media/` is served from `filesDir/media`.
  - `PracticeBridge` is `window.PracticeApp`. It has the desktop web channel's method names, and calls
    run on `Store.worker`, a single thread for all database work.
  - `Store` holds `practice.db` (snapshot copy plus local tables, behind `SwappableSql` so a new
    snapshot can replace it; an older build's `cards.db` and its side files are moved to it on first
    open, and a copy of a schema-1 snapshot is dropped so the next sync takes the folder's in again)
    and does SAF I/O on the shared folder: `flush` writes batches as
    `.syncthing.*.tmp` then renames them, `takeSnapshot` copies the snapshot and adopts it, and
    `syncMedia` mirrors the media.
  - `Reminders` is an inexact daily alarm that syncs, counts what's waiting and notifies.
- No bury on the desktop: the phone hides buried questions itself (`buried`, until the next 4 am),
  and the `bury` event moves a scheduled question's due date to tomorrow on the desktop.
