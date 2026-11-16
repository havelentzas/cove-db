# cove sync — Obsidian plugin

Two-way sync between an Obsidian vault and **cove Notes** (the `notes` service in this repo).
Works on desktop and mobile. It follows the protocol in [`docs/notes-sync.md`](../docs/notes-sync.md).

- **Live sync:** changes upload about 2 s after you stop typing, and other devices' changes arrive on
  startup, when you switch back to Obsidian, and every 30 s.
- **Nothing is overwritten:** edits to different parts of a note merge on the server. Edits to the same
  lines keep both versions, with yours saved as `Name (conflict YYYY-MM-DD device).md`.
- **Renames keep history:** renames and moves (whole folders too) keep each note's identity, history and
  backlinks. Deletes go to the trash on other devices. If one device deletes a note while another edits it,
  the edit wins.
- **Attachments are uploaded once,** identified by content hash. They can be turned off.
- **Offline-safe:** offline edits queue up and sync when cove is reachable again. If most of the vault
  suddenly goes missing (for example an unmounted folder), the plugin refuses to delete it from cove until you confirm.
- **Never synced:** hidden folders (`.obsidian`, `.trash`, `.git`), plus any folders you exclude.

## Install

**Desktop:**
```sh
sh obsidian-plugin/install.sh "/path/to/Your Vault"
```

**Any device, including phones:** in cove, open **Settings → Devices** (`/settings/devices`) and download
`main.js`, `manifest.json` and `styles.css`. Put them in `<vault>/.obsidian/plugins/cove-sync/`.
- **iPhone:** use the Files app with the vault under *On My iPhone → Obsidian*. You may need to show hidden
  folders, or copy the files over from a computer.

Then:
1. In Obsidian, go to **Settings → Community plugins**, turn on **cove sync**, and open its settings.
2. In cove, go to **Settings → Devices** and create a token named after this device (for example `iphone`).
3. In the plugin settings, paste the server address (e.g. `https://<machine>.<tailnet>.ts.net`) and the
   token, then press **Test**. The first sync starts automatically.

The phone needs Tailscale switched on to reach cove.

**If the vault also syncs another way** (Obsidian Sync, iCloud, git), it's best to let cove be the only sync.
Two sync systems moving the same files can confuse each other.

## Commands
- **cove sync: Sync now** (also the ribbon icon). Re-checks every file and syncs.
- **cove sync: Push local deletions.** Confirms deletions that the mass-delete guard held back.
- **cove sync: Show recent activity.**
- **Settings → Reset sync state.** Compares the whole vault with cove again. Nothing is deleted; differing
  files become conflict copies.

## Develop
Everything builds in Docker, so Node isn't needed on the host:
```sh
cd obsidian-plugin
docker run --rm -u $(id -u):$(id -g) -e HOME=/tmp -v "$PWD":/work -w /work node:22-alpine@sha256:1314fb30ec37592d5845e5a160d522c0e680972aa534549940b8d73bde88481a \
  sh -c 'npm ci && npm run build'        # → main.js (committed, so cove can serve it)
sh test/run-integration.sh               # from the repo root: 9 two-device scenarios against a throwaway cove server
```

| File | What it is |
|---|---|
| `src/engine.ts` | The sync engine. No Obsidian imports, so it's fully testable. |
| `src/obsidian-adapter.ts` | Maps the engine onto Obsidian's vault API and `requestUrl`. |
| `src/main.ts` | The plugin: settings, status bar, ribbon icon, commands, vault events. |
| `src/api.ts` | Client for `/api/notes/*`. |
| `test/` | Integration tests: in-memory vaults acting as two devices, run against a real cove server on the test database. |
