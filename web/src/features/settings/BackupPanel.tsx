/*
 * Backups panel on Settings: what the scheduler last did, and the settings it
 * reads (schedule, destination, off-site copy, what is included). Edits are a
 * local draft until Save; the server validates and answers 400 naming the bad
 * key. Owner tokens only: other tokens see a note instead.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Archive, CalendarClock, CloudUpload, FolderOpen, KeyRound, ListChecks } from "lucide-react";
import {
  Panel,
  Field,
  Input,
  Select,
  Switch,
  Segmented,
  Badge,
  StaticChip,
  Button,
  KeyValue,
  CodeBlock,
  RelativeTime,
  ErrorState,
  Skeleton,
  toast,
} from "@/ui";
import { ApiError } from "@/api";
import {
  useBackupSettings,
  useBackupStatus,
  useSaveBackupSettings,
  type BackupSettings,
  type BackupStatus,
  type OffsiteKind,
} from "@/api/backups";
import styles from "./Settings.module.css";

const EVERY = [1, 3, 6, 12, 24, 48, 168];
const VERIFY = [
  { hours: 0, label: "Off" },
  { hours: 24, label: "Daily" },
  { hours: 168, label: "Weekly" },
  { hours: 720, label: "Every 30 days" },
];

const REASONS: Record<string, string> = {
  no_backup: "no complete backup yet",
  backup_stale: "last backup is stale",
  last_run_failed: "last run failed",
  no_verify: "no restore drill yet",
  verify_stale: "restore drill is stale",
  disabled: "scheduler disabled",
};

export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i += 1;
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

function everyLabel(h: number): string {
  if (h === 168) return "Weekly";
  if (h === 24) return "Daily";
  if (h % 24 === 0) return `Every ${h / 24} days`;
  return h === 1 ? "Hourly" : `Every ${h} hours`;
}

const lines = (s: string): string[] =>
  s
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);

interface Draft {
  enabled: boolean;
  every_hours: number;
  at: string;
  verify_hours: number;
  dir: string;
  keep: string;
  offsite_kind: OffsiteKind;
  target: string;
  ssh_key: string;
  command: string;
  offsite_keep: string;
  roles: boolean;
  exclude_databases: string;
  paths: string;
  exclude_paths: string;
}

function toDraft(s: BackupSettings): Draft {
  const cmd = s.offsite.command;
  return {
    enabled: s.enabled,
    every_hours: s.schedule.every_hours,
    at: s.schedule.at,
    verify_hours: s.verify.every_hours,
    dir: s.destination.dir,
    keep: String(s.destination.keep),
    offsite_kind: s.offsite.kind,
    target: s.offsite.target ?? "",
    ssh_key: s.offsite.ssh_key ?? "",
    command: Array.isArray(cmd) ? cmd.join(" ") : (cmd ?? ""),
    offsite_keep: String(s.offsite.keep),
    roles: s.include.roles,
    exclude_databases: s.include.exclude_databases.join("\n"),
    paths: s.include.paths.join("\n"),
    exclude_paths: s.include.exclude_paths.join("\n"),
  };
}

function problems(d: Draft): Record<string, string> {
  const out: Record<string, string> = {};
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(d.at)) out.at = "HH:MM, 24-hour";
  if (!d.dir.trim()) out.dir = "Required";
  if (!(Number.parseInt(d.keep, 10) >= 1)) out.keep = "At least 1";
  if (d.offsite_kind !== "none" && !(Number.parseInt(d.offsite_keep, 10) >= 1)) out.offsite_keep = "At least 1";
  if (d.offsite_kind === "rsync" && !d.target.trim()) out.target = "host:/path or a local path";
  if (d.offsite_kind === "command" && !d.command.trim()) out.command = "Required";
  if (!lines(d.paths).length) out.paths = "Nothing would be archived";
  return out;
}

function toPatch(d: Draft) {
  return {
    enabled: d.enabled,
    schedule: { every_hours: d.every_hours, at: d.at },
    verify: { every_hours: d.verify_hours },
    destination: { dir: d.dir.trim(), keep: Number.parseInt(d.keep, 10) },
    offsite: {
      kind: d.offsite_kind,
      target: d.target.trim() || null,
      ssh_key: d.ssh_key.trim() || null,
      command: d.command.trim() || null,
      keep: Number.parseInt(d.offsite_keep, 10) || 1,
    },
    include: {
      roles: d.roles,
      exclude_databases: lines(d.exclude_databases),
      paths: lines(d.paths),
      exclude_paths: lines(d.exclude_paths),
    },
  };
}

export function BackupPanel({ isOwner }: { isOwner: boolean }): ReactNode {
  const settings = useBackupSettings(isOwner);
  const status = useBackupStatus(isOwner);
  const save = useSaveBackupSettings();
  const [draft, setDraft] = useState<Draft | null>(null);

  const server = useMemo(() => (settings.data ? toDraft(settings.data) : null), [settings.data]);
  useEffect(() => {
    if (server) setDraft(server);
  }, [server]);

  if (!isOwner) {
    return (
      <Panel title="Backups" subtitle="Schedule, destination and contents" className={styles.span2}>
        <p className={styles.note}>Backup settings are visible to owner tokens only.</p>
      </Panel>
    );
  }
  if (settings.error) {
    const missing = settings.error instanceof ApiError && settings.error.status === 404;
    return (
      <Panel title="Backups" subtitle="Schedule, destination and contents" className={styles.span2}>
        {missing ? (
          <p className={styles.note}>
            This server has no <code>/v1/backup</code> routes yet. Update Throughline, or configure backups on the box with{" "}
            <CodeBlock inline code="throughline lib backup settings" />.
          </p>
        ) : (
          <ErrorState error={settings.error} title="Could not load backup settings" onRetry={() => void settings.refetch()} />
        )}
      </Panel>
    );
  }
  if (!draft || !server || !settings.data) {
    return (
      <Panel title="Backups" subtitle="Schedule, destination and contents" className={styles.span2}>
        <Skeleton height={180} />
      </Panel>
    );
  }

  const dirty = JSON.stringify(draft) !== JSON.stringify(server);
  const errs = problems(draft);
  const invalid = Object.keys(errs).length > 0;
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft({ ...draft, [key]: value });
  const everyOptions = EVERY.includes(draft.every_hours) ? EVERY : [...EVERY, draft.every_hours].sort((a, b) => a - b);
  const verifyOptions = VERIFY.some((v) => v.hours === draft.verify_hours)
    ? VERIFY
    : [...VERIFY, { hours: draft.verify_hours, label: everyLabel(draft.verify_hours) }];

  const onSave = () => {
    save.mutate(toPatch(draft), {
      onSuccess: () => toast.ok("Backup settings saved", "The scheduler reads them on its next tick."),
      onError: (err) =>
        toast.error("Backup settings not saved", err instanceof Error ? err.message : String(err)),
    });
  };

  return (
    <Panel
      title="Backups"
      subtitle="Every database, the container databases and your files, encrypted, on a schedule"
      className={styles.span2}
      actions={
        <div className={styles.row} style={{ marginTop: 0 }}>
          {dirty && (
            <Button variant="ghost" size="sm" onClick={() => setDraft(server)} disabled={save.isPending}>
              Revert
            </Button>
          )}
          <Button variant="primary" size="sm" onClick={onSave} disabled={!dirty || invalid || save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
      }
    >
      <BackupStatusBlock status={status.data} error={status.error} loading={status.isLoading} />

      <div className={styles.backupGrid}>
        <section className={styles.backupSection}>
          <h4 className={styles.sectionHead}>
            <CalendarClock size={14} /> Schedule
          </h4>
          <Field label="Scheduled backups" inline hint="Off pauses the timer; manual runs still work">
            <Switch checked={draft.enabled} onChange={(v) => set("enabled", v)} label="Scheduled backups" />
          </Field>
          <Field label="Frequency" htmlFor="bk-every">
            <Select id="bk-every" value={draft.every_hours} onChange={(e) => set("every_hours", Number(e.target.value))}>
              {everyOptions.map((h) => (
                <option key={h} value={h}>
                  {everyLabel(h)}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label="Starting at"
            htmlFor="bk-at"
            hint="Local time on the box; later runs follow at the frequency"
            error={errs.at}
          >
            <Input id="bk-at" type="time" mono value={draft.at} invalid={!!errs.at} onChange={(e) => set("at", e.target.value)} />
          </Field>
          <Field label="Restore drill" htmlFor="bk-verify" hint="Restores into a scratch database and checks every part">
            <Select id="bk-verify" value={draft.verify_hours} onChange={(e) => set("verify_hours", Number(e.target.value))}>
              {verifyOptions.map((v) => (
                <option key={v.hours} value={v.hours}>
                  {v.label}
                </option>
              ))}
            </Select>
          </Field>
        </section>

        <section className={styles.backupSection}>
          <h4 className={styles.sectionHead}>
            <FolderOpen size={14} /> Destination
          </h4>
          <Field label="Folder on this machine" htmlFor="bk-dir" error={errs.dir}>
            <Input id="bk-dir" mono value={draft.dir} invalid={!!errs.dir} onChange={(e) => set("dir", e.target.value)} />
          </Field>
          <Field label="Keep" htmlFor="bk-keep" hint="Newest backups kept in the folder" error={errs.keep}>
            <Input
              id="bk-keep"
              type="number"
              min={1}
              mono
              value={draft.keep}
              invalid={!!errs.keep}
              onChange={(e) => set("keep", e.target.value)}
            />
          </Field>

          <h4 className={styles.sectionHead}>
            <CloudUpload size={14} /> Off-site copy
          </h4>
          <Segmented<OffsiteKind>
            aria-label="Off-site copy"
            value={draft.offsite_kind}
            onChange={(v) => set("offsite_kind", v)}
            items={[
              { value: "none", label: "None" },
              { value: "rsync", label: "rsync / ssh" },
              { value: "command", label: "Command" },
            ]}
          />
          {draft.offsite_kind === "none" && (
            <p className={styles.note}>Backups stay on this machine only. A lost disk loses them too.</p>
          )}
          {draft.offsite_kind === "rsync" && (
            <>
              <Field label="Target" htmlFor="bk-target" hint="user@host:/path, or a mounted path" error={errs.target}>
                <Input
                  id="bk-target"
                  mono
                  value={draft.target}
                  invalid={!!errs.target}
                  placeholder="user@nas:/srv/backups/throughline"
                  onChange={(e) => set("target", e.target.value)}
                />
              </Field>
              <Field label="SSH key" htmlFor="bk-key" hint="Optional; the default ssh identity otherwise">
                <Input
                  id="bk-key"
                  mono
                  value={draft.ssh_key}
                  placeholder="~/.ssh/id_ed25519"
                  onChange={(e) => set("ssh_key", e.target.value)}
                />
              </Field>
            </>
          )}
          {draft.offsite_kind === "command" && (
            <Field
              label="Command"
              htmlFor="bk-cmd"
              hint={
                <>
                  Run without a shell; <code>{"{dir}"}</code> is the bundle folder, <code>{"{name}"}</code> its name
                </>
              }
              error={errs.command}
            >
              <Input
                id="bk-cmd"
                mono
                value={draft.command}
                invalid={!!errs.command}
                placeholder="rclone copy {dir} remote:throughline/{name}"
                onChange={(e) => set("command", e.target.value)}
              />
            </Field>
          )}
          {draft.offsite_kind !== "none" && (
            <Field label="Keep off-site" htmlFor="bk-okeep" error={errs.offsite_keep}>
              <Input
                id="bk-okeep"
                type="number"
                min={1}
                mono
                value={draft.offsite_keep}
                invalid={!!errs.offsite_keep}
                onChange={(e) => set("offsite_keep", e.target.value)}
              />
            </Field>
          )}
        </section>
      </div>

      <details className={styles.included}>
        <summary className={styles.sectionHead}>
          <ListChecks size={14} /> What is included
        </summary>
        <div className={styles.backupGrid}>
          <section className={styles.backupSection}>
            <Field
              label="Databases"
              htmlFor="bk-xdb"
              hint="Every database on the server is dumped except these patterns (one per line, * wildcards)"
            >
              <textarea
                id="bk-xdb"
                className={styles.textarea}
                rows={4}
                spellCheck={false}
                value={draft.exclude_databases}
                onChange={(e) => set("exclude_databases", e.target.value)}
              />
            </Field>
            <Field label="Roles and grants" inline hint="pg_dumpall --globals-only">
              <Switch checked={draft.roles} onChange={(v) => set("roles", v)} label="Roles and grants" />
            </Field>
            <Field label="Container databases" hint="Set on the box: include.containers">
              <div className={styles.row} style={{ marginTop: 0 }}>
                {settings.data.include.containers.length ? (
                  settings.data.include.containers.map((c) => (
                    <StaticChip key={c.container}>{c.container}</StaticChip>
                  ))
                ) : (
                  <span className={styles.muted}>none</span>
                )}
              </div>
            </Field>
          </section>
          <section className={styles.backupSection}>
            <Field label="Folders" htmlFor="bk-paths" hint="Archived into one encrypted tar (one per line)" error={errs.paths}>
              <textarea
                id="bk-paths"
                className={styles.textarea}
                rows={4}
                spellCheck={false}
                value={draft.paths}
                onChange={(e) => set("paths", e.target.value)}
              />
            </Field>
            <Field label="Excluded" htmlFor="bk-xpaths" hint="Full paths, or a bare name to skip it at any depth">
              <textarea
                id="bk-xpaths"
                className={styles.textarea}
                rows={7}
                spellCheck={false}
                value={draft.exclude_paths}
                onChange={(e) => set("exclude_paths", e.target.value)}
              />
            </Field>
          </section>
        </div>
      </details>

      <div className={styles.row}>
        <StaticChip lead={<KeyRound size={13} />}>
          {settings.data.encryption.kind === "gpg" ? "gpg AES-256" : "custom encryption command"}
        </StaticChip>
        {settings.data.encryption.kind === "gpg" &&
          (settings.data.encryption.passphrase_present ? (
            <Badge tone="ok">passphrase file present</Badge>
          ) : (
            <Badge tone="bad">passphrase file missing</Badge>
          ))}
      </div>
      <p className={styles.note}>
        {settings.data.encryption.kind === "gpg" && !settings.data.encryption.passphrase_present ? (
          <>
            Backups cannot run until the key exists: <CodeBlock inline code="throughline lib backup init-key" />
          </>
        ) : (
          <>
            Keep a copy of <code>{settings.data.encryption.passphrase_file}</code> off this machine — without it no backup
            can be opened. Run one now with <CodeBlock inline code="throughline lib backup now" />.
          </>
        )}
      </p>
      <p className={styles.note}>
        Stored in <code>{settings.data.config_file}</code>
      </p>
    </Panel>
  );
}

function BackupStatusBlock({
  status,
  error,
  loading,
}: {
  status: BackupStatus | undefined;
  error: unknown;
  loading: boolean;
}): ReactNode {
  if (loading) return <Skeleton height={64} />;
  if (error || !status) return <p className={styles.note}>Backup status unavailable.</p>;
  const bundle = status.latest?.bundle;
  const parts = bundle?.parts ?? [];
  const failedParts = bundle?.errors ?? [];
  const tone = status.ok ? "ok" : status.reasons.includes("last_run_failed") || status.reasons.includes("backup_stale") ? "bad" : "warn";
  return (
    <div className={styles.backupStatus}>
      <div className={styles.row} style={{ marginTop: 0 }}>
        <Badge tone={tone}>{status.ok ? "healthy" : status.reasons.map((r) => REASONS[r] ?? r).join(" · ")}</Badge>
      </div>
      <KeyValue
        rows={[
          {
            key: "Last backup",
            value: status.last_ok_at ? (
              <span>
                <RelativeTime value={status.last_ok_at} />
                {bundle?.ok && bundle.bytes ? <span className={styles.muted}> · {fmtBytes(bundle.bytes)}</span> : null}
              </span>
            ) : (
              "never"
            ),
          },
          {
            key: "Restore drill",
            value: status.last_verified_at ? <RelativeTime value={status.last_verified_at} /> : "never",
          },
          { key: "Next run", value: status.enabled ? <RelativeTime value={status.next_run_at} /> : "paused" },
          {
            key: "Off-site",
            value: status.offsite ? (
              <span>
                <span className="tnum">{status.offsite}</span>
                {bundle?.offsite === "failed" && <Badge tone="bad">last copy failed</Badge>}
              </span>
            ) : (
              <span className={styles.muted}>none</span>
            ),
          },
        ]}
      />
      {parts.length > 0 && (
        <div className={styles.row}>
          {parts.map((p) => (
            <StaticChip key={`${p.kind}:${p.name}`} lead={<Archive size={12} />}>
              {p.name} <span className={styles.muted}>{fmtBytes(p.bytes)}</span>
            </StaticChip>
          ))}
        </div>
      )}
      {failedParts.length > 0 && (
        <ul className={styles.failures}>
          {failedParts.map((f) => (
            <li key={f.part}>
              <code>{f.part}</code> {f.error}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
