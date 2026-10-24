# 2.0.0: key capture

Capture the 7.1+ cutscene keys the game server sends at login, inside charlotte, and show them in a GUI dialog ready to paste into keys.json. This folds `../cutscene-key` into charlotte and retires it.

## What the user gets

- A **Capture keys** button in the GUI opens a dialog. **Start** shows a UAC prompt, then the dialog says to launch the game and log in.
- Each key prints in real time as a keys.json group, in selectable monospace text, with a **Copy all** button at the top right:

  ```json
  {
    "audioKey": 5281416956962385,
    "aesKey": "f9e9e1c5cf3a68deffa0a4d3df665836",
    "parentQuestId": "7017",
    "videos": ["Cs_ZD_AQ70171401_UR_Boy", "Cs_ZD_AQ70171401_UR_Girl"]
  }
  ```

- **Only new keys**: "Show only keys that are not in keys.json." It filters live and needs no restart.
- Nothing is saved anywhere. keys.json and `recovered_keys.json` are never touched.
- A status line covers: waiting for login, decrypting, keys found, and warnings (packets lost, log in again, turn off RSC).
- Notes in the dialog: start before logging in; only quests active on this account send keys; Windows 10 support is best effort and untested.
- Console users get the same with `charlotte-cli --capture`, which relaunches itself elevated in a new window and prints the same groups. Ctrl+C stops it.

## Decisions already made

| Decision | Reason |
|---|---|
| Capture lives in the engine, cutscene-key is retired | No new repo, and the engine stays complete without the GUI |
| GUI stays unelevated, only the capture engine runs elevated (UAC per capture) | An elevated GUI loses drag-and-drop from Explorer (UIPI), and the main window takes dropped files |
| Elevated engine talks to the GUI over a named pipe | An elevated child can't inherit redirected stdio from an unelevated parent |
| `videos` comes from trial decryption against the game's own `VideoAssets` folder, else `[]` | Quest-id name matching got 100 of 111 groups exact, and reunion videos have no quest digits |
| Windows 11 interface first, Windows 10 fallback ported from the pktmon crate, with a disclaimer | Windows 10 still has users, and the game can't be tested on Windows 10 here |
| Antivirus risk accepted | User's call (2026-10-20) |

## Measured facts the design rests on (2026-10-20, this machine)

- **Packet size:** the real-time stream honours a truncation size up to **9000**. Anything above is accepted by the create call and silently becomes 128, which cuts every packet short. The engine must compare the started event's `truncation_size` with what it asked for and fail loudly on a mismatch.
- **Clean login:** at 9000 the login decrypted, found 7017/7019/7020, dropped 0 packets and had no reassembly gaps. The largest packet was 1262 bytes despite RSC and UDP receive offload being on. The >9000 merged packets from Irminsul [#153](https://github.com/konkers/irminsul/issues/153) and [#163](https://github.com/konkers/irminsul/issues/163) don't occur here, so their fix stays untested.
- **Duplicates:** every inbound packet appears once as Ethernet (type 1) and four times as raw Wi-Fi frames (type 2). The Ethernet copy alone is enough.
- **GIL:** the buffer is 2.6 MB at multiplier 10, and the peak burst was 38 MB/s of captured data. Main-thread work for the whole login took 0.16 s. Its one long step, the 69 ms session-key search, ran while traffic was near zero.
- **Trial decryption:** a consistent VP9 superframe index after AES decryption. Over the first 300 frames the right key passed 26–49 on all 10 `USM/7.1` files, and every wrong key passed 0.
- **Windows 10 exports:** this machine's `PktMonApi.dll` (10.0.26100) exports both the Windows 11 `PacketMonitor*` functions and the older `Pktmon*` ones, so the Windows 10 path can be forced here.

## Engine (charlotte)

### Layout

A new top-level `capture/` package. It's a run mode like `--crack` rather than a pipeline stage, and it imports from `stages/` (the USM reader) and `resources/` (keys, game install), so it can't live in `resources/`.

| File | Contents | Source |
|---|---|---|
| `capture/pktmon.py` | Windows 11 real-time backend | cutscene-key `capture.py`, plus event-callback structs from `pktmon.h` |
| `capture/pktmon_legacy.py` | Windows 10 backend: `Pktmon*` driver calls plus an ETW real-time consumer | pktmon crate `src/legacy/` (driver.rs, c_filter.rs, etw.rs) |
| `capture/net.py` | Ethernet / raw IP to UDP payload | cutscene-key `pcap.udp` (the .pcap file readers are dropped) |
| `capture/sniffer.py` | KCP reassembly, dispatch and session decryption | cutscene-key `sniffer.py` and `crypto.py` |
| `capture/proto.py` | Shape-based key search | cutscene-key `proto.py` |
| `capture/match.py` | Game folder scan and superframe test | new |
| `capture/run.py` | The capture loop, events and stopping | cutscene-key `main.py`, reshaped for the reporter |
| `capture/secrets/` | `gi.json`, `private_key_4.pem`, `private_key_5.pem` | cutscene-key `keys/` (from Irminsul and auto-artifactarium, both MIT) |
| `resources/game.py` | `game_install()` from the registry, shared by fonts and capture | split out of `resources/fonts.py` |
| `utils/elevation.py` | `is_admin()`, `relaunch_as_admin()` | cutscene-key `capture.py` |

- **Crypto:** the RSA step moves from `cryptography` to pycryptodome's `PKCS1_v1_5`, which charlotte already ships. No new dependencies.
- **Lazy imports:** numpy, AES and RSA are `lazy` imports, so `--version` and `--probe` stay fast.
- **Bundling:** `charlotte.spec` bundles `capture/secrets/`.

### CLI

- **`--capture`**: a fifth mutually exclusive mode, rejected together with input files, `--probe`, `--crack`, `--update` and `--key`.
- **`--pipe NAME`**: hidden, and only valid with `--capture` and `--json`. The GUI passes it.
- **Without admin rights:**
  - With `--pipe`, it fails with an error event, because the GUI is supposed to elevate.
  - From a console, it relaunches through `runas` in a new window that waits for Enter before closing.
- **Exit code:** 0 for a normal stop, including Stop and Ctrl+C. 1 when capture couldn't start.

### Capture loop (`capture/run.py`)

1. **Load keys.json** with `Keys(reporter)`. It bootstraps when the file is missing, and the "only new" flag needs it. Its update prompt must not fire here. Either pass a flag, or compare only against what's on disk plus the bootstrap.
2. **Scan the game folder before the login,** while the line is quiet: list `VideoAssets/StandaloneWindows64/*.usm`, drop stems keys.json already lists, and run `video_nonce()` on the rest to keep the 7.1-scheme files. If there's no install, log it and every key gets `videos: []`.
3. **Open the backend:** Windows 11 first. If `PacketMonitorInitialize` is missing or fails, fall back to legacy, and report which one is in use. Then:
   - Verify the started event's `truncation_size` equals 9000.
   - Raise `buffer_size_multiplier` and read back the reported buffer size. Find the honoured maximum the same way as the truncation sweep.
   - Keep only Ethernet and raw-IP frames.
4. **Main loop:** feed frames to the sniffer, and run `find_keys` on server messages. Call `reporter.checkpoint()` at least every 0.5 s.
5. **For each new key:**
   - If its `aesKey` is in keys.json: `known: true`, with `videos` and `parentQuestId` taken from keys.json.
   - Otherwise: hold the key until 2 s after the last key, run the trial decryption and emit it once with its `videos`. A `pending` first emission was dropped on 2026-10-23, because it saved only those 2 s and cost a field, a second event and an upsert in the GUI.
   - The trial batches every pending key per file read, so the login burst never competes with file I/O and AES for the GIL.
6. **Warnings** go to the reporter as translated log lines:
   - packets lost (the drop counters)
   - a `ProcessInfo` warning, which means a packet over the size limit
   - a login that wasn't seen ("start before logging in")
   - a session key that wasn't recovered ("log in again")
   - a missing dispatch key
   - a missed-packet total over a threshold, which also suggests turning off RSC
7. **Stop** on a `cancel` command, Ctrl+C, or the pipe closing (see below). Close every PktMon/ETW handle, and print the totals.

### Sniffer changes from cutscene-key

- **Skip reassembly gaps.** A segment lost in capture never comes back, because the game doesn't resend what it received, so today's `Kcp` stalls for the rest of the session. When later segments pile up past a missing `sn`, jump to the segment after the first pending one with `frg == 0`, which is a message boundary. That drops only the message that had the gap. Each message is XORed from its own start, so later messages still decrypt.
- **A failed message isn't fatal.** Under the session key, a message that fails to parse is counted and skipped instead of killing the sniffer.
- **Sniffer statuses become events or log lines** instead of `typer.echo`.

### Matching (`capture/match.py`)

- **`video_sample(path)`** reads the first 300 video payloads once per file and notes where each IVF frame sits (32-byte file header on the first payload, 12-byte frame header on each), because the frame length is in the clear part. A frame ending inside the clear 0x40 bytes is dropped, because its index would pass under any key. The corpus has 3,301 such frames in the first 300 and none is a superframe, but the smallest superframe is only 80 bytes (2026-10-22).
- **`decrypts(sample, nonce, aes_key) -> bool`** decrypts each payload with `decrypt_stream` and counts frames that end in a consistent superframe index: marker byte, repeated byte at `len - index_size`, and sizes adding up. Decrypting only each frame's last 34 bytes took a wrong key from 12.9 ms to 2.4 ms per file (2026-10-22), and was dropped on 2026-10-23 because it needed an `offset` parameter on `decrypt_stream` in a core stage for a saving the file reads dwarf.
- **Accept at 3 or more**, since right keys scored 26–49 and wrong keys 0.
- **Prototype:** `verify71.py` in the session scratchpad.

### Pipe protocol

- **Setup:** the GUI creates a duplex byte-mode pipe `\\.\pipe\charlotte-capture-<guid>` and passes its name. The engine opens it with `open(..., "r+b", buffering=0)` and builds `JsonReporter(out=TextIOWrapper(pipe, write_through=True), stdin=pipe)`. `pipe_peek` works on a named pipe client handle, so cancel polling is unchanged.
- **Pipe closing means stop, in capture mode only.** That reverses the usual "EOF doesn't cancel" rule. The elevated engine is outside the GUI's Job Object, and an unelevated GUI can't kill it, so the pipe is the only thing that can end it when the GUI dies. `JsonReporter` gets a flag that turns a hang-up into `cancelled`.
- **stderr:** nobody reads the elevated process's stderr. Under `--pipe`, a logging handler forwards records as `log` events, and `main.py` turns an unhandled exception into an `error` event before exiting.

### Events (new kinds, fixed fields)

| Kind | Fields | When |
|---|---|---|
| `capture_key` | `video_key` (old scheme, else null), `audio_key` and `aes_key` (7.1, else null), `parent_quest_id` (string or null), `known`, `videos` | Once per key, a new 7.1 key after its match. The GUI appends it and renders an old key as `{"videoKey", "parentQuestId", "videos"}`. |

The status text goes out as `log` events, already translated. The dialog's status shows the latest one, and `summarize()` logs the count before any warning or error, which leaves the problem showing at the end. A `capture_hint` event for the one warning the dialog showed live was dropped on 2026-10-21, because a held-back status hid the other mid-run errors and kept the "log in again" warning after a successful relogin. An existing `error` event covers a failed start. A `capture_start` event carrying the backend, truncation and buffer was dropped on 2026-10-21 because neither the console nor the dialog read it. If the Windows 10 backend needs to show which interface ran, that goes in a log line.

### Tests (`tests/test_capture_*.py`)

- **Ported pins, kept:** `dotnet_random` against real `System.Random` output, `session_key` against auto-artifactarium, proto shape matching, sniffer handshake and decryption, and net frame parsing.
- **New:**
  - gap skipping resumes at the next message boundary
  - a failed message doesn't kill the session
  - a truncation mismatch fails loudly (fake API)
  - superframe acceptance on a synthetic frame against random bytes
  - `--capture` rejected with the other modes
  - the hang-up-cancels flag
- **Strings:** `test_strings.py` covers the new keys automatically.

### Docs and strings

- **Strings:** new keys in `locales/en-US.json` with all seven translations, then `crowdin upload sources` and `crowdin upload translations`.
- **README:**
  - a capture section with the Windows 10 disclaimer and the RSC hint
  - credits for auto-artifactarium, Irminsul's `gi.json` and the pktmon crate's layouts
  - their MIT notices in a third-party notice file
- **CLAUDE.md:**
  - a "Key capture" section covering the backends, the 9000 cap and its silent fallback, the pipe and its hang-up rule, matching, and the events
  - five run modes instead of four
  - `capture/` in the layout

## GUI (MonsieurVerite)

| File | Change |
|---|---|
| `Views/MainWindow.xaml` | A **Capture keys** button, disabled while a batch runs |
| `Views/CaptureDialog.xaml(.cs)` | Instructions, Start/Stop, **Only new keys** with its description, read-only monospace `TextBox`, **Copy all**, status line |
| `Engine/CaptureClient.cs` | Creates the pipe, starts the engine through `runas`, pumps NDJSON from the pipe into `EngineEvent.Parse`, sends `cancel`, disposes the pipe on close |
| `Engine/EngineEvent.cs` | `CaptureKeyEvent`, `CaptureHintEvent` |
| `Strings` / `gui.json` | New keys plus seven translations, uploaded to Crowdin |
| `EngineEventTests` | Wire-format pins for the three new kinds |

- **Starting:** `UseShellExecute = true`, `Verb = "runas"`, `WindowStyle = Hidden`. Arguments: `--capture --json --pipe <name>`, and `CHARLOTTE_LANG` through the environment.
- **UAC cancelled:** a `Win32Exception` with code 1223 shows "Capture needs administrator rights" and leaves the dialog open.
- **Rendering:** the text box shows the groups in arrival order, re-rendered on each new key or filter change. The engine's `CAPTURE_MATCHING` log line is the status while files are matched.
- **Errors:** the dialog shows an `error` event as a warning line. The engine does not send the same text again as a `log` event (`sent_as_event` on the record, checked by `ReporterHandler`).
- **Closing:** closing the dialog sends `cancel` and then closes the pipe, which stops the engine even if the cancel is lost.
- **Language:** pass the GUI's language as the hidden `--lang` flag, because a process started through UAC does not inherit the environment `CHARLOTTE_LANG` would ride in. The engine's console relaunch passes nothing, because the elevated window follows the same Windows display language as the console that asked, and only a hand-set `CHARLOTTE_LANG` would differ (dropped 2026-10-24). The flag is fine here, unlike for conversions, because no engine older than 2.0.0 knows `--capture` anyway.
- **Pipe name:** pass only the bare name after `\\.\pipe\`. The engine refuses anything with a slash, backslash or colon, so an elevated engine never opens a path it was handed.

## Order of work

1. **Engine core:** `capture/` with the Windows 11 backend, sniffer with gap skipping, crypto on pycryptodome, the ported tests, console `--capture` with elevation.
2. **Matching:** game folder scan, superframe test, the deferred batch.
3. **Pipe mode:** `--pipe`, the `JsonReporter` hang-up flag, the log forwarding, the events.
4. **GUI:** dialog, client, events, strings, tests.
5. **Windows 10 backend:** the port, forced on this machine with a hidden switch.
6. **Live checks on this machine:**
   - console capture
   - GUI capture, including the UAC cancel and closing mid-capture
   - the forced legacy backend
   - a login with the game already running, which should give a clear message
7. **Docs, translations, CLAUDE.md in both repos, release notes.** Bump `pyproject.toml` to 2.0.0, then tag the GUI first.

## Still unknown, to settle during the work

- **Old-scheme keys are not matched (settled 2026-10-21).** The old mask decrypts every other 32-byte block without the key, so a superframe index there passes under any key. Over 48 old files a wrong key passed on up to 22 of 300 frames and one right key on only 10, and `--crack` recovers those keys from the files anyway. A new old-scheme key is listed with `videos: []`.
- **Reunion keys:** whether a login catches them. In 6.x the reunion key was a string field in another message, and `find_keys` wants a u64 plus hex in one record.
- **Legacy packet size (settled 2026-10-21).** The crate's unnamed second `u32` of the `PktmonStart` struct sits where `pktmon start --pkt-size` would go, and zero gives whole packets. An elevated probe filtered TCP 443 during a 20 MB HTTPS download, and all 10,659 payload events had `LoggedPayloadSize` equal to `OriginalPayloadSize`, the largest at 62,906 bytes (coalesced segments). The Windows 10 path therefore has no 9000-byte cap, and the merged packets behind Irminsul #153/#163 would arrive whole there, while the Windows 11 stream drops them.
- **Legacy throughput (2026-10-21, same probe).** The Python ETW callback, with its TDH reads by name, took 9.9 µs per event, 0.1 s for the whole download at about 44 MB/s, and ETW reported 0 events lost. Event 160 carries `PacketType`, `LoggedPayloadSize`, `OriginalPayloadSize` and `ComponentId` under those names here, and Windows 10's manifest is unchecked. 2,445 events were Ethernet and 8,214 raw Wi-Fi copies, the same duplication as Windows 11.
- **Legacy recovery from a crash (2026-10-21).** A probe killed mid-capture left the `charlotte-pktmon` session running and the driver capturing with its filter. The next start stopped both before starting its own, and its close left no session and the driver stopped.
- **Admin rights from a different account** (UAC credentials typed by someone else): HKCU then belongs to that account, so the game install isn't found and every key gets `videos: []`. Acceptable, but say so in the log.
- **How to force the legacy backend for testing (settled 2026-10-21):** the hidden `--legacy-capture` flag, which the user chose over an environment variable.

## Progress (2026-10-21)

Nothing is committed in either repo. The user commits after their own testing pass.

**Done, tests green (charlotte 297, MonsieurVerite 144), ruff and `dotnet build` clean:**

- **Step 1, engine core.** `capture/` (pktmon, net, crypto, proto, sniffer, run), `utils/elevation.py`, console `--capture` with UAC relaunch and a hidden `--pause`. Verified live twice in the console.
- **Additions agreed after step 1.**
  - **Old-scheme keys.** `find_keys` also takes old-scheme keys: a parent-quest record with one decimal string and no hex. They print as `{"videoKey", "parentQuestId", "videos"}`, and `capture_key` gained `video_key`. A live login gave 79 keys (76 old, 3 from 7.1), all known, with videos from keys.json.
  - **Session-key recovery retries.** It keeps messages until the login reply arrives and tries ±10 s, nearest first in 3 s chunks, on up to 3 messages. A ±1.5 s first pass followed by a ±10 s one was collapsed into this on 2026-10-23, because the nearest-first order already finds a near key first and the wide pass only costs on a message that fails. It names the cause (`CAPTURE_LOGIN_REPLY_MISSED` or `CAPTURE_SESSION_KEY_OUT_OF_WINDOW`), and "log in again" (`CAPTURE_NOTHING_DECRYPTED`) comes only at the end. This came from one live login that failed recovery with the cause unknown, which is the user's first restart in the 2026-10-21 console test.
- **Step 2, matching.** `resources/game.py` (shared with fonts) and `capture/match.py`. New 7.1 keys are matched 2 s after the last key, or at stop, and go out then. On the real game folder every 7.1 key found exactly its files, and the reunion 7.1 AES key also decrypts `Video_Reunion_67_test`. Old-scheme keys are not matched, see above.
- **Step 3, pipe mode.** Hidden `--pipe NAME` (bare name only) and `--lang`. `open_pipe` in `utils/reporter/json.py` gives a `JsonReporter` with `hangup_cancels`. `ReporterHandler` forwards log lines, and an unexpected exception sends a `CAPTURE_CRASHED` error event. `capture_stats` was dropped, because the GUI has no use for it and every event kind must be consumed.
- **Step 4, GUI.** `Engine/CaptureClient.cs`, `ViewModels/KeyCapture.cs`, `Views/CaptureDialog.xaml(.cs)`, `CaptureKeyEvent`, `Settings.CaptureNewKeysOnly` (default true), the overflow-menu item "Capture keys…", and 12 strings in 8 languages. Both repos pin `--capture --json --pipe <name> --lang <language>` in a test.
- **Frozen exe.** Built 2026-10-20 22:14 into `dist/` (107 MB, still reports 1.2.0). Its TOC lists the capture modules, pycryptodome's RSA/PKCS1_v1_5 and `capture/keys/*` (renamed `capture/secrets/` on 2026-10-21, so rebuild). Run unelevated, `--capture --json` returns the admin error event as intended. The debug GUI's `settings.json` points `EnginePath` at `charlotte\dist\charlotte-cli.exe`.

**Next:**

1. **The user's live test of the capture dialog** in the debug GUI (`MonsieurVerite\MonsieurVerite\bin\Debug\net10.0-windows\charlotte-gui.exe`, menu … → Capture keys…). Check:
   - the UAC prompt, the status lines, the "only new" filter, Stop/Stopping
   - Copy all
   - declining UAC
   - closing the dialog mid-capture, which should end the elevated engine
   - whether `--lang` reaches the elevated engine
2. **Step 5, Windows 10 legacy backend.** Built in `capture/pktmon_legacy.py`, forced by `--legacy-capture` and chosen on its own when `PktMonApi.dll` lacks `PacketMonitorInitialize`, which logs `CAPTURE_LEGACY_BACKEND`. The probe above passed, and a real game login with `--capture --legacy-capture` printed its keys on 2026-10-21 (the user closed the window before pasting the output, so its warnings and count were not seen). No `charlotte-pktmon` session was left afterwards. Windows 10 itself stays untested.
3. **Step 7, docs.**
   - CLAUDE.md in both repos: a Key capture section, five run modes, the layout lists (capture/, CaptureClient, KeyCapture, CaptureDialog), the pipe hang-up rule, the 9000-byte cap.
   - README with the Windows 10 disclaimer and credits, plus third-party MIT notices.
   - `crowdin upload sources` and `crowdin upload translations` for both files.
   - Release notes, then `pyproject.toml` to 2.0.0 and tag the GUI first.

**Deferred by the user (2026-10-21), a separate GUI bug:** a probe or convert of many files with long paths exceeds Windows' 32,767-character command line. 278 game-folder files make 47,375 characters, so the engine never starts. The agreed fix is to run the engine with its working directory set to the folder holding most targets, pass those files as bare names and others as full paths, and resolve `--output` to a full path against the engine's folder (a relative one would otherwise land in the game folder). No engine change is needed, because charlotte finds keys.json, Subtitle/, font/, ffmpeg and vs/ from the exe's location.
