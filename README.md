# packetnodebot

A Python asyncio bot that bridges a ham-radio packet node and an online chat service. A privileged "sysop" user direct-messages the bot to issue node commands, watch live monitor traffic, set callsign alerts, or open a raw telnet passthru into the node.

The long-term goal is to support a range of node software — [BPQ](https://www.cantab.net/users/john.wiseman/Documents/index.html) is the first implementation, with XRouter and potentially others to follow.

Currently only BPQ on the node side and Discord on the chat side are implemented, and only a single sysop user is supported. Anyone authorised to talk to the bot inherits the configured telnet/FBB credentials — only register a user you trust.

## bpqnodebot

Once running, bpqnodebot maintains two on-demand TCP connections to your BPQ node:

- An **FBB** connection (binary protocol, typically port 8011) used for the live monitor stream and callsign alerts. Opened only when monitoring or alerts are active and torn down when they're all off.
- A **telnet** connection (text, typically port 8010 or the FBB port — see below) used only while the sysop is in `telnet` passthru mode.

From a chat DM the sysop can:

- Toggle a live monitor feed of packets seen on selected ports, with optional from/to call filtering.
- Receive alerts when specific callsigns are heard on air, or when they connect to your node.
- Drop into a raw telnet session with the node — every message typed is forwarded straight to the node, every line the node sends comes back as a DM.
- Shut the bot down remotely.

## Quickstart

### Install

Requires Python 3.8+.

```bash
git clone https://github.com/allthefurlongs/packetnodebot.git
cd packetnodebot
python -m venv venv
source venv/bin/activate
pip install -e .
```

Then copy the sample config:

```bash
cp packetnodebot.yaml.sample packetnodebot.yaml
```

### Configure BPQ access

Edit `packetnodebot.yaml`. The two BPQ-side blocks you must fill in are `bpq:` (host/port/user/pass for FBB and telnet) and `node_callsigns:` (your node's AX.25 callsigns, used by the "connected" alert to detect inbound connects).

```yaml
node_callsigns: [N0CALL, N0CALL-2]
bpq:
  fbb_host: 127.0.0.1
  fbb_port: 8011
  fbb_user: N0CALL
  fbb_pass: yourpass
  telnet_host: 127.0.0.1
  telnet_port: 8011      # see note below
  telnet_user: N0CALL
  telnet_pass: yourpass
```

**Note on `telnet_port`:** you can point this at BPQ's plain telnet port (typically 8010) or at the FBB port (8011). Pointing it at the FBB port is recommended — using the plain telnet port causes BPQ to drop the node-level session after ~15 minutes of idle, even though the underlying TCP connection stays up.

### Link Discord

1. **Create a Discord application/bot.** Go to <https://discord.com/developers/applications>, create a new application, add a Bot user, and copy the **bot token**.
2. **Enable required intents** on the bot page in the developer portal:
   - `MESSAGE CONTENT INTENT`
   - `SERVER MEMBERS INTENT`
3. **Invite the bot to a server you share with your sysop account.** The bot only DMs the sysop, but Discord generally requires a shared server before a bot can DM a user. Generate an OAuth2 invite URL with at least the `bot` scope and invite it to any server you and the bot will both be in.
4. **Paste the token** into `packetnodebot.yaml`:
   ```yaml
   bot_connector: discord
   discord:
     token: your-bot-token-here
   ```
5. **Register yourself as the sysop.** You have two options:

   **Option A — password registration (easier):** set a one-time password in the config:
   ```yaml
   discord:
     token: your-bot-token-here
     register_sysop_user_id_bot_password: some-long-random-string
   ```
   Then start the bot, DM it `register some-long-random-string`, and it will record your Discord user ID into the YAML file under `sysop_user_id`. From then on, only that user ID can talk to the bot. You can remove the `register_sysop_user_id_bot_password` line afterwards.

   **Option B — hard-code your user ID:** if you already know your Discord user ID (enable Developer Mode in Discord, right-click your name → Copy User ID), set it directly:
   ```yaml
   discord:
     token: your-bot-token-here
     sysop_user_id: 123456789012345678
   ```

### Run

From the directory containing `packetnodebot.yaml`:

```bash
bpqnodebot
```

The bot logs to stdout. Once you see `Connected to discord as ...` and (after registration) receive a `Bot Online` DM, you're set — try DMing it `help`.

### Running as a systemd service

To keep the bot running across reboots, drop a unit file at `/etc/systemd/system/bpqnodebot.service`:

```ini
[Unit]
Description=bpqnodebot
After=network.target

[Service]
WorkingDirectory=/home/youruser/packetnodebot
User=youruser
Group=youruser
ExecStart=/home/youruser/packetnodebot/venv/bin/bpqnodebot
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
```

Replace `youruser` with the user that owns the checkout and venv, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bpqnodebot
sudo journalctl -u bpqnodebot -f    # tail the logs
```

## Commands

All commands are sent as Discord DMs from the registered sysop user. Commands are case-insensitive, as are callsigns (the bot uppercases them before use).

### Command summary

| Command | Purpose |
| --- | --- |
| `help` (also `?`, `#help`) | Show the command list. |
| `fixed <on\|off>` | Wrap bot output in a Discord code block for monospaced rendering. |
| `telnet` | Open a raw telnet passthru into the node. `#quit` exits. |
| `monitor <on\|off>` | Stream live packets from monitored ports into the chat. |
| `monports [add\|del <portnum>]` | Show / change which BPQ port numbers are monitored. |
| `monfilter [add\|del <from\|to> <call>]` | Show / change call filters that exclude traffic from the monitor stream. |
| `alert call seen <callsign>` | Alert whenever `<callsign>` appears in a monitored frame. |
| `alert call connected <callsign>` | Alert whenever `<callsign>` connects to the node. |
| `alert cooldown <seconds>` | Suppress duplicate alerts for this many seconds (`0` disables). |
| `remove alert call seen <callsign>` | Remove a "seen" alert. |
| `remove alert call connected <callsign>` | Remove a "connected" alert. |
| `terminate bot` | Shut the bot down (requires `yes` confirmation). |

### Command details

#### `help`
Prints the command list. `?` and `#help` are aliases. The exact formatting depends on the current `fixed` setting.

#### `fixed <on|off>`
When `on`, all subsequent bot output is wrapped in a Discord triple-backtick code block so it renders in a monospaced font — useful for monitor output and tabular node responses. When `off`, messages are sent as plain text.

This setting can also be set at startup via the `fixed_width_font` config option.

#### `telnet`
Opens a raw passthru telnet session to the node. After the bot reports `Entering telnet passthru mode`, every message you send is forwarded as a line to the node, and every line the node sends is DMed back. Send `#quit` to close the session and return to normal bot command mode.

While in passthru mode, no bot commands are interpreted — only the literal `#quit` is intercepted locally.

#### `monitor <on|off>`
Turns the live monitor feed on or off. When on, the bot subscribes to BPQ's monitor stream (over the FBB connection) for the configured port set and forwards each frame as a DM, prefixed with `Monitor:`.

Without arguments, prints the current state.

#### `monports [add|del <portnum>]`
Shows the current list of BPQ port numbers being monitored, or adds/removes a port from that list. Changes take effect immediately if monitoring is already active.

```
monports             # show current ports
monports add 1       # also monitor port 1
monports del 2       # stop monitoring port 2
```

#### `monfilter [add|del <from|to> <call>]`
Filters the monitor feed by source/destination callsign. A frame is suppressed if its `from` matches any call in the `from` filter, or if its `to` (or any digipeater in the path) matches any call in the `to` filter. Without arguments, prints the current filter sets.

```
monfilter add to N0CALL      # hide frames addressed to or routed through N0CALL
monfilter add from N0CALL    # hide frames originated by N0CALL
monfilter del to N0CALL
```

Filters affect monitor output only — alerts still fire regardless of monitor filters.

#### `alert call seen <callsign>`
Adds an alert that fires whenever `<callsign>` appears anywhere in a monitored frame's address fields (from, to, or any digipeater). The bot DMs `ALERT: <call> seen on air on port <n>`.

This implicitly enables the FBB connection if it isn't already up.

#### `alert call connected <callsign>`
Adds an alert that fires when `<callsign>` initiates a connect (a `SABM` frame) addressed to one of your `node_callsigns`. The bot DMs `ALERT: <call> connecting to <node_call> on port <n>`.

If `node_callsigns` is empty in the config, this alert can never fire — the bot will warn you.

#### `alert cooldown <seconds>`
Minimum gap, in seconds, between duplicate alerts for the same `(alert-type, callsign)` pair. `0` disables the cooldown entirely. Defaults to the config value (or 300s if unset). Without arguments, prints the current cooldown.

#### `remove alert call seen <callsign>` / `remove alert call connected <callsign>`
Removes a previously added alert. If no other alerts and no `monitor` are active afterwards, the FBB connection is torn down.

#### `terminate bot`
Asks for `yes` confirmation, then shuts the bot down cleanly. You'll need shell access to the host to restart it.

## Configuration

### Session settings (set via chat)

These are runtime-only — they are **not** persisted to the YAML file and reset to their configured defaults on restart. To make a change permanent, edit the YAML.

| Setting | Chat command | Config key |
| --- | --- | --- |
| Fixed-width output | `fixed on` / `fixed off` | `fixed_width_font` |
| Monitor on/off | `monitor on` / `monitor off` | `bpq.monitor_on_startup` |
| Monitored port list | `monports add\|del <n>` | `bpq.monitor_ports` |
| Monitor from/to filters | `monfilter add\|del from\|to <call>` | `bpq.mon_filter.from` / `bpq.mon_filter.to` |
| Callsign-seen alerts | `alert call seen <call>` | *(not persisted — there is no config key)* |
| Callsign-connected alerts | `alert call connected <call>` | *(not persisted — there is no config key)* |
| Alert cooldown | `alert cooldown <secs>` | `bpq.alert_cooldown` |

Note that the alert lists themselves are runtime-only at the moment — they need to be re-added after each restart.

### Configuration file settings

The active config lives at `packetnodebot.yaml` in the working directory the bot was started from. A documented sample is in `packetnodebot.yaml.sample`.

#### Top-level

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `node_callsigns` | for connected alerts | `[]` | List of AX.25 callsigns your node accepts inbound connects on (e.g. base call + SSID variants). Used to detect SABM-to-node frames for `alert call connected`. Compared case-insensitively. |
| `fixed_width_font` | no | `false` | Start with fixed-width (code-block) output enabled. Equivalent to running `fixed on` after startup. |
| `bot_connector` | yes | — | Which chat backend to use. Currently only `discord` is supported. |

#### `bpq:` block

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `fbb_host` | yes | — | BPQ FBB listener host. |
| `fbb_port` | yes | — | BPQ FBB listener port (typically 8011). |
| `fbb_user` | yes | — | FBB username. |
| `fbb_pass` | yes | — | FBB password. |
| `telnet_host` | for `telnet` command | — | BPQ telnet listener host. |
| `telnet_port` | for `telnet` command | — | BPQ telnet listener port. **Prefer the FBB port** (e.g. 8011) over the plain telnet port (8010) to avoid node-level idle timeouts — see the [Configure BPQ access](#configure-bpq-access) section. |
| `telnet_user` | for `telnet` command | — | Telnet username. |
| `telnet_pass` | for `telnet` command | — | Telnet password. |
| `monitor_on_startup` | no | `false` | If true, the bot starts with `monitor on` already active and opens the FBB connection immediately. |
| `monitor_ports` | no | `[]` | List of BPQ port numbers (integers) to subscribe to in the monitor stream. |
| `mon_filter.from` | no | `[]` | List of callsigns whose **source** matches will be excluded from the monitor stream. |
| `mon_filter.to` | no | `[]` | List of callsigns whose **destination** (or any digipeater hop) matches will be excluded from the monitor stream. |
| `fbb_reconnect_initial_delay` | no | `5` | Seconds to wait before the first FBB reconnect attempt after a drop. |
| `fbb_reconnect_max_delay` | no | `60` | Cap on the exponential backoff between FBB reconnect attempts, in seconds. |
| `fbb_read_idle_timeout` | no | `600` | If no bytes are received on the FBB socket for this many seconds, treat the connection as dead and reconnect. Must be greater than the internal keepalive interval (~540s). |
| `alert_cooldown` | no | `300` | Default minimum seconds between duplicate alerts for the same `(call, alert-type)` pair. `0` disables. Overridable at runtime via `alert cooldown`. |

#### `discord:` block

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `token` | yes | — | Discord bot token from the developer portal. |
| `sysop_user_id` | one of these | — | Discord user ID (integer) of the single authorised sysop. Takes precedence over the registration password if both are set. |
| `register_sysop_user_id_bot_password` | one of these | — | If set, DMing the bot `register <this-password>` from any user will record that user as the sysop in the YAML file. Remove after first use. |
