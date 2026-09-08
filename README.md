# Milky Way Idle Auto Labyrinth

Playwright-based automation for Milky Way Idle labyrinth runs.

## Setup

Use Python 3.10+:

~~~bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
cp accounts.example.json accounts.json
~~~

Edit your local accounts.json: replace YOUR_CHARACTER_ID, choose an account
label, and point state_file to your own Playwright storage-state JSON.
The example contains placeholders and cannot be used to log in.

For a browser you already started with a local Chrome debugging endpoint,
export its signed-in state using:

~~~bash
python3 milkyway_autolabyrinth.py save-state \
  --cdp-url http://127.0.0.1:9222 --output mwidle_state_example.json
~~~

The debugging endpoint must be available first. Keep both the endpoint and
the exported state private.

## Run

~~~bash
python3 milkyway_autolabyrinth.py run --accounts accounts.json
~~~

The script passively watches the existing game WebSocket for completed
/actions/labyrinth/explore actions in action_completed or actions_updated,
and for labyrinth_updated changing isActive from true to false. A signal
wakes the worker to verify the page, claim or escape the finished run, and
enter/start the next run in the same maintenance pass. It does not create
another game WebSocket or send custom labyrinth packets.

An initial UI check and a fallback check every 600 seconds cover reconnects
and missed messages. Routine DOM inspection does not send extra requests; the
new approach mainly avoids repeated UI navigation. A failed check retries after
20 seconds by default. Duplicate completion messages are coalesced, and
notifications caused by the script's own maintenance are suppressed.

Useful options:

| Option | Effect |
| --- | --- |
| --watchdog-sec 300 | Change the event mode fallback interval |
| --no-event-driven --delay-sec 60 | Use the previous polling mode |
| --startup-timeout-ms 90000 | Allow longer page navigation and shell startup waits |
| --browser-channel chrome | Use an installed Google Chrome |
| --no-headless | Show the automation browser |
| --account example_account | Select one configured account |
| --loops 2 | Stop after two maintenance passes; this is not a round count |

Periodic page refresh and context recycling default to disabled. Recovery still
recreates a failed context and attaches listeners before navigating.

Connections are direct by default. To use a SOCKS proxy already running locally,
including Tor Browser's proxy:

~~~bash
python3 milkyway_autolabyrinth.py run --accounts accounts.json \
  --proxy-server socks5://127.0.0.1:9150
~~~

This option applies to the automation browser only. It does not launch Tor,
rotate exits, or change macOS network settings.

## Verification

Offline unit tests use synthetic packets and mocked browser operations:

~~~bash
python3 -m unittest discover -s tests -v
~~~

Local browser navigation checks use synthetic HTML with no account login:

~~~bash
python3 test_labyrinth_navigation_recovery.py
~~~

## Private data

Only source code, documentation, tests, and a placeholder example belong in
this repository. Real account configurations, browser state, environment files,
keys, logs, screenshots, profiles, and diagnostic outputs are ignored by Git.
Ignored files can still be force-added; review the staged files before pushing.

The probe command, diagnose_labyrinth_escape.py, and inspect_escape_dialog.py
create local diagnostics that can contain account information. Do not publish
those outputs.

This repository contains only labyrinth automation. It has no Cowbell/checkout
automation and no third-party verification service.
