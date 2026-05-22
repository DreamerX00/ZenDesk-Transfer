/**
 * Shared TypeScript types. Mirrors the Pydantic models on the backend
 * (server/app.py) — keep these in sync.
 */

export type Role = "source" | "target";

export interface MaskedConnection {
  id: string;
  role: Role;
  subdomain: string;
  auth_kind: "oauth" | "api_token";
  account_name: string | null;
  oauth_token: string | null;
  api_token: string | null;
  email: string | null;
}

export interface SessionResponse {
  token: string;
  subdomain: string;
  user_id: number;
  user_email: string;
}

export interface PreflightResult {
  source: { ok: boolean; subdomain?: string; account_name?: string | null; error?: string };
  target: { ok: boolean; subdomain?: string; account_name?: string | null; error?: string };
  source_baseline?: Array<{ resource: string; count: number }>;
  source_baseline_error?: string;
  baseline?: Array<{ resource: string; count: number }>;
  baseline_error?: string;
}

export interface MigrateRequest {
  source_connection_id: string;
  target_connection_id: string;
  phases: number[] | null;
  max_users: number | null;
  users_from: number;
  dry_run: boolean;
  format_target: boolean;
}

export interface MigrateResponse {
  migration_id: string;
  rq_job_id: string;
}

export interface JobStatusResponse {
  migration_id: string;
  status: Record<string, string>;
  log_tail: LogRecord[];
}

export interface LogRecord {
  ts: string;
  action: "CREATED" | "PURGED" | "SKIPPED" | "FAILED" | "MANUAL" | "NOTE";
  resource?: string;
  source_id?: string | number;
  target_id?: string | number;
  name?: string;
  reason?: string;
  error?: string;
  note?: string;
}

export interface BackupInfo {
  path: string;
  name: string;
  resource_count: number | null;
}

export interface MigrationInfo {
  migration_id: string;
  created_at: string;
  phase: string;
  has_report: boolean;
  has_log: boolean;
  has_id_map: boolean;
}

export type WizardStep =
  | "preflight"
  | "source-auth"
  | "choose-phases"
  | "preview-confirm"
  | "progress"
  | "report";
