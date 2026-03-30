export interface User {
  id: number;
  email: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface Job {
  id: number;
  title: string;
  company: string;
  location?: string;
  description?: string | null;
  source_board: string;
  url: string;
  job_type?: string;
  work_mode?: string;
  experience_level?: string;
  salary_min?: number;
  salary_max?: number;
  salary_currency?: string;
  easy_apply: boolean;
  posted_at?: string;
  scraped_at: string;
  application_status: string;
  applied_at?: string;
  skills: string[];
  // AI pipeline
  match_score?: number | null;
  ats_score?: number | null;
  ats_type?: string | null;
  tailored_resume_path?: string | null;
  cover_letter_path?: string | null;
}

export interface JobsPage {
  items: Job[];
  total: number;
  page: number;
  page_size: number;
}

export interface Application {
  id: number;
  job_id: number;
  attempted_at: string;
  status: string;
  confirmation_id?: string;
  message?: string;
}

export interface ApplicationsPage {
  items: Application[];
  total: number;
  page: number;
  page_size: number;
}

export interface Stats {
  total_scraped: number;
  total_applied: number;
  total_skipped: number;
  total_failed: number;
  total_interviewing: number;
  total_offered: number;
  total_rejected: number;
  success_rate: number;
  this_week_applied: number;
  by_board: Record<string, number>;
}

export interface Run {
  id: string;
  status: string;
  boards?: string;
  keywords?: string;
  location?: string;
  started_at: string;
  finished_at?: string;
  jobs_found: number;
  jobs_applied: number;
  error_message?: string;
}

export interface Settings {
  auto_apply: boolean;
  max_applications_per_day: number;
  easy_apply_only: boolean;
  headless_browser: boolean;
  dry_run: boolean;
  enabled_boards: string[];
  preferred_work_modes: string[];
  blacklisted_companies: string[];
  request_delay_seconds: number;
  custom_answers: Record<string, string>;
}

export interface SearchCriteria {
  keywords: string[];
  location?: string;
  work_modes?: string[];
  job_types?: string[];
  experience_levels?: string[];
  easy_apply_only?: boolean;
  remote_only?: boolean;
  boards: string[];
  max_age_days?: number;
  max_age_hours?: number | null;
  max_jobs?: number | null;
  tailor_documents?: boolean;
  min_match_score?: number | null;
}

export interface SavedSearchState {
  enabled: boolean;
  interval_hours: 1 | 3;
  criteria?: SearchCriteria | null;
  last_triggered_at?: string | null;
  last_run_id?: string | null;
  next_trigger_at?: string | null;
}

export interface Profile {
  first_name?: string;
  last_name?: string;
  email?: string;
  phone?: string;
  address?: {
    street?: string;
    city?: string;
    state?: string;
    zip_code?: string;
    country?: string;
  };
  headline?: string;
  summary?: string;
  years_of_experience?: number;
  skills?: string[];
  languages?: string[];
  work_experience?: Array<{
    company: string;
    title: string;
    start_date: string;
    end_date?: string;
    description?: string;
    location?: string;
  }>;
  education?: Array<{
    institution: string;
    degree: string;
    field_of_study?: string;
    start_date?: string;
    end_date?: string;
    gpa?: number;
  }>;
  social_links?: {
    linkedin?: string;
    github?: string;
    portfolio?: string;
    twitter?: string;
  };
  custom_answers?: Record<string, string>;
  resume_path?: string;
  job_board_accounts?: Record<string, { username?: string; password?: string }>;
}
