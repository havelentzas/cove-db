// Thin client for the cove Notes sync API (/api/notes/*). See docs/notes-sync.md.
import type { Http, RemoteFile, WriteResult } from "./types";

export class AuthError extends Error {}
export class NetworkError extends Error {}
export class ServerError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export interface Changes {
  changes: RemoteFile[];
  last_seq: number;
  head_seq: number;
  more: boolean;
}

export class CoveApi {
  private base: string;

  constructor(private http: Http, baseUrl: string, private token: string) {
    this.base = baseUrl.replace(/\/+$/, "") + "/api/notes";
  }

  private async req(method: string, path: string, body?: unknown, raw?: ArrayBuffer, contentType?: string) {
    let res;
    try {
      res = await this.http({
        url: this.base + path,
        method,
        headers: { Authorization: `Bearer ${this.token}` },
        body: raw ?? (body === undefined ? undefined : JSON.stringify(body)),
        contentType: raw ? contentType ?? "application/octet-stream" : body === undefined ? undefined : "application/json",
      });
    } catch (e) {
      throw new NetworkError(`can't reach cove: ${(e as Error).message ?? e}`);
    }
    if (res.status === 401) throw new AuthError("device token was rejected (revoked or mistyped?)");
    if (res.status >= 500) throw new ServerError(res.status, `cove server error ${res.status}`);
    return res;
  }

  /** For writes: 2xx and documented 4xx bodies are results, anything else is an error. */
  private async write(method: string, path: string, body?: unknown): Promise<WriteResult> {
    const res = await this.req(method, path, body);
    const data = res.json as WriteResult | null;
    if (data && typeof data === "object" && "status" in data) return data;
    throw new ServerError(res.status, `unexpected response ${res.status}`);
  }

  async whoami(): Promise<{ device: string; api_version: number; head_seq: number }> {
    return (await this.req("GET", "/whoami")).json as any;
  }

  async manifest(): Promise<{ files: RemoteFile[]; head_seq: number }> {
    return (await this.req("GET", "/manifest")).json as any;
  }

  async changes(since: number, limit = 500): Promise<Changes> {
    return (await this.req("GET", `/changes?since=${since}&limit=${limit}`)).json as Changes;
  }

  async getFile(id: string): Promise<RemoteFile> {
    const res = await this.req("GET", `/files/${id}`);
    if (res.status !== 200) throw new ServerError(res.status, `can't fetch file ${id}`);
    return res.json as RemoteFile;
  }

  putText(id: string, path: string, content: string, baseVersion: number | null) {
    return this.write("PUT", `/files/${id}`, { path, content, base_version: baseVersion });
  }

  putAttachment(id: string, path: string, sha: string, baseVersion: number | null) {
    return this.write("PUT", `/files/${id}`, { path, blob_sha256: sha, base_version: baseVersion });
  }

  rename(id: string, path: string, baseVersion: number) {
    return this.write("POST", `/files/${id}/rename`, { path, base_version: baseVersion });
  }

  remove(id: string, baseVersion: number) {
    return this.write("DELETE", `/files/${id}?base_version=${baseVersion}`);
  }

  async ensureBlob(sha: string, data: ArrayBuffer): Promise<void> {
    const head = await this.req("HEAD", `/blobs/${sha}`);
    if (head.status === 200) return;
    const put = await this.req("PUT", `/blobs/${sha}`, undefined, data);
    if (put.status !== 200) throw new ServerError(put.status, `attachment upload failed (${put.status})`);
  }

  async getBlob(sha: string): Promise<ArrayBuffer> {
    const res = await this.req("GET", `/blobs/${sha}`);
    if (res.status !== 200) throw new ServerError(res.status, `can't download attachment ${sha}`);
    return res.arrayBuffer;
  }
}
