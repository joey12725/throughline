/*
 * Whole-install backups (throughline.backups): settings and status.
 *   GET /v1/backup/settings  -> BackupSettings (owner tokens only)
 *   PUT /v1/backup/settings  body: a partial BackupSettings, deep-merged and
 *                            validated server-side (400 names the bad key)
 *   GET /v1/backup/status    -> BackupStatus
 * Running a bundle is a box job (systemd timer / `throughline lib backup now`);
 * the browser only edits what the scheduler reads.
 */
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { request } from "./client";

export type OffsiteKind = "none" | "rsync" | "command";

export interface BackupSettings {
  enabled: boolean;
  schedule: { every_hours: number; at: string };
  verify: { every_hours: number };
  destination: { dir: string; keep: number };
  offsite: {
    kind: OffsiteKind;
    target: string | null;
    ssh_key: string | null;
    command: string | string[] | null;
    keep: number;
  };
  encryption: {
    kind: "gpg" | "command";
    passphrase_file: string | null;
    passphrase_present: boolean;
  };
  include: {
    databases: "all" | string[];
    exclude_databases: string[];
    roles: boolean;
    containers: Array<{ container: string; user?: string }>;
    paths: string[];
    exclude_paths: string[];
  };
  retry_minutes: number;
  config_file: string;
}

export interface BackupPart {
  name: string;
  kind: "throughline" | "globals" | "database" | "container" | "files";
  bytes: number;
}

export interface BackupStatus {
  ok: boolean;
  reasons: string[];
  enabled: boolean;
  next_run_at: string;
  last_attempt_at: string | null;
  last_ok_at: string | null;
  backup_age_hours: number | null;
  last_verified_at: string | null;
  verify_age_hours: number | null;
  stale_hours: number;
  verify_stale_hours: number | null;
  destination: string;
  offsite: string | null;
  latest: {
    status: "ok" | "failed";
    started_at: string;
    finished_at: string | null;
    error: string | null;
    bundle: {
      ok: boolean;
      pending?: boolean;
      name?: string;
      bytes?: number;
      parts?: BackupPart[];
      errors?: Array<{ part: string; error: string }>;
      offsite?: "ok" | "failed" | null;
    } | null;
  } | null;
}

export type BackupSettingsPatch = {
  [K in keyof BackupSettings]?: BackupSettings[K] extends object
    ? Partial<BackupSettings[K]>
    : BackupSettings[K];
};

const SETTINGS_KEY = ["backup", "settings"] as const;
const STATUS_KEY = ["backup", "status"] as const;

export function useBackupSettings(enabled = true): UseQueryResult<BackupSettings> {
  return useQuery({
    queryKey: SETTINGS_KEY,
    queryFn: ({ signal }) => request<BackupSettings>("/backup/settings", { signal }),
    enabled,
    retry: false,
    staleTime: 60_000,
  });
}

export function useBackupStatus(enabled = true): UseQueryResult<BackupStatus> {
  return useQuery({
    queryKey: STATUS_KEY,
    queryFn: ({ signal }) => request<BackupStatus>("/backup/status", { signal }),
    enabled,
    retry: false,
    refetchInterval: 60_000,
  });
}

export function useSaveBackupSettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (patch: BackupSettingsPatch) =>
      request<BackupSettings>("/backup/settings", { method: "PUT", body: patch }),
    onSuccess: (saved) => {
      client.setQueryData(SETTINGS_KEY, saved);
      void client.invalidateQueries({ queryKey: STATUS_KEY });
    },
  });
}
