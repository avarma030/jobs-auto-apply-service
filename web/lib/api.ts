import { getToken } from "./auth";
import type {
  Application,
  ApplicationsPage,
  Job,
  JobsPage,
  Profile,
  Run,
  SavedSearchState,
  Settings,
  Stats,
  TokenResponse,
  User,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const headers: HeadersInit = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(init?.headers ?? {}),
  };
  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Request failed");
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// Auth
export const auth = {
  register: (email: string, password: string) =>
    request<TokenResponse>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  login: (email: string, password: string) =>
    request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  me: () => request<User>("/auth/me"),
};

// Jobs
export const jobs = {
  list: (params?: { status?: string; board?: string; keywords?: string; page?: number }) => {
    const q = new URLSearchParams();
    if (params?.status) q.set("status", params.status);
    if (params?.board) q.set("board", params.board);
    if (params?.keywords) q.set("keywords", params.keywords);
    if (params?.page) q.set("page", String(params.page));
    return request<JobsPage>(`/jobs?${q}`);
  },
  get: (id: number) => request<Job>(`/jobs/${id}`),
  updateStatus: (id: number, status: string) =>
    request<Job>(`/jobs/${id}/status`, { method: "PUT", body: JSON.stringify({ status }) }),
  scrape: (body: {
    keywords: string[];
    location: string;
    work_modes?: string[];
    job_types?: string[];
    experience_levels?: string[];
    easy_apply_only?: boolean;
    remote_only?: boolean;
    boards: string[];
    max_age_days?: number;
    max_age_hours?: number;
    max_jobs?: number;
    tailor_documents?: boolean;
    min_match_score?: number;
    save_search?: boolean;
    saved_search_enabled?: boolean;
    saved_search_interval_hours?: 1 | 3;
  }) =>
    request<{ run_id: string }>("/jobs/scrape", { method: "POST", body: JSON.stringify(body) }),
  getSavedSearch: () => request<SavedSearchState>("/jobs/saved-search"),
  /** Returns a download URL for Excel export. Open or assign to window.location.href. */
  exportUrl: (params?: { status?: string; board?: string }): string => {
    const q = new URLSearchParams();
    if (params?.status) q.set("status", params.status);
    if (params?.board) q.set("board", params.board);
    const token = getToken();
    if (token) q.set("token", token);
    return `${BASE}/jobs/export?${q}`;
  },
  artifactUrl: (jobId: number, artifact: "resume" | "cover-letter"): string => {
    const q = new URLSearchParams();
    const token = getToken();
    if (token) q.set("token", token);
    return `${BASE}/jobs/${jobId}/artifacts/${artifact}?${q}`;
  },
};

// Applications
export const applications = {
  list: (params?: { status?: string; page?: number }) => {
    const q = new URLSearchParams();
    if (params?.status) q.set("status", params.status);
    if (params?.page) q.set("page", String(params.page));
    return request<ApplicationsPage>(`/applications?${q}`);
  },
  get: (id: number) => request<Application>(`/applications/${id}`),
  update: (id: number, status: string, notes?: string) =>
    request<Application>(`/applications/${id}`, {
      method: "PUT",
      body: JSON.stringify({ status, notes }),
    }),
};

// Profile
export const profile = {
  get: () => request<{ profile: Profile }>("/profile"),
  update: (data: Profile) =>
    request<{ profile: Profile }>("/profile", { method: "PUT", body: JSON.stringify({ profile: data }) }),
  uploadResume: (file: File): Promise<{
    resume_path: string;
    filename: string;
    extracted_profile?: Record<string, unknown> | null;
    profile_updated?: boolean;
    ai_extraction_enabled?: boolean;
    extraction_error?: string | null;
  }> => {
    const form = new FormData();
    form.append("file", file);
    const token = getToken();
    return fetch(`${BASE}/profile/resume`, {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    }).then((r) => r.json());
  },
};

// Settings
export const settings = {
  get: () => request<Settings>("/settings"),
  update: (data: Partial<Settings>) =>
    request<Settings>("/settings", { method: "PUT", body: JSON.stringify(data) }),
};

// Runs
export const runs = {
  list: () => request<Run[]>("/runs"),
  stop: (id: string) => request<Run>(`/runs/${id}/stop`, { method: "POST" }),
};

// Stats
export const statsApi = {
  get: () => request<Stats>("/stats"),
};
