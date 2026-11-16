// VaultAdapter + Http on top of the Obsidian API (works on desktop and mobile).
import { App, normalizePath, requestUrl, TFile, TFolder } from "obsidian";
import type { FileInfo, Http, VaultAdapter } from "./types";

export class ObsidianVault implements VaultAdapter {
  constructor(private app: App) {}

  private file(path: string): TFile | null {
    const f = this.app.vault.getAbstractFileByPath(normalizePath(path));
    return f instanceof TFile ? f : null;
  }

  private async ensureParent(path: string): Promise<void> {
    const parts = normalizePath(path).split("/").slice(0, -1);
    for (let i = 1; i <= parts.length; i++) {
      const dir = parts.slice(0, i).join("/");
      const existing = this.app.vault.getAbstractFileByPath(dir);
      if (existing instanceof TFolder) continue;
      if (existing) throw new Error(`"${dir}" is a file, can't create a folder there`);
      try { await this.app.vault.createFolder(dir); } catch (e) { if (!this.app.vault.getAbstractFileByPath(dir)) throw e; }
    }
  }

  async list(): Promise<FileInfo[]> {
    return this.app.vault.getFiles().map(f => ({ path: f.path, mtime: f.stat.mtime, size: f.stat.size }));
  }

  async stat(path: string): Promise<FileInfo | null> {
    const f = this.file(path);
    return f ? { path: f.path, mtime: f.stat.mtime, size: f.stat.size } : null;
  }

  async readText(path: string): Promise<string> {
    const f = this.file(path);
    if (!f) throw new Error(`missing ${path}`);
    return this.app.vault.read(f);
  }

  async readBinary(path: string): Promise<ArrayBuffer> {
    const f = this.file(path);
    if (!f) throw new Error(`missing ${path}`);
    return this.app.vault.readBinary(f);
  }

  async writeText(path: string, content: string): Promise<void> {
    const f = this.file(path);
    if (f) { await this.app.vault.modify(f, content); return; }
    await this.ensureParent(path);
    await this.app.vault.create(normalizePath(path), content);
  }

  async writeBinary(path: string, data: ArrayBuffer): Promise<void> {
    const f = this.file(path);
    if (f) { await this.app.vault.modifyBinary(f, data); return; }
    await this.ensureParent(path);
    await this.app.vault.createBinary(normalizePath(path), data);
  }

  async rename(from: string, to: string): Promise<void> {
    const f = this.file(from);
    if (!f) return;
    await this.ensureParent(to);
    // vault.rename (not fileManager.renameFile): links in other notes are NOT rewritten here,
    // because the device that did the rename already synced those link edits.
    await this.app.vault.rename(f, normalizePath(to));
  }

  async trash(path: string): Promise<void> {
    const f = this.file(path);
    if (!f) return;
    const fm = this.app.fileManager as unknown as { trashFile?: (f: TFile) => Promise<void> };
    if (fm.trashFile) await fm.trashFile(f);          // respects the user's "deleted files" setting
    else await this.app.vault.trash(f, true);
  }

  async exists(path: string): Promise<boolean> {
    return this.app.vault.getAbstractFileByPath(normalizePath(path)) !== null;
  }
}

/** requestUrl bypasses CORS and works on mobile. Never throws on HTTP errors. */
export const obsidianHttp: Http = async req => {
  const res = await requestUrl({
    url: req.url, method: req.method, headers: req.headers, body: req.body, contentType: req.contentType, throw: false,
  });
  let json: unknown = null;
  try { json = res.json; } catch { json = null; }
  let arrayBuffer = new ArrayBuffer(0);
  try { arrayBuffer = res.arrayBuffer; } catch { /* empty body */ }
  return { status: res.status, json, arrayBuffer };
};
