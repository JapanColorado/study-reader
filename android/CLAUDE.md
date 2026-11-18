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
(`fsrs_fixtures.py`, `grading_fixtures.py`, `queue_fixtures.py`; each takes an output path, a folder for
`queue_fixtures.py`). The desktop's `tests/test_android_fixtures.py` makes each again and fails when a
committed fixture no longer matches the desktop's code: regenerate it, then run `pixi run test` here and port
what fails.

@~/Projects/Android-Apps/CLAUDE.md

## Releases

Every push to `main` that passes `../.github/workflows/android.yml` (the same checks as `pixi run check`) publishes a signed APK as a GitHub release tagged `android-v<versionName>`. Obtainium on the phone installs it. Pushes that touch only docs, or only desktop assets the app doesn't bundle, don't release: the
workflow's "Ships" step asks Gradle (`:app:webAssetFiles`, from `shippedAssets` in `app/build.gradle.kts`,
the one list of what `copyWebAssets` copies) which desktop files the app ships. `versionCode` is the commit count and `versionName` is `<base>.<count>`: bump the base in `app/build.gradle.kts` by hand, never hard-code `versionCode`, and never rewrite `main`'s history. CI signs with the keystore in the repo secrets, which must stay the key the installed app was signed with (the workflow's `EXPECTED_CERT_SHA256` enforces this). The shared change → emulator → push loop is in `~/Projects/Android-Apps/CLAUDE.md`.

## Architecture

- `core/` is plain Kotlin/JVM (no Android), so it is unit-tested with sqlite-jdbc (`JdbcSql` in tests). It
  talks to SQLite through the `Sql` interface (a `transaction` inside another is a savepoint, `Sql.savepoint`,
  in `AndroidSql` and `JdbcSql` alike: one that throws undoes only its own changes). Each file ports a
  desktop module and is tested against what that module returns for the same input:
  - `Fsrs.kt` ports py-fsrs 6.3.2 (keeping its names). Times are kept as µs Longs, card JSON matches `Card.to_json`, and
    `FsrsTest` replays py's random draws to check the fuzzing too.
  - `Grading.kt` (and the calculator/LaTeX ports beside it) ports `quiz.grade` (`GradingTest`).
  - `Practice.kt` (class `Practice`) and `Gamify.kt` port `practice.py`'s queue, counts, sections
    and previews, and `gamify.py` (`QueueTest`, over `queue_snapshot.db`, a real export).
    `Practice.content` is the variant, else `ownContent`, the question's own data (kinds by today's
    names: a schema 2 snapshot has no others). Days are counted in the phone's time zone as it is at
    the time (`Practice.zone`). `Gamify.kt` reads old achievement keys as the desktop's
    `ACHIEVEMENT_ALIASES` does.
  - `PracticeController.kt` ports `practice_view.PracticePanel`. It drives the desktop's own `practice.html`
    through `js` callbacks (`showOverview`, `showQuestion`, `celebrate`, `inputError`).
  - `SyncStore.kt` is the phone's half of the sync format: the `pending` event queue, batches
    (`FORMAT` 2: `attempt` events and the rest, each naming its `question`; `takeBatch` marks nothing:
    `sent` marks the events once their file is written, and `sentBatches` rebuilds the sent,
    unacknowledged ones), `adopt` (take over a
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
    (`copyWebAssets`, of `shippedAssets`), and `/media/` is served from `filesDir/media`. Links in
    question text open in another app (a toast if none can). The overview is redrawn on every resume
    (each starts a sync) and whenever a sync takes in a new snapshot.
  - `PracticeBridge` is `window.PracticeApp`. It has the desktop web channel's method names, and calls
    run on `Store.worker`, a single thread for all database work.
  - `Store` holds `practice.db` (snapshot copy plus local tables, behind `SwappableSql` so a new
    snapshot can replace it; an older build's `cards.db` and its side files are moved to it on first
    open, and a copy of a schema-1 snapshot is dropped so the next sync takes the folder's in again)
    and does SAF I/O on the shared folder: `flush` writes batches as
    `.syncthing.*.tmp` then renames them (and writes again any sent batch whose file is missing),
    `takeSnapshot` copies the snapshot and adopts it, and
    `syncMedia` mirrors the media.
  - `Reminders` is an inexact daily alarm that syncs, counts what's waiting and notifies.
- No bury on the desktop: the phone hides buried questions itself (`buried`, until the next 4 am),
  and the `bury` event moves a scheduled question's due date to tomorrow on the desktop.
