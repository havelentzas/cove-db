// The cove sync engine: platform-neutral (no Obsidian imports), so it is unit/integration tested
// against a real cove server with in-memory vaults (see test/).
//
// Protocol: docs/notes-sync.md in the cove-db repo. In short:
//  - every local path has a ledger entry {id, version, hash} = what we last agreed with the server;
//  - local changes become ops (dirty / rename / delete), flushed in order;
//  - remote changes are pulled from the change feed (seq) and applied only to files that are
//    unmodified locally; modified ones are pushed instead and the server merges;
//  - before we write a file ourselves we update its ledger entry, so the vault event our own write
//    triggers is recognised as an echo and not pushed back.
import { AuthError, CoveApi, NetworkError } from "./api";
import type { FileInfo, LedgerEntry, Op, RemoteFile, SyncState, VaultAdapter, WriteResult } from "./types";

export const MAX_TEXT = 5 * 1024 * 1024;
export const MAX_BLOB = 200 * 1024 * 1024;

export type Status = "idle" | "syncing" | "offline" | "unauthorized" | "error" | "not-configured";

export interface EngineOptions {
  deviceName: string;
  syncAttachments: boolean;
  excludedFolders: string[];
  save: () => void;                       // persist state (debounced by the host)
  notify?: (msg: string) => void;          // user-visible message (Obsidian Notice)
  log?: (msg: string) => void;
  onStatus?: (s: Status, detail?: string) => void;
  uuid?: () => string;
}

export type Kind = "note" | "canvas" | "attachment";
export const kindOf = (path: string): Kind =>
  /\.md$/i.test(path) ? "note" : /\.canvas$/i.test(path) ? "canvas" : "attachment";
const isText = (path: string) => kindOf(path) !== "attachment";
const nfc = (s: string) => s.normalize("NFC");
const lower = (s: string) => nfc(s).toLowerCase();

export async function sha256(data: string | ArrayBuffer): Promise<string> {
  const bytes = typeof data === "string" ? new TextEncoder().encode(data) : new Uint8Array(data);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
}

interface Local { hash: string; text?: string; data?: ArrayBuffer; size: number }

export class SyncEngine {
  status: Status = "idle";
  lastSyncAt: number | null = null;
  lastError: string | null = null;
  private running: Promise<void> | null = null;
  private again = false;
  /** Vault events we caused ourselves (renames/trash), so they aren't mistaken for user actions. */
  private expected = new Set<string>();

  constructor(private vault: VaultAdapter, private api: CoveApi, public state: SyncState, private opts: EngineOptions) {}

  // ------------------------------------------------------------------ public surface
  get pending(): number { return this.state.ops.length; }

  excluded(path: string): boolean {
    const p = nfc(path);
    if (p.startsWith(".") || p.includes("/.")) return true;               // hidden files/folders (.obsidian, .trash, .git…)
    if (!this.opts.syncAttachments && kindOf(p) === "attachment") return true;
    return this.opts.excludedFolders.some(f => {
      const folder = f.replace(/^\/+|\/+$/g, "");
      return folder && (p === folder || p.startsWith(folder + "/"));
    });
  }

  onChange(path: string): void { this.enqueue({ type: "dirty", path: nfc(path) }); }

  onRename(from: string, to: string, isFolder: boolean): void {
    from = nfc(from); to = nfc(to);
    if (this.expected.delete(`rename\0${from}\0${to}`)) return;   // our own move
    if (isFolder) {
      for (const p of Object.keys(this.state.files)) {
        if (p.startsWith(from + "/")) this.enqueue({ type: "rename", from: p, to: to + p.slice(from.length) });
      }
    } else {
      this.enqueue({ type: "rename", from, to });
    }
  }

  onDelete(path: string, isFolder: boolean): void {
    path = nfc(path);
    if (this.expected.delete(`delete\0${path}`)) return;            // our own trash
    if (isFolder) {
      for (const p of Object.keys(this.state.files)) if (p.startsWith(path + "/")) this.enqueue({ type: "delete", path: p });
    } else {
      this.enqueue({ type: "delete", path });
    }
  }

  /** Run one sync cycle (coalesces concurrent calls). `fullScan` re-checks every file against the ledger. */
  sync(fullScan = false, confirmDeletes = false): Promise<void> {
    if (this.running) { this.again = true; return this.running; }
    this.running = (async () => {
      try {
        do {
          this.again = false;
          await this.cycle(fullScan, confirmDeletes);
          fullScan = false;
        } while (this.again);
      } finally {
        this.running = null;
      }
    })();
    return this.running;
  }

  // ------------------------------------------------------------------ cycle
  private async cycle(fullScan: boolean, confirmDeletes: boolean): Promise<void> {
    this.setStatus("syncing");
    try {
      if (!this.state.initialized) await this.firstSync();
      else if (fullScan) await this.scan(confirmDeletes);
      await this.flushOps();
      await this.pull();
      await this.flushOps();   // pulling may have queued pushes (locally-modified files)
      this.lastSyncAt = Date.now();
      this.lastError = null;
      this.setStatus("idle");
    } catch (e) {
      this.lastError = (e as Error).message ?? String(e);
      if (e instanceof AuthError) this.setStatus("unauthorized", this.lastError);
      else if (e instanceof NetworkError) this.setStatus("offline", this.lastError);
      else { this.setStatus("error", this.lastError); this.log(`error: ${this.lastError}`); }
    } finally {
      this.opts.save();
    }
  }

  private setStatus(s: Status, detail?: string) { this.status = s; this.opts.onStatus?.(s, detail); }
  private log(m: string) { this.opts.log?.(m); }
  private notify(m: string) { this.opts.notify?.(m); this.log(m); }
  private uuid() { return this.opts.uuid ? this.opts.uuid() : crypto.randomUUID(); }

  private enqueue(op: Op) {
    const ops = this.state.ops;
    if (op.type === "dirty" && ops.some(o => o.type === "dirty" && o.path === op.path)) return;
    ops.push(op);
    this.opts.save();
  }

  // ------------------------------------------------------------------ first sync
  private async firstSync(): Promise<void> {
    this.log("first sync: comparing this vault with cove");
    const manifest = await this.api.manifest();
    const remote = new Map(manifest.files.map(f => [lower(f.path), f]));
    for (const f of await this.vault.list()) {
      if (this.excluded(f.path)) continue;
      const r = remote.get(lower(f.path));
      if (!r) { this.enqueue({ type: "dirty", path: f.path }); continue; }
      remote.delete(lower(f.path));
      const local = await this.readLocal(f.path);
      if (local.hash === r.content_hash) {
        await this.adopt(f.path, r, local.hash);       // also adopts the server's capitalisation of the path
      } else {
        // Both sides have different content under the same name. Never guess which wins: push ours
        // without a base, so the server keeps its copy and saves ours as a "(conflict …)" copy.
        const res = await this.pushContent(r.id, f.path, local, null);
        await this.handleWrite(f.path, r.id, local, res);
      }
    }
    for (const r of remote.values()) {
      if (!this.excluded(r.path)) await this.download(r);
    }
    this.state.lastSeq = manifest.head_seq;
    this.state.initialized = true;
    this.opts.save();
    this.log(`first sync done (${manifest.files.length} files on cove)`);
  }

  // ------------------------------------------------------------------ scan (startup / manual)
  private async scan(confirmDeletes: boolean): Promise<void> {
    const files = await this.vault.list();
    const seen = new Set<string>();
    for (const f of files) {
      const p = nfc(f.path);
      if (this.excluded(p)) continue;
      seen.add(p);
      const e = this.state.files[p];
      if (!e || e.mtime !== f.mtime || e.size !== f.size) this.enqueue({ type: "dirty", path: p });
    }
    const missing = Object.keys(this.state.files).filter(p => !seen.has(p) && !this.excluded(p));
    const total = Object.keys(this.state.files).length;
    // Safety: if a big share of the vault "disappeared" (unmounted folder, vault not loaded yet…),
    // don't delete it from cove until the user confirms.
    if (!confirmDeletes && missing.length > 10 && missing.length > total * 0.25) {
      this.notify(`cove sync: ${missing.length} synced files are missing from this vault. Not deleting them on ` +
        `cove. If you really deleted them, run "cove sync: Push local deletions".`);
      return;
    }
    for (const p of missing) this.enqueue({ type: "delete", path: p });
  }

  // ------------------------------------------------------------------ outgoing
  private async flushOps(): Promise<void> {
    while (this.state.ops.length) {
      const op = this.state.ops[0];
      if (op.type === "dirty") await this.push(op.path);
      else if (op.type === "rename") await this.pushRename(op.from, op.to);
      else await this.pushDelete(op.path);
      this.state.ops.shift();       // only after success: a network error leaves it queued
      this.opts.save();
    }
  }

  private async readLocal(path: string): Promise<Local> {
    if (isText(path)) {
      const text = await this.vault.readText(path);
      return { text, hash: await sha256(text), size: new TextEncoder().encode(text).length };
    }
    const data = await this.vault.readBinary(path);
    return { data, hash: await sha256(data), size: data.byteLength };
  }

  private async pushContent(id: string, path: string, local: Local, base: number | null): Promise<WriteResult> {
    if (local.text !== undefined) return this.api.putText(id, path, local.text, base);
    await this.api.ensureBlob(local.hash, local.data!);
    return this.api.putAttachment(id, path, local.hash, base);
  }

  private async push(path: string): Promise<void> {
    if (this.excluded(path)) return;
    const st = await this.vault.stat(path);
    if (!st) return;                                    // gone again; a delete op follows
    const e = this.state.files[path];
    if (e && e.mtime === st.mtime && e.size === st.size) return;
    const local = await this.readLocal(path);
    if (e && e.hash === local.hash) { e.mtime = st.mtime; e.size = st.size; return; }   // echo of our own write
    const limit = local.text !== undefined ? MAX_TEXT : MAX_BLOB;
    if (local.size > limit) { this.notify(`cove sync: skipped ${path} (too large to sync)`); return; }
    const id = e?.id ?? this.uuid();
    const res = await this.pushContent(id, path, local, e ? e.version : null);
    await this.handleWrite(path, id, local, res);
  }

  /** Apply the server's answer to one of our content writes. */
  private async handleWrite(path: string, id: string, local: Local, res: WriteResult): Promise<void> {
    const f = res.file;
    switch (res.status) {
      case "created": case "updated": case "unchanged": case "restored":
        await this.adopt(path, f!, local.hash);
        return;
      case "merged":
        // Our edit and a newer one were combined on the server: take the merged text.
        await this.adopt(path, f!, f!.content_hash);
        await this.writeLocal(f!.path, res.content ?? "", f!);
        this.log(`merged ${f!.path}`);
        return;
      case "conflict":
        // Overlapping edits. The server kept its version and saved ours as a conflict copy
        // (which arrives through the change feed). Make our file match the server.
        this.notify(`cove sync: conflicting edits in "${f!.path}". Your version was kept as ` +
          `"${res.conflict_copy?.path ?? "a conflict copy"}".`);
        await this.download(await this.api.getFile(f!.id), path);
        return;
      case "path_taken": {
        const ex = res.existing!;
        if (ex.content_hash === local.hash) {           // same file created on two devices: adopt it
          await this.adopt(path, ex, local.hash);
          return;
        }
        // A different file already has this name on cove: keep ours under a new name, then fetch theirs.
        const alt = await this.freeName(path);
        delete this.state.files[path];
        await this.localRename(path, alt);
        this.notify(`cove sync: "${path}" already exists on cove; your file was renamed to "${alt}".`);
        await this.push(alt);
        await this.download(await this.api.getFile(ex.id));
        return;
      }
      case "not_found":
        delete this.state.files[path];
        return;
      default:
        this.notify(`cove sync: couldn't sync ${path}: ${res.detail ?? res.status}`);
        // remember the failing content so we don't retry it every cycle
        this.state.files[path] = { id, version: this.state.files[path]?.version ?? 0, hash: local.hash, ...(await this.statOf(path)) };
    }
  }

  private async pushRename(from: string, to: string): Promise<void> {
    const e = this.state.files[from];
    if (!e) { this.enqueue({ type: "dirty", path: to }); return; }     // never synced (or already moved)
    if (this.excluded(to)) { await this.pushDelete(from); return; }
    if (this.excluded(from)) { delete this.state.files[from]; this.enqueue({ type: "dirty", path: to }); return; }
    const res = await this.api.rename(e.id, to, e.version);
    if (res.status === "updated" || res.status === "unchanged") {
      delete this.state.files[from];
      this.state.files[to] = { ...e, version: res.file!.version };
    } else if (res.status === "conflict") {
      // renamed differently elsewhere: the server's path wins
      delete this.state.files[from];
      this.state.files[to] = { ...e, version: res.file!.version };
      if (res.file!.path !== to) await this.moveLocal(to, res.file!.path);
      this.notify(`cove sync: "${to}" was also renamed on another device; using "${res.file!.path}".`);
      return;
    } else if (res.status === "not_found") {
      delete this.state.files[from];
    } else if (res.status === "path_taken") {
      const alt = await this.freeName(to);
      await this.localRename(to, alt);
      this.enqueue({ type: "rename", from, to: alt });
      return;
    }
    this.enqueue({ type: "dirty", path: to });           // content may have changed too
  }

  private async pushDelete(path: string): Promise<void> {
    const e = this.state.files[path];
    if (!e) return;
    if (await this.vault.exists(path)) return;           // it came back (undo / recreated): nothing to delete
    const res = await this.api.remove(e.id, e.version);
    delete this.state.files[path];
    if (res.status === "conflict" && res.file) {
      // edited on another device since we last saw it: an edit beats a delete, so bring it back
      this.notify(`cove sync: "${res.file.path}" was edited elsewhere, so it was restored instead of deleted.`);
      await this.download(await this.api.getFile(res.file.id));
    }
  }

  // ------------------------------------------------------------------ incoming
  private async pull(): Promise<void> {
    for (;;) {
      const page = await this.api.changes(this.state.lastSeq);
      for (const c of page.changes) {
        await this.applyRemote(c);
        this.state.lastSeq = c.seq ?? this.state.lastSeq;
      }
      this.state.lastSeq = Math.max(this.state.lastSeq, page.last_seq);
      this.opts.save();
      if (!page.more) return;
    }
  }

  private findById(id: string): [string, LedgerEntry] | undefined {
    for (const [p, e] of Object.entries(this.state.files)) if (e.id === id) return [p, e];
    return undefined;
  }

  private async modifiedLocally(path: string, e: LedgerEntry): Promise<boolean> {
    const st = await this.vault.stat(path);
    if (!st) return false;
    if (st.mtime === e.mtime && st.size === e.size) return false;
    const h = (await this.readLocal(path)).hash;
    if (h === e.hash) { e.mtime = st.mtime; e.size = st.size; return false; }
    return true;
  }

  private async applyRemote(c: RemoteFile): Promise<void> {
    const mine = this.findById(c.id);
    if (c.deleted) {
      if (!mine) return;
      const [p, e] = mine;
      if (e.version >= c.version) return;
      if (await this.modifiedLocally(p, e)) {
        e.version = c.version;                        // our pending edit will restore it (edit beats delete)
        this.enqueue({ type: "dirty", path: p });
        return;
      }
      delete this.state.files[p];
      if (await this.vault.exists(p)) await this.localTrash(p);
      this.log(`deleted ${p} (deleted on another device)`);
      return;
    }
    if (mine && mine[1].version >= c.version) return;   // our own write echoing back, or already applied
    if (this.excluded(c.path)) return;
    if (mine) {
      const [p, e] = mine;
      if (await this.modifiedLocally(p, e)) { this.enqueue({ type: "dirty", path: p }); return; }
      if (p !== c.path) await this.moveLocal(p, c.path);
      if (e.hash !== c.content_hash) await this.download(c);
      else this.state.files[c.path] = { ...this.state.files[c.path], version: c.version };
      return;
    }
    await this.download(c);
  }

  /** Fetch a file from cove and write it locally at its server path (or `localPath`). */
  private async download(r: RemoteFile, localPath?: string): Promise<void> {
    if (r.deleted) return;
    const target = r.path;
    if (localPath && localPath !== target && (await this.vault.exists(localPath))) {
      delete this.state.files[localPath];
      await this.localRename(localPath, target);
    }
    if (!this.findById(r.id) && (await this.vault.exists(target))) {
      // an untracked local file has the same name
      const local = await this.readLocal(target);
      if (local.hash === r.content_hash) { await this.adopt(target, r, local.hash); return; }
      const alt = await this.freeName(target);
      await this.localRename(target, alt);
      this.enqueue({ type: "dirty", path: alt });
      this.notify(`cove sync: kept your local "${target}" as "${alt}" (cove has a different file with that name).`);
    }
    if (kindOf(target) === "attachment") {
      const full = r.blob_sha256 ? r : await this.api.getFile(r.id);
      const data = await this.api.getBlob(full.blob_sha256!);
      await this.writeLocal(target, data, full);
    } else {
      const full = r.content !== undefined ? r : await this.api.getFile(r.id);
      await this.writeLocal(target, full.content ?? "", full);
    }
  }

  /** Write content we got from cove. The ledger is updated FIRST so the resulting vault event is an echo. */
  private async writeLocal(path: string, content: string | ArrayBuffer, f: RemoteFile): Promise<void> {
    for (const [p, e] of Object.entries(this.state.files)) if (e.id === f.id && p !== path) delete this.state.files[p];
    this.state.files[path] = { id: f.id, version: f.version, hash: f.content_hash, mtime: -1, size: -1 };
    if (typeof content === "string") await this.vault.writeText(path, content);
    else await this.vault.writeBinary(path, content);
    Object.assign(this.state.files[path], await this.statOf(path));
  }

  private async adopt(path: string, f: RemoteFile, hash: string): Promise<void> {
    for (const [p, e] of Object.entries(this.state.files)) if (e.id === f.id && p !== path) delete this.state.files[p];
    this.state.files[path] = { id: f.id, version: f.version, hash, ...(await this.statOf(path)) };
    if (f.path !== path) await this.moveLocal(path, f.path);
  }

  private async moveLocal(from: string, to: string): Promise<void> {
    if (from === to) return;
    if (lower(from) !== lower(to) && (await this.vault.exists(to)) && !this.state.files[to]) {
      const alt = await this.freeName(to);
      await this.localRename(to, alt);
      this.enqueue({ type: "dirty", path: alt });
    }
    const e = this.state.files[from];
    delete this.state.files[from];
    if (e) this.state.files[to] = e;
    await this.localRename(from, to);
    if (e) Object.assign(this.state.files[to], await this.statOf(to));
  }

  private async localRename(from: string, to: string): Promise<void> {
    const key = `rename\0${nfc(from)}\0${nfc(to)}`;
    this.expected.add(key);
    try { await this.vault.rename(from, to); } catch (e) { this.expected.delete(key); throw e; }
    setTimeout(() => this.expected.delete(key), 60_000);      // never let a missed event linger
  }

  private async localTrash(path: string): Promise<void> {
    const key = `delete\0${nfc(path)}`;
    this.expected.add(key);
    try { await this.vault.trash(path); } catch (e) { this.expected.delete(key); throw e; }
    setTimeout(() => this.expected.delete(key), 60_000);
  }

  private async statOf(path: string): Promise<{ mtime: number; size: number }> {
    const st: FileInfo | null = await this.vault.stat(path);
    return { mtime: st?.mtime ?? -1, size: st?.size ?? -1 };
  }

  private async freeName(path: string): Promise<string> {
    const dot = path.lastIndexOf(".");
    const slash = path.lastIndexOf("/");
    const [stem, ext] = dot > slash ? [path.slice(0, dot), path.slice(dot)] : [path, ""];
    const day = new Date().toISOString().slice(0, 10);
    const who = this.opts.deviceName.replace(/[^\w-]+/g, "-") || "this device";
    for (let n = 1; n < 100; n++) {
      const cand = `${stem} (conflict ${day} ${who}${n > 1 ? " " + n : ""})${ext}`;
      if (!(await this.vault.exists(cand))) return cand;
    }
    throw new Error(`no free name for ${path}`);
  }
}
