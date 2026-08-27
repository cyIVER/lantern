# Keys and secrets

Every credential LANtern uses, where it lives, and how to create it.

**This repository is public.** Nothing on this page is stored in git. Each secret
lives in exactly one of three places:

| Store | Used by | Why there |
|---|---|---|
| Gitignored `.env` file on the VM | Docker Compose, the control UI | Read at container start |
| GitHub Actions secret | CI workflows | Encrypted at rest, not exposed to fork pull requests |
| Windows Credential Manager | Router MCP server | DPAPI-encrypted; no plaintext on disk at all |

The `.env` files live under `/opt/lantern` on the `lantern` VM. The router MCP
server is the exception that still runs on Windows, which is why its password
lives in a Windows store.

> **Correction, and it matters: the `.env` files now leave the VM every night.**
> The nightly backup copies them to `D:\LANtern-Backups\data\<timestamp>\config.tgz`
> on the Windows host, because they are gitignored and therefore exist nowhere
> else — a backup that omits them restores a stack that cannot start.
>
> On the VM that archive is written mode 600. On D: it is an ordinary file with
> ordinary permissions. **Treat `D:\LANtern-Backups` as a secret store**: it holds
> every database password, the RCON password, the Pelican API key, the CurseForge
> key and the Steam refresh token, in fourteen dated copies. Do not sync that
> folder anywhere, and if you ever hand someone a backup set, hand them one with
> `config.tgz` removed.
>
> Rotating a secret does not rewrite the old sets. That is the point of them, and
> also the reason a leak is not cleaned up by rotation alone — see
> [If a secret leaks](#if-a-secret-leaks). Full detail on the backups is in
> [../vm/README.md](../vm/README.md).

A quick way to prove nothing leaked before you push:

```bash
git grep -nIE '(password|secret|api_key|token|webhook)' -- . | grep -viE 'example|change-me|environ|getenv|<'
```

---

## 1. CurseForge API key

Needed to download All the Mods 10 server files during install, and by CI to
detect new pack releases.

**Get one** — free, instant:

1. Sign in at <https://console.curseforge.com/>
2. **API Keys** in the sidebar
3. Copy the key (a long `$2a$10$...` string)

CurseForge's terms forbid redistributing the key, so it never enters git.

**Put it in two places.** On the VM, in `/opt/lantern/stack/.env`:

```bash
CURSEFORGE_API_KEY=$2a$10$your-key-here
```

And as a GitHub Actions secret:

```bash
gh secret set CURSEFORGE_API_KEY
```

The command prompts for the value and never echoes it. Verify with `gh secret list`.

> The Minecraft egg reads this as a Pelican server variable, not from the egg
> JSON. The egg definition is committed; the key is not.

**Two things to know about how this key behaves.**

Pelican writes every environment variable, including this one, in plaintext into
the install log:

```
/var/log/pelican/install/<server-uuid>.log
```

That file stays on the host and is not in git, but it does mean the key is
readable by anything that can read the Docker volumes. Rotate it if you ever
share a log while debugging.

Second: CurseForge keys are bcrypt-shaped and start `$2a$10$...`. Docker Compose
parses `stack/.env` for its own interpolation and treats each `$` as a variable
reference, so it prints warnings like:

```
warning: The "UhALWueg8PnCnsFIMXfxueGhBA" variable is not set.
```

Harmless — LANtern's scripts read the key with `grep` directly rather than
through Compose, so the real value is passed through intact. Do not "fix" it by
escaping the dollars as `$$`, which would corrupt what those scripts read.

---

## 2. Discord webhook URL

Posts a message when a new modpack version lands so everyone knows to update.

A webhook, not a bot — no OAuth, no gateway connection, no process to keep
running. You only need a bot if you want to type commands *into* Discord.

**Create it:**

1. In Discord, right-click the target channel → **Edit Channel**
2. **Integrations** → **Webhooks** → **New Webhook**
3. Name it (`LANtern`), pick the channel, **Copy Webhook URL**

**Treat the URL as a password.** Anyone holding it can post to that channel as
that webhook. It goes in GitHub secrets only:

```bash
gh secret set DISCORD_WEBHOOK_URL
```

Test it without involving CI:

```bash
curl -X POST -H 'Content-Type: application/json' -d '{"content":"LANtern webhook works"}' "$DISCORD_WEBHOOK_URL"
```

> **Size limit.** Webhook attachments cap at 10–25 MB, or 100 MB on a
> Boost Level 3 server. The ATM10 pack is 192 MB, so the file itself is never
> attached — the message carries the version, the CurseForge file ID and a small
> instance manifest, and Prism does the download. See [MINECRAFT.md](MINECRAFT.md).

---

## 3. Steam credentials — Stardew Valley only

The Stardew dedicated server downloads the game you own, so it needs a Steam
login. There is no way around this; it is why the game files can be fetched at all.

`/opt/lantern/stardew/.env` on the VM (gitignored):

```bash
STEAM_USERNAME=your-steam-login
STEAM_PASSWORD=your-steam-password
VNC_PASSWORD=pick-something
```

Then authenticate once, interactively — this prompts for your Steam Guard code.
Run it from an ssh session on the VM, in `/opt/lantern/stardew`:

```bash
docker compose run --rm -it steam-auth setup
```

It needs a TTY, so `ssh lantern` and run it there; `ssh lantern '...'` as a
one-shot command will not give it one.

**Rules:**

- **Never** put Steam credentials in a GitHub secret. A self-hosted runner or a
  workflow log can leak them, and this repository is public.
- The session token is cached in the `steam-session` Docker volume, so the
  password is used once at setup and not on every start.
- Consider a Steam account that owns only Stardew Valley if you would rather not
  put your main account in a file.

---

## 4. Server credentials — generated, not chosen

These are created for you and written into gitignored files. Listed here so you
know what exists and how to rotate it.

| Secret | Lives in | Created by |
|---|---|---|
| `DB_ROOT_PASSWORD`, `DB_PASSWORD` | `stack/.env` | You, at first bring-up |
| `RCON_PASSWORD` | Pelican server variable → `ui/.env` | `bootstrap/rotate-rcon.php` |
| `PELICAN_API_KEY` | `ui/.env` | `bootstrap/create-ui-credentials.php` |
| WeaponPaints DB user | `ui/.env` | `bootstrap/setup-weaponpaints-db.sh` |
| `STARDEW_API_URL`, `STARDEW_API_KEY` | `ui/.env` | copied from `stardew/.env` so the control UI can reach the farm |
| `VNC_PASSWORD` | `stardew/.env` | you, with `openssl rand -base64 24` — it guards a console that drives the live game |

All of the above are inside `config.tgz` in every nightly backup set on `D:`.

Rotate RCON any time — it regenerates the password, updates Pelican, rewrites
`ui/.env` and restarts what needs restarting:

```bash
docker compose exec -T panel php artisan tinker < bootstrap/rotate-rcon.php
```

RCON passwords must never be passed on the command line. CounterStrikeSharp
prints its own argv to the console, which leaks them into the log — that is why
`boot.sh` writes credentials into `lantern.cfg` instead. See
[DECISIONS.md](DECISIONS.md).

---

## 5. Router password

Not in `.env`. Not in the repo. The router MCP server stores it in **Windows
Credential Manager**, encrypted at rest with DPAPI under your user account. No
tool in that server returns it, logs it, or puts it in an error message.

```bash
python -m mcp.router.set_password
```

`LANTERN_ROUTER_PASSWORD` exists as an escape hatch for headless use, but
keyring is strongly preferred. See [ROUTER-MCP.md](ROUTER-MCP.md).

---

## Setting up a fresh clone

On the VM (`ssh lantern`):

```bash
git clone https://github.com/cyIVER/lantern.git /opt/lantern && cd /opt/lantern

cp stack/.env.example stack/.env
$EDITOR stack/.env          # DB passwords, APP_URL, CURSEFORGE_API_KEY

# Generates the UI's API key inside the panel container, then copies it out.
# It is a tinker script, not a shell script, so it is piped into artisan
# rather than executed.
cd stack
docker compose exec -T panel php artisan tinker < bootstrap/create-ui-credentials.php
docker compose cp panel:/tmp/lantern-ui.env ../ui/.env
cd ..

gh secret set CURSEFORGE_API_KEY
gh secret set DISCORD_WEBHOOK_URL
```

On **Windows**, separately — the router MCP server runs there and keeps its
password in Windows Credential Manager:

```powershell
python -m mcp.router.set_password                # optional
```

## If a secret leaks

1. **Rotate first, scrub second.** A key that is revoked is harmless wherever it
   has been copied to; a key that is only deleted from git is still valid.
   - CurseForge: delete the key in the console, generate a new one
   - Discord: **Delete Webhook** in channel settings, make a new one
   - Steam: change the password, then **Deauthorize all devices** in Steam Guard settings
   - RCON: `rotate-rcon.php`
2. Then remove it from history with `git filter-branch` or `git filter-repo`, and
   force-push. Assume anything that reached a public GitHub is already scraped —
   rotation is what actually protects you, not the rewrite.
3. **Remember the backups.** Up to fourteen dated `config.tgz` files under
   `D:\LANtern-Backups\data` still contain the old value, and they are on a disk
   that never gets scrubbed. They are not a leak by themselves — they are on your
   own machine — but they are why "I deleted the file" is not the same as "the
   secret is gone", and why step 1 is rotation.
