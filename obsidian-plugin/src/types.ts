// Platform-neutral contracts. The sync engine only talks to these, so it can run inside
// Obsidian (src/obsidian-adapter.ts) or against an in-memory vault in tests.

export interface FileInfo {
  path: string;
  mtime: number;
  size: number;
}

export interface VaultAdapter {
  /** Every file the vault knows about (Obsidian already hides its config folder). */
  list(): Promise<FileInfo[]>;
  stat(path: string): Promise<FileInfo | null>;
  readText(path: string): Promise<string>;
  readBinary(path: string): Promise<ArrayBuffer>;
  /** Create or overwrite, creating parent folders as needed. */
  writeText(path: string, content: string): Promise<void>;
  writeBinary(path: string, data: ArrayBuffer): Promise<void>;
  /** Move a file, creating parent folders as needed. Must NOT rewrite links in other notes. */
  rename(from: string, to: string): Promise<void>;
  /** Move to the user's trash (recoverable), not a hard delete. */
  trash(path: string): Promise<void>;
  exists(path: string): Promise<boolean>;
}

export interface HttpRequest {
  url: string;
  method: string;
  headers?: Record<string, string>;
  body?: string | ArrayBuffer;
  contentType?: string;
}

export interface HttpResponse {
  status: number;
  json: unknown;
  arrayBuffer: ArrayBuffer;
}

/** Must resolve for any HTTP status; reject only on network failure. */
export type Http = (req: HttpRequest) => Promise<HttpResponse>;

/** Server-side file metadata (see docs/notes-sync.md). */
export interface RemoteFile {
  id: string;
  kind: "note" | "canvas" | "attachment";
  path: string;
  content_hash: string;
  version: number;
  seq?: number;
  deleted?: boolean;
  blob_sha256?: string | null;
  content?: string;
}

export type WriteStatus =
  | "created" | "updated" | "unchanged" | "restored" | "merged" | "deleted"
  | "conflict" | "path_taken" | "not_found" | "invalid";

export interface WriteResult {
  status: WriteStatus;
  file?: RemoteFile;
  content?: string;
  conflict_copy?: RemoteFile;
  existing?: RemoteFile;
  detail?: string;
}

/** What this device last agreed with the server, per local path. */
export interface LedgerEntry {
  id: string;
  version: number;
  hash: string;
  mtime: number;
  size: number;
}

export type Op =
  | { type: "dirty"; path: string }
  | { type: "rename"; from: string; to: string }
  | { type: "delete"; path: string };

export interface SyncState {
  initialized: boolean;
  lastSeq: number;
  files: Record<string, LedgerEntry>;
  ops: Op[];
}

export const emptyState = (): SyncState => ({ initialized: false, lastSeq: 0, files: {}, ops: [] });
