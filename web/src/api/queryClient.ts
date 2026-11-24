/*
 * TanStack Query setup. Tuned for a self-hosted single-user hub where every
 * request writes an audit row (map-http.md §5): generous staleness, no refetch
 * on window focus, no retry on the structured API errors (a 400/403/404 will not
 * get better by repeating).
 */
import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "./errors";

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 60_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        retry: (failureCount, error) => {
          if (error instanceof ApiError) return false;
          return failureCount < 2;
        },
      },
      mutations: { retry: false },
    },
  });
}
