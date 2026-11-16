# Study Practice (Android)

A phone companion to Study Reader for practicing your questions anywhere. It shows the same
screen as the desktop's Practice page: decks, books and their sections with due and new counts,
then one question at a time. Every question type works: self-checks (one-way or both ways),
fill-in-the-blanks, image labels, multiple choice, true/false, calculations (with MathLive's math
keyboard, graded like the desktop) and short answers (self-rated), rated Missed / Shaky / Got it /
Easy. Questions are scheduled with FSRS using your parameters, desired retention and
new-questions-a-day limit. XP, combos, the daily goal, streaks, streak freezes and achievements
work as on the desktop and add up with it. While practicing you can skip, **Flag** (Anki's seven
colors), **Bury** (until tomorrow) or **Suspend** a question. A daily reminder (Settings) notifies
you when questions are waiting.

This app was called Study Review. Updating keeps what you did on the phone and not yet synced; it
needs a Study Reader on the computer from after the rename too (it says so if the computer's is
older).

## Syncing

The phone and the computer share a folder through [Syncthing](https://syncthing.net):

1. Install Syncthing on the computer and [Syncthing-Fork](https://f-droid.org/packages/com.github.catfriend1.syncthingandroid/)
   (or Syncthing) on the phone, pair the two devices, and share one folder between them (an empty
   folder of its own, e.g. `~/Sync/study-reader` on the computer).
2. In Study Reader: **Settings → Phone sync…** → **Choose…** that folder. It writes your questions
   into it straight away.
3. In the app: **Settings → Choose folder**, and pick the same folder where Syncthing keeps it on
   the phone.

From then on the app syncs when it opens, when you tap ⟳, and before the reminder. The computer
syncs when Study Reader starts, every two minutes while it runs, and when it quits. Neither has to
be online at the same time as the other: Syncthing carries the files over when it can.

The computer stays the source of truth. It writes `desktop/snapshot.db` (your questions,
schedules, attempts and practice settings) and `desktop/media/` (question images); the phone
writes only small files of what it did under `phones/<device>/`. Each side writes only its own
files, so Syncthing never has a conflict. The computer applies each attempt from the phone at the
time it happened, so questions you practice on both between syncs end up as if you had done all
the attempts in one place. Edits, new questions and setting changes come from the computer.

## Building

Every push to `main` that passes CI publishes a signed APK on the [Releases page](https://github.com/JapanColorado/study-reader/releases). To install it and get updates automatically, add `https://github.com/JapanColorado/study-reader` as a GitHub source in [Obtainium](https://github.com/ImranR98/Obtainium).

Everything goes through pixi (it provides the JDK):

```sh
pixi run test           # JVM tests of the shared logic, checked against the desktop's own results
pixi run build          # release APK (debug-signed unless keystore.properties exists)
pixi run install        # build and install on the connected phone
pixi run run            # install and launch
```

The practice page and its scripts (`practice.html`, `quiz.js`, `markdown.js`, KaTeX, MathLive …) are
copied from `../src/study_reader/assets/` at build time, so rebuild after changing them.
