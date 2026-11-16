import { App, Modal, Notice, Platform, Plugin, PluginSettingTab, Setting, TAbstractFile, TFolder, debounce, setIcon } from "obsidian";
import { CoveApi } from "./api";
import { SyncEngine, Status } from "./engine";
import { ObsidianVault, obsidianHttp } from "./obsidian-adapter";
import { emptyState, SyncState } from "./types";

interface Settings {
  serverUrl: string;
  token: string;
  deviceName: string;
  intervalSeconds: number;
  syncAttachments: boolean;
  excludedFolders: string;   // one per line
}

const DEFAULTS: Settings = {
  serverUrl: "", token: "", deviceName: "", intervalSeconds: 30, syncAttachments: true, excludedFolders: "",
};

const STATUS_TEXT: Record<Status, string> = {
  idle: "synced", syncing: "syncing…", offline: "offline", unauthorized: "token rejected",
  error: "error", "not-configured": "not set up",
};

export default class CoveSyncPlugin extends Plugin {
  settings: Settings = { ...DEFAULTS };
  state: SyncState = emptyState();
  engine: SyncEngine | null = null;
  activity: string[] = [];
  private statusEl: HTMLElement | null = null;
  private ribbon: HTMLElement | null = null;
  private ready = false;

  private persist = debounce(() => this.saveData({ settings: this.settings, state: this.state }), 1500, true);
  private soon = debounce(() => this.runSync(), 2000, true);

  async onload() {
    const data = (await this.loadData()) ?? {};
    this.settings = { ...DEFAULTS, ...(data.settings ?? {}) };
    this.state = { ...emptyState(), ...(data.state ?? {}) };

    if (!Platform.isMobile) this.statusEl = this.addStatusBarItem();
    this.ribbon = this.addRibbonIcon("refresh-cw", "cove sync: sync now", () => this.runSync(true));
    this.addSettingTab(new CoveSettingTab(this.app, this));

    this.addCommand({ id: "sync-now", name: "Sync now", callback: () => this.runSync(true) });
    this.addCommand({
      id: "push-deletions", name: "Push local deletions (confirm missing files were deleted)",
      callback: () => this.runSync(true, true),
    });
    this.addCommand({ id: "show-activity", name: "Show recent activity", callback: () => new ActivityModal(this.app, this).open() });

    this.rebuild();

    this.app.workspace.onLayoutReady(() => {
      // Register only now: while the vault loads Obsidian fires "create" for every file.
      const v = this.app.vault;
      this.registerEvent(v.on("create", f => this.changed(f)));
      this.registerEvent(v.on("modify", f => this.changed(f)));
      this.registerEvent(v.on("delete", f => { this.engine?.onDelete(f.path, f instanceof TFolder); this.soon(); }));
      this.registerEvent(v.on("rename", (f, old) => { this.engine?.onRename(old, f.path, f instanceof TFolder); this.soon(); }));
      this.ready = true;
      this.runSync(true);
    });
    this.registerDomEvent(window, "focus", () => this.runSync());
    this.resetInterval();
  }

  async onunload() {
    this.persist.run();
  }

  private changed(f: TAbstractFile) {
    if (f instanceof TFolder) return;
    this.engine?.onChange(f.path);
    this.soon();
  }

  /** (Re)create the engine after settings change; the ledger is kept. */
  rebuild() {
    const s = this.settings;
    if (!s.serverUrl || !s.token) { this.engine = null; this.showStatus("not-configured"); return; }
    const api = new CoveApi(obsidianHttp, s.serverUrl, s.token);
    this.engine = new SyncEngine(new ObsidianVault(this.app), api, this.state, {
      deviceName: s.deviceName || "obsidian",
      syncAttachments: s.syncAttachments,
      excludedFolders: s.excludedFolders.split("\n").map(x => x.trim()).filter(Boolean),
      save: () => this.persist(),
      notify: m => new Notice(m, 8000),
      log: m => this.record(m),
      onStatus: (st, detail) => this.showStatus(st, detail),
    });
    this.showStatus("idle");
  }

  private intervalId: number | null = null;
  resetInterval() {
    if (this.intervalId !== null) window.clearInterval(this.intervalId);
    this.intervalId = window.setInterval(() => this.runSync(), Math.max(10, this.settings.intervalSeconds) * 1000);
    this.registerInterval(this.intervalId);
  }

  async runSync(fullScan = false, confirmDeletes = false) {
    if (!this.engine || !this.ready) return;
    await this.engine.sync(fullScan, confirmDeletes);
  }

  record(m: string) {
    this.activity.unshift(`${new Date().toLocaleTimeString()}  ${m}`);
    this.activity.length = Math.min(this.activity.length, 100);
  }

  showStatus(st: Status, detail?: string) {
    const pending = this.engine?.pending ?? 0;
    const when = this.engine?.lastSyncAt ? ` · ${new Date(this.engine.lastSyncAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "";
    const text = `cove ${st === "idle" ? "✓" : st === "syncing" ? "↻" : "⚠"} ${STATUS_TEXT[st]}${st === "idle" ? when : ""}${pending ? ` · ${pending} pending` : ""}`;
    if (this.statusEl) { this.statusEl.setText(text); this.statusEl.title = detail ?? ""; }
    if (this.ribbon) {
      this.ribbon.setAttr("aria-label", text);
      setIcon(this.ribbon, st === "idle" || st === "syncing" ? "refresh-cw" : "refresh-cw-off");
    }
    if (st === "unauthorized") new Notice("cove sync: the device token was rejected. Check Settings → cove sync.", 10000);
  }

  async saveSettings() {
    await this.saveData({ settings: this.settings, state: this.state });
  }
}

class ActivityModal extends Modal {
  constructor(app: App, private plugin: CoveSyncPlugin) { super(app); }
  onOpen() {
    this.titleEl.setText("cove sync · recent activity");
    const e = this.plugin.engine;
    this.contentEl.createEl("p", { text: e ? `Status: ${e.status}${e.lastError ? ` (${e.lastError})` : ""} · ${Object.keys(this.plugin.state.files).length} files tracked · ${e.pending} pending` : "Not configured." });
    const pre = this.contentEl.createEl("pre", { text: this.plugin.activity.join("\n") || "Nothing yet." });
    pre.style.whiteSpace = "pre-wrap"; pre.style.maxHeight = "50vh"; pre.style.overflow = "auto";
  }
  onClose() { this.contentEl.empty(); }
}

class CoveSettingTab extends PluginSettingTab {
  constructor(app: App, private plugin: CoveSyncPlugin) { super(app, plugin); }

  display(): void {
    const { containerEl: el } = this;
    const p = this.plugin;
    el.empty();
    el.createEl("p", {
      text: "Two-way sync with cove Notes. Create a device token in cove under Settings → Devices, " +
        "then paste it here. Your phone needs Tailscale running to reach cove.",
    });

    new Setting(el).setName("Server address").setDesc("e.g. https://your-machine.your-tailnet.ts.net")
      .addText(t => t.setPlaceholder("https://…").setValue(p.settings.serverUrl).onChange(async v => {
        p.settings.serverUrl = v.trim(); await p.saveSettings(); p.rebuild();
      }));

    new Setting(el).setName("Device token").setDesc("Starts with cove_. Shown once when you create it in cove.")
      .addText(t => {
        t.inputEl.type = "password";
        t.setPlaceholder("cove_…").setValue(p.settings.token).onChange(async v => {
          p.settings.token = v.trim(); await p.saveSettings(); p.rebuild();
        });
      })
      .addButton(b => b.setButtonText("Test").onClick(async () => {
        if (!p.settings.serverUrl || !p.settings.token) { new Notice("Fill in the address and token first."); return; }
        try {
          const who = await new CoveApi(obsidianHttp, p.settings.serverUrl, p.settings.token).whoami();
          p.settings.deviceName = who.device; await p.saveSettings(); p.rebuild();
          new Notice(`Connected to cove as “${who.device}”.`);
          this.display();
        } catch (e) { new Notice(`cove sync: ${(e as Error).message}`, 8000); }
      }));

    new Setting(el).setName("Sync every (seconds)").setDesc("Also syncs on startup, when you switch back to Obsidian, and ~2 s after you stop typing.")
      .addText(t => t.setValue(String(p.settings.intervalSeconds)).onChange(async v => {
        const n = parseInt(v, 10); if (!isNaN(n)) { p.settings.intervalSeconds = Math.max(10, n); await p.saveSettings(); p.resetInterval(); }
      }));

    new Setting(el).setName("Sync attachments").setDesc("Images, PDFs and other files (up to 200 MB each).")
      .addToggle(t => t.setValue(p.settings.syncAttachments).onChange(async v => {
        p.settings.syncAttachments = v; await p.saveSettings(); p.rebuild();
      }));

    new Setting(el).setName("Excluded folders").setDesc("One per line. Hidden folders (.obsidian, .trash) are never synced.")
      .addTextArea(t => t.setValue(p.settings.excludedFolders).onChange(async v => {
        p.settings.excludedFolders = v; await p.saveSettings(); p.rebuild();
      }));

    const e = p.engine;
    new Setting(el).setName("Status")
      .setDesc(e ? `${e.status}${e.lastError ? ` — ${e.lastError}` : ""} · device “${p.settings.deviceName || "?"}” · ` +
        `${Object.keys(p.state.files).length} files tracked · last change #${p.state.lastSeq}` : "Not configured yet.")
      .addButton(b => b.setButtonText("Sync now").setCta().onClick(async () => { await p.runSync(true); this.display(); }))
      .addButton(b => b.setButtonText("Activity").onClick(() => new ActivityModal(this.app, p).open()));

    new Setting(el).setName("Reset sync state")
      .setDesc("Forget what this device has synced and compare the whole vault with cove again. Nothing is deleted; differing files become conflict copies.")
      .addButton(b => b.setButtonText("Reset").setWarning().onClick(async () => {
        if (!confirm("Reset cove sync state on this device?")) return;
        Object.assign(p.state, emptyState()); await p.saveSettings(); p.rebuild(); new Notice("cove sync: state reset; next sync compares everything.");
        this.display();
      }));
  }
}
