# KILLU X HOST

Your personal hosting platform for Python and Node.js bots and projects.
Upload code, run it, watch it, edit it, back it up. All from one panel.

## Run it
```
pip install -r requirements.txt
python server.py            # or:  sh start.sh   (auto-restarts the server, keeps Termux awake)
```
Open http://localhost:8000. Node.js projects need Node (Termux: `pkg install nodejs`).
GitHub import needs git (Termux: `pkg install git`).

## Pages
- index.html: the website (shows your real projects and server stats)
- panel.html: dashboard (all projects, server health, alerts, backup of everything)
- bot.html:   one project (tap a project to open it)

## Inside a project
- Overview: Start / Restart / Stop, live CPU, memory, uptime, console, recent activity
- Terminal: live output, remembers `cd`, Stop button
- Files: editor with line numbers, upload files or a .zip (can extract), drag and drop, rename, delete
- Manage: packages (add / remove / install all) and a live usage chart
- Backups: create, restore, download, delete (latest 20 kept)
- Settings: name, description, main file (e.g. loke.py) or full command, auto-restart,
  scheduled restart every N hours, environment variables (tokens), delete

## Alerts
Panel > Alerts & backup. Add a Discord webhook link and/or a Telegram bot token + chat ID,
then press "Send test alert". You get a message when a project crashes or keeps failing.

## Password (strongly recommended if reachable from the internet)
```
PANEL_USER=killu PANEL_PASS=something-long python server.py
```
Without it, anyone who can reach the port can run code on your server.
`HOST=0.0.0.0` lets other devices on your network connect.

## Your data
Everything lives in `data/` (projects, logs, backups, settings). Back that folder up.
Tokens are stored there as plain text, so don't share the folder.

## Hosting 24/7
A phone can be killed by Android. For always-on hosting use a small VPS with the same commands.
render.yaml is included for Render (paid web service + disk, because free services sleep and lose files).
