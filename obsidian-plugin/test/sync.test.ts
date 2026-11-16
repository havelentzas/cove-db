// Integration tests: two simulated devices sync through a real (throwaway) cove server.
// Run with test/run-integration.sh, which starts the server and provides COVE_URL + tokens.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { CoveApi } from "../src/api";
import { SyncEngine } from "../src/engine";
import { emptyState } from "../src/types";
import { fetchHttp, MemoryVault } from "./memory-vault";

const URL = process.env.COVE_URL ?? "http://127.0.0.1:18080";
const TOKENS: Record<string, string> = JSON.parse(readFileSync(process.env.COVE_TOKENS ?? "/tmp/cove-tokens.json", "utf8"));

let n = 0;
function device(name: "laptop" | "phone", opts: { syncAttachments?: boolean } = {}) {
  const vault = new MemoryVault();
  const offline = { on: false };
  const notes: string[] = [];
  const engine = new SyncEngine(vault, new CoveApi(fetchHttp(offline), URL, TOKENS[name]), emptyState(), {
    deviceName: name, syncAttachments: opts.syncAttachments ?? true, excludedFolders: ["Private"],
    save: () => {}, notify: m => notes.push(m),
  });
  // Emulate Obsidian: EVERY write/rename/trash fires a vault event, including the engine's own
  // (those must be recognised as echoes and not pushed back).
  const put = vault.put.bind(vault), ren = vault.rename.bind(vault), tr = vault.trash.bind(vault);
  vault.put = (p, c) => { put(p, c); if (engine.state.initialized) engine.onChange(p); };
  vault.rename = async (from, to) => { await ren(from, to); engine.onRename(from, to, false); };
  vault.trash = async p => { await tr(p); engine.onDelete(p, false); };
  return {
    vault, engine, offline, notes,
    edit(p: string, c: string | Uint8Array) { vault.put(p, c); },
    async rename(from: string, to: string) { await vault.rename(from, to); },
    async remove(p: string) { vault.files.delete(p); engine.onDelete(p, false); },
    sync: (full = false) => engine.sync(full),
  };
}
type Dev = ReturnType<typeof device>;

async function converge(...devs: Dev[]) {
  for (let i = 0; i < 2; i++) for (const d of devs) await d.sync();
  const [first, ...rest] = devs.map(d => d.vault.snapshot());
  for (const s of rest) assert.deepEqual(s, first, "vaults did not converge");
  for (const d of devs) assert.equal(d.engine.status, "idle", d.engine.lastError ?? "");
  return first;
}

const folder = () => `T${++n}-${Date.now() % 100000}`;

test("first sync uploads a vault, a second device downloads it, and nothing echoes", async () => {
  const F = folder();
  const A = device("laptop");
  A.vault.put(`${F}/Kitchen.md`, "# Kitchen\nSee [[Budget]]\n");
  A.vault.put(`${F}/Budget.md`, "# Budget\n");
  A.vault.put(`${F}/img/sketch.png`, new Uint8Array([137, 80, 78, 71, 1, 2, 3]));
  A.vault.put(`Private/secret.md`, "never synced");
  A.vault.put(`.obsidian/app.json`, "{}");
  await A.sync();
  const B = device("phone");
  await B.sync();
  assert.equal(B.vault.text(`${F}/Kitchen.md`), "# Kitchen\nSee [[Budget]]\n");
  assert.deepEqual([...B.vault.files.get(`${F}/img/sketch.png`)!.data], [137, 80, 78, 71, 1, 2, 3]);
  assert.equal(B.vault.text("Private/secret.md"), undefined);
  assert.equal(B.vault.text(".obsidian/app.json"), undefined);
  const seq = A.engine.state.lastSeq;
  await A.sync(true); await B.sync(true);
  const after = await new CoveApi(fetchHttp(), URL, TOKENS.laptop).changes(seq);
  assert.equal(after.changes.length, 0, "a no-op sync must not write anything");
});

test("non-overlapping edits on two devices are merged", async () => {
  const F = folder();
  const A = device("laptop"); const B = device("phone");
  A.vault.put(`${F}/Plan.md`, "line 1\nline 2\nline 3\n");
  await A.sync(); await B.sync();
  A.edit(`${F}/Plan.md`, "LINE 1\nline 2\nline 3\n");
  B.edit(`${F}/Plan.md`, "line 1\nline 2\nline 3\nline 4 from phone\n");
  const snap = await converge(A, B);
  assert.equal(snap[`${F}/Plan.md`], "LINE 1\nline 2\nline 3\nline 4 from phone\n");
});

test("overlapping edits keep both versions as a conflict copy on every device", async () => {
  const F = folder();
  const A = device("laptop"); const B = device("phone");
  A.vault.put(`${F}/Note.md`, "colour: blue\n");
  await A.sync(); await B.sync();
  A.edit(`${F}/Note.md`, "colour: green\n");
  B.edit(`${F}/Note.md`, "colour: red\n");
  const snap = await converge(A, B);
  assert.equal(snap[`${F}/Note.md`], "colour: green\n");
  const copy = Object.keys(snap).find(p => p.startsWith(`${F}/Note (conflict `));
  assert.ok(copy, "conflict copy exists");
  assert.equal(snap[copy!], "colour: red\n");
  assert.ok(B.notes.some(m => m.includes("conflicting edits")));
});

test("renames and deletes propagate; edit beats delete", async () => {
  const F = folder();
  const A = device("laptop"); const B = device("phone");
  A.vault.put(`${F}/old.md`, "x\n");
  A.vault.put(`${F}/gone.md`, "bye\n");
  A.vault.put(`${F}/contested.md`, "v1\n");
  await A.sync(); await B.sync();
  await A.rename(`${F}/old.md`, `${F}/Sub/new.md`);
  await B.remove(`${F}/gone.md`);
  await B.remove(`${F}/contested.md`);
  A.edit(`${F}/contested.md`, "v2 edited on laptop\n");
  await B.sync();                       // phone deletes first
  const snap = await converge(A, B);    // laptop's edit then restores it everywhere
  assert.equal(snap[`${F}/Sub/new.md`], "x\n");
  assert.equal(snap[`${F}/old.md`], undefined);
  assert.equal(snap[`${F}/gone.md`], undefined);
  assert.equal(snap[`${F}/contested.md`], "v2 edited on laptop\n");
  assert.ok(A.vault.trashed.includes(`${F}/gone.md`), "remote deletes go to the trash, not hard-deleted");
  const idA = A.engine.state.files[`${F}/Sub/new.md`].id, idB = B.engine.state.files[`${F}/Sub/new.md`].id;
  assert.equal(idA, idB, "rename keeps the same id");
});

test("attachments update, and offline edits sync when the server is back", async () => {
  const F = folder();
  const A = device("laptop"); const B = device("phone");
  A.vault.put(`${F}/pic.png`, new Uint8Array([1, 2, 3]));
  await A.sync(); await B.sync();
  A.offline.on = true;
  A.edit(`${F}/pic.png`, new Uint8Array([4, 5, 6, 7]));
  A.edit(`${F}/Offline.md`, "written on a plane\n");
  await A.sync();
  assert.equal(A.engine.status, "offline");
  assert.ok(A.engine.pending >= 1);
  A.offline.on = false;
  await converge(A, B);
  assert.deepEqual([...B.vault.files.get(`${F}/pic.png`)!.data], [4, 5, 6, 7]);
  assert.equal(B.vault.text(`${F}/Offline.md`), "written on a plane\n");
});

test("same name created independently: identical content is adopted, different content is kept as a copy", async () => {
  const F = folder();
  const A = device("laptop"); const B = device("phone");
  await A.sync(); await B.sync();
  A.edit(`${F}/Same.md`, "same\n"); B.edit(`${F}/Same.md`, "same\n");
  A.edit(`${F}/Clash.md`, "laptop's\n"); B.edit(`${F}/Clash.md`, "phone's\n");
  const snap = await converge(A, B);
  assert.equal(snap[`${F}/Same.md`], "same\n");
  assert.equal(Object.keys(snap).filter(p => p.startsWith(`${F}/Same`)).length, 1);
  const clash = Object.keys(snap).filter(p => p.startsWith(`${F}/Clash`));
  assert.equal(clash.length, 2);
  assert.deepEqual(new Set(clash.map(p => snap[p])), new Set(["laptop's\n", "phone's\n"]));
});

test("a vault that suddenly lost most files does not mass-delete on cove", async () => {
  const F = folder();
  const A = device("laptop");
  for (let i = 0; i < 20; i++) A.vault.put(`${F}/n${i}.md`, `note ${i}\n`);
  await A.sync();
  for (let i = 0; i < 20; i++) A.vault.files.delete(`${F}/n${i}.md`);   // e.g. folder unmounted, no delete events
  await A.sync(true);
  assert.ok(A.notes.some(m => m.includes("missing from this vault")));
  const B = device("phone");
  await B.sync();
  assert.equal(B.vault.text(`${F}/n0.md`), "note 0\n", "files must still be on cove");
});

test("renaming a folder moves every file inside it", async () => {
  const F = folder();
  const A = device("laptop"); const B = device("phone");
  A.vault.put(`${F}/Old/a.md`, "a\n"); A.vault.put(`${F}/Old/deep/b.md`, "b\n"); A.vault.put(`${F}/Old/c.canvas`, '{"nodes":[],"edges":[]}');
  await A.sync(); await B.sync();
  for (const p of [...A.vault.files.keys()].filter(p => p.startsWith(`${F}/Old/`))) {
    const to = p.replace(`${F}/Old/`, `${F}/New/`);
    const f = A.vault.files.get(p)!; A.vault.files.delete(p); A.vault.files.set(to, f);   // move silently…
  }
  A.engine.onRename(`${F}/Old`, `${F}/New`, true);                                         // …one folder event
  const snap = await converge(A, B);
  assert.deepEqual(Object.keys(snap).filter(p => p.startsWith(`${F}/`)).sort(),
    [`${F}/New/a.md`, `${F}/New/c.canvas`, `${F}/New/deep/b.md`]);
});

test("a device joining with a stale copy of the vault never overwrites cove", async () => {
  const F = folder();
  const A = device("laptop");
  A.vault.put(`${F}/Shared.md`, "current version\n");
  A.vault.put(`${F}/Unchanged.md`, "same everywhere\n");
  await A.sync();
  const B = device("phone");                     // e.g. vault folder copied from an old backup
  B.vault.put(`${F}/Shared.md`, "old version\n");
  B.vault.put(`${F}/Unchanged.md`, "same everywhere\n");
  B.vault.put(`${F}/OnlyOnPhone.md`, "phone only\n");
  const snap = await converge(A, B);
  assert.equal(snap[`${F}/Shared.md`], "current version\n");
  assert.equal(snap[`${F}/OnlyOnPhone.md`], "phone only\n");
  const copy = Object.keys(snap).find(p => p.startsWith(`${F}/Shared (conflict `));
  assert.ok(copy && snap[copy] === "old version\n", "stale content preserved as a conflict copy");
  assert.equal(Object.keys(snap).filter(p => p.startsWith(`${F}/Unchanged`)).length, 1);
});
