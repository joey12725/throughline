/*
 * The Ledger's non-SQL surface: proposals, approvals and the spend ledger over
 * their HTTP routes, and the three write calls that decide a pending item. The
 * decisions are owner-gated by the server; the UI only ever issues them behind
 * an explicit confirmation (see Decisions.tsx). Audit comes from the arrivals
 * reads lane (owner only) and is read where it is shown.
 */
import { useQuery } from "@tanstack/react-query";
import { request } from "@/api";

export interface Proposal {
  id: number;
  kind: string;
  payload: Record<string, unknown>;
  evidence?: Record<string, unknown>;
  origin: string;
  asker: string | null;
  message_ids?: number[];
  status: string;
  created_at: string;
  decided_by?: string | null;
  decided_at?: string | null;
}
export interface Approval {
  id: string;
  asker: string | null;
  estimate: Record<string, unknown>;
  cost: string | number;
  limit_cost: string | number;
  explanation: string | null;
  status: string;
  created_at: string;
  decided_at?: string | null;
}
export interface SpendEntryRow {
  purpose?: string;
  provider?: string;
  model?: string;
  origin?: string;
  cost?: number;
  tokens?: number;
  calls?: number;
  first_at?: string;
  last_at?: string;
}
export interface Spend {
  cost: number;
  tokens: number;
  calls: number;
  by: SpendEntryRow[];
}

export function useProposals(status?: string) {
  const qs = status ? `?status=${status}&limit=200` : "?limit=200";
  return useQuery({
    queryKey: ["ledger", "proposals", status ?? "all"],
    queryFn: ({ signal }) => request<Proposal[]>(`/proposals${qs}`, { signal }),
    staleTime: 30_000,
  });
}

export function useApprovals(status?: string) {
  const qs = status ? `?status=${status}` : "";
  return useQuery({
    queryKey: ["ledger", "approvals", status ?? "all"],
    queryFn: ({ signal }) => request<Approval[]>(`/approvals${qs}`, { signal }),
    staleTime: 30_000,
  });
}

export function useSpend(enabled: boolean) {
  return useQuery({
    queryKey: ["ledger", "spend"],
    queryFn: ({ signal }) => request<Spend>(`/spend`, { signal }),
    enabled,
    staleTime: 60_000,
  });
}

export function approveApproval(id: string): Promise<unknown> {
  return request(`/approvals/${encodeURIComponent(id)}/approve`, { method: "POST" });
}
export function rejectApproval(id: string): Promise<unknown> {
  return request(`/approvals/${encodeURIComponent(id)}/reject`, { method: "POST" });
}
export function decideProposal(id: number, accept: boolean, note?: string): Promise<unknown> {
  return request(`/proposals/${id}/decide`, { body: { accept, ...(note ? { note } : {}) } });
}
