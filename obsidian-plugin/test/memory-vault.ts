// In-memory VaultAdapter + fetch-based Http for integration tests.
import type { FileInfo, Http, VaultAdapter } from "../src/types";

let clock = 1_000;
const enc = new TextEncoder();
const dec = new TextDecoder();

export class MemoryVault implements VaultAdapter {
  files = new Map<string, { data: Uint8Array; mtime: number }>();
  trashed: string[] = [];

  // --- test helpers (simulate the user editing in Obsidian) ---
  put(path: string, content: string | Uint8Array) {
    this.files.set(path, { data: typeof content === "string" ? enc.encode(content) : content, mtime: ++clock });
  }
  text(path: string): string | undefined {
    const f = this.files.get(path);
    return f ? dec.decode(f.data) : undefined;
  }
  snapshot(): Record<string, string> {
    return Object.fromEntries([...this.files].sort().map(([p, f]) => [p, dec.decode(f.data)]));
  }

  // --- VaultAdapter ---
  async list(): Promise<FileInfo[]> {
    return [...this.files].map(([path, f]) => ({ path, mtime: f.mtime, size: f.data.byteLength }));
  }
  async stat(path: string) {
    const f = this.files.get(path);
    return f ? { path, mtime: f.mtime, size: f.data.byteLength } : null;
  }
  async readText(path: string) { return dec.decode(this.must(path).data); }
  async readBinary(path: string) { const d = this.must(path).data; return d.buffer.slice(d.byteOffset, d.byteOffset + d.byteLength) as ArrayBuffer; }
  async writeText(path: string, content: string) { this.put(path, content); }
  async writeBinary(path: string, data: ArrayBuffer) { this.put(path, new Uint8Array(data.slice(0))); }
  async rename(from: string, to: string) {
    if (this.files.has(to) && from.toLowerCase() !== to.toLowerCase()) throw new Error(`exists: ${to}`);
    const f = this.must(from); this.files.delete(from); this.files.set(to, { ...f, mtime: ++clock });
  }
  async trash(path: string) { this.files.delete(path); this.trashed.push(path); }
  async exists(path: string) { return this.files.has(path); }
  private must(path: string) { const f = this.files.get(path); if (!f) throw new Error(`missing ${path}`); return f; }
}

export function fetchHttp(offline: { on: boolean } = { on: false }): Http {
  return async req => {
    if (offline.on) throw new Error("network down");
    const headers: Record<string, string> = { ...(req.headers ?? {}) };
    if (req.contentType) headers["content-type"] = req.contentType;
    const r = await fetch(req.url, { method: req.method, headers, body: req.body as BodyInit | undefined });
    const buf = await r.arrayBuffer();
    let json: unknown = null;
    try { json = JSON.parse(dec.decode(buf)); } catch { json = null; }
    return { status: r.status, json, arrayBuffer: buf };
  };
}
