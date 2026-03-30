"use client";

import { useCallback, useEffect, useState } from "react";
import { jobs as jobsApi, runs as runsApi, statsApi } from "@/lib/api";
import type {
  Run,
  RunDetail,
  RunJobSummary,
  RunSearchCriteria,
  SavedSearchRunSummary,
  SavedSearchState,
  SearchCriteria,
  Stats,
} from "@/lib/types";
import { TopBar } from "@/components/layout/TopBar";
import { StatsCards } from "@/components/dashboard/StatsCards";
import { RunProgress } from "@/components/dashboard/RunProgress";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatDistanceToNow } from "date-fns";

const EMPTY_STATS: Stats = {
  total_scraped: 0,
  total_applied: 0,
  total_skipped: 0,
  total_failed: 0,
  total_interviewing: 0,
  total_offered: 0,
  total_rejected: 0,
  success_rate: 0,
  this_week_applied: 0,
  by_board: {},
};

const EMPTY_RUN_SUMMARY: RunJobSummary = {
  total: 0,
  pending: 0,
  applied: 0,
  skipped: 0,
  failed: 0,
  interviewed: 0,
  offered: 0,
  rejected: 0,
};

function statusVariant(status: string) {
  switch (status) {
    case "done":
    case "applied":
      return "success" as const;
    case "failed":
      return "destructive" as const;
    case "running":
      return "info" as const;
    case "skipped":
    case "stopped":
      return "warning" as const;
    default:
      return "secondary" as const;
  }
}

function formatDateTime(value?: string | null) {
  if (!value) return "Not available";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return "Not available";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(dt);
}

function formatRelative(value?: string | null) {
  if (!value) return "Unknown time";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return "Unknown time";
  return formatDistanceToNow(dt, { addSuffix: true });
}

function formatAgeWindow(criteria?: SearchCriteria | RunSearchCriteria | null) {
  if (!criteria) return "Any time";
  if (criteria.max_age_hours != null) {
    const hours = criteria.max_age_hours;
    if (hours < 24) return `${hours}h`;
    if (hours % 24 === 0) return `${hours / 24}d`;
    return `${hours}h`;
  }
  if (criteria.max_age_days != null) return `${criteria.max_age_days}d`;
  return "Any time";
}

function formatList(items?: string[] | null, fallback = "Any") {
  return items && items.length > 0 ? items.join(", ") : fallback;
}

function normalizeSummary(summary?: Partial<RunJobSummary> | Record<string, number> | null): RunJobSummary {
  if (!summary) return EMPTY_RUN_SUMMARY;
  return {
    total: summary.total ?? 0,
    pending: summary.pending ?? 0,
    applied: summary.applied ?? 0,
    skipped: summary.skipped ?? 0,
    failed: summary.failed ?? 0,
    interviewed: summary.interviewed ?? 0,
    offered: summary.offered ?? 0,
    rejected: summary.rejected ?? 0,
  };
}

function criteriaRows(criteria?: SearchCriteria | RunSearchCriteria | null) {
  if (!criteria) return [];
  return [
    { label: "Keywords", value: formatList(criteria.keywords, "None") },
    { label: "Location", value: criteria.location || "Anywhere" },
    { label: "Boards", value: formatList(criteria.boards, "Default") },
    { label: "Work modes", value: formatList(criteria.work_modes) },
    { label: "Job types", value: formatList(criteria.job_types) },
    { label: "Experience", value: formatList(criteria.experience_levels) },
    { label: "Easy Apply only", value: criteria.easy_apply_only ? "Yes" : "No" },
    { label: "Remote only", value: criteria.remote_only ? "Yes" : "No" },
    { label: "Posted within", value: formatAgeWindow(criteria) },
    { label: "Max jobs", value: criteria.max_jobs != null ? String(criteria.max_jobs) : "No limit" },
    { label: "Tailor documents", value: criteria.tailor_documents ? "Yes" : "No" },
    {
      label: "Min match score",
      value: criteria.min_match_score != null ? `${criteria.min_match_score}%` : "Default",
    },
  ];
}

function CriteriaPreview({ criteria }: { criteria?: SearchCriteria | RunSearchCriteria | null }) {
  const rows = criteriaRows(criteria).filter((row) => !["Work modes", "Job types", "Experience"].includes(row.label));
  if (rows.length === 0) {
    return <p className="text-xs text-muted-foreground">No saved search criteria yet.</p>;
  }
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground">
      {rows.map((row) => (
        <div key={row.label}>
          <span className="font-medium text-foreground">{row.label}:</span> {row.value}
        </div>
      ))}
    </div>
  );
}

function SummaryPills({ summary }: { summary?: Partial<RunJobSummary> | Record<string, number> | null }) {
  const normalized = normalizeSummary(summary);
  const pills = [
    ["Found", normalized.total],
    ["Pending", normalized.pending],
    ["Applied", normalized.applied],
    ["Skipped", normalized.skipped],
    ["Failed", normalized.failed],
  ].filter(([, count]) => Number(count) > 0);

  if (pills.length === 0) {
    return <p className="text-xs text-muted-foreground">No job activity recorded yet.</p>;
  }

  return (
    <div className="flex flex-wrap gap-2">
      {pills.map(([label, count]) => (
        <Badge key={String(label)} variant="secondary" className="text-xs">
          {label}: {count}
        </Badge>
      ))}
    </div>
  );
}

function RunListItem({
  run,
  active,
  onOpen,
}: {
  run: Run | SavedSearchRunSummary;
  active: boolean;
  onOpen: (id: string) => void;
}) {
  const summary = normalizeSummary("job_summary" in run ? run.job_summary : undefined);
  const criteria = "search_criteria" in run ? run.search_criteria : undefined;

  return (
    <button
      type="button"
      onClick={() => onOpen(run.id)}
      className={`w-full rounded-lg border p-3 text-left transition-colors ${
        active ? "border-blue-500 bg-blue-50" : "border-gray-200 hover:border-blue-300 hover:bg-gray-50"
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="space-y-1">
          <p className="font-medium text-sm">{("keywords" in run && run.keywords) || "Saved search run"}</p>
          <p className="text-xs text-muted-foreground">
            {formatRelative(run.started_at)} · {run.jobs_found} jobs found · {run.jobs_applied} applied
          </p>
        </div>
        <div className="flex items-center gap-2">
          {"trigger_type" in run && run.trigger_type ? (
            <Badge variant="outline" className="text-xs">
              {run.trigger_type === "saved_search" ? "Auto" : "Manual"}
            </Badge>
          ) : null}
          <Badge variant={statusVariant(run.status)}>{run.status}</Badge>
        </div>
      </div>
      {criteria ? <div className="mt-3"><CriteriaPreview criteria={criteria} /></div> : null}
      <div className="mt-3">
        <SummaryPills summary={summary} />
      </div>
      {"error_message" in run && run.error_message ? (
        <p className="mt-2 text-xs text-rose-700">{run.error_message}</p>
      ) : null}
    </button>
  );
}

export default function DashboardPage() {
  const [stats, setStats] = useState<Stats>(EMPTY_STATS);
  const [recentRuns, setRecentRuns] = useState<Run[]>([]);
  const [savedSearch, setSavedSearch] = useState<SavedSearchState | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<RunDetail | null>(null);
  const [loadingRunDetail, setLoadingRunDetail] = useState(false);
  const [runDetailError, setRunDetailError] = useState<string | null>(null);

  const loadRunDetail = useCallback(async (runId: string) => {
    setLoadingRunDetail(true);
    setRunDetailError(null);
    try {
      const detail = await runsApi.get(runId);
      setSelectedRun(detail);
    } catch (error: any) {
      setSelectedRun(null);
      setRunDetailError(error.message ?? "Could not load run details.");
    } finally {
      setLoadingRunDetail(false);
    }
  }, []);

  const reloadBase = useCallback(async () => {
    const [statsResponse, runsResponse, savedSearchResponse] = await Promise.all([
      statsApi.get().catch(() => EMPTY_STATS),
      runsApi.list().catch(() => []),
      jobsApi.getSavedSearch().catch(() => null),
    ]);
    setStats(statsResponse);
    setRecentRuns(runsResponse.slice(0, 8));
    setSavedSearch(savedSearchResponse);
  }, []);

  const reload = useCallback(async () => {
    await reloadBase();
    if (selectedRunId) {
      await loadRunDetail(selectedRunId);
    }
  }, [loadRunDetail, reloadBase, selectedRunId]);

  useEffect(() => {
    void reloadBase();
  }, [reloadBase]);

  useEffect(() => {
    if (!selectedRunId) return;
    void loadRunDetail(selectedRunId);
  }, [loadRunDetail, selectedRunId]);

  const savedSearchRuns = savedSearch?.runs ?? [];
  const selectedRunCriteria = selectedRun?.search_criteria;

  return (
    <div className="flex-1 overflow-y-auto">
      <TopBar title="Dashboard" subtitle="Your application overview" />
      <div className="p-6 space-y-6">
        <StatsCards stats={stats} />

        <div className="grid grid-cols-1 xl:grid-cols-[1.4fr,1fr] gap-6">
          <RunProgress onComplete={reload} />

          <div className="space-y-6">
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Saved Recurring Search</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                {savedSearch?.criteria ? (
                  <>
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant={savedSearch.enabled ? "success" : "secondary"}>
                        {savedSearch.enabled ? "Active" : "Paused"}
                      </Badge>
                      <Badge variant="outline">Every {savedSearch.interval_hours}h</Badge>
                      <Badge variant="outline">Runs so far: {savedSearch.run_count}</Badge>
                    </div>
                    <CriteriaPreview criteria={savedSearch.criteria} />
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
                      <div className="rounded-lg border p-3">
                        <p className="text-xs text-muted-foreground">Last triggered</p>
                        <p className="mt-1 font-medium">{formatDateTime(savedSearch.last_triggered_at)}</p>
                      </div>
                      <div className="rounded-lg border p-3">
                        <p className="text-xs text-muted-foreground">Next trigger</p>
                        <p className="mt-1 font-medium">{formatDateTime(savedSearch.next_trigger_at)}</p>
                      </div>
                    </div>
                    <div className="space-y-3">
                      <div className="flex items-center justify-between">
                        <p className="text-sm font-medium">Recent scheduled/manual runs for this search</p>
                        {savedSearch.last_run_id ? (
                          <Button variant="ghost" size="sm" onClick={() => setSelectedRunId(savedSearch.last_run_id!)}>
                            Open latest
                          </Button>
                        ) : null}
                      </div>
                      {savedSearchRuns.length === 0 ? (
                        <p className="text-sm text-muted-foreground">
                          No runs have been recorded for this saved search yet.
                        </p>
                      ) : (
                        <div className="space-y-3">
                          {savedSearchRuns.map((run) => (
                            <RunListItem
                              key={run.id}
                              run={run}
                              active={selectedRunId === run.id}
                              onOpen={setSelectedRunId}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  </>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    No saved recurring search yet. Start a new scrape with recurring search enabled to populate this section.
                  </p>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="text-base">Recent Runs</CardTitle>
              </CardHeader>
              <CardContent>
                {recentRuns.length === 0 ? (
                  <p className="text-sm text-muted-foreground text-center py-6">
                    No runs yet. Start a scrape to begin.
                  </p>
                ) : (
                  <div className="space-y-3">
                    {recentRuns.map((run) => (
                      <RunListItem
                        key={run.id}
                        run={run}
                        active={selectedRunId === run.id}
                        onOpen={setSelectedRunId}
                      />
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Run Details</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {!selectedRunId ? (
              <p className="text-sm text-muted-foreground">
                Click any recent run or saved-search run to inspect the full criteria and the jobs found in that run.
              </p>
            ) : loadingRunDetail ? (
              <p className="text-sm text-muted-foreground">Loading run details…</p>
            ) : runDetailError ? (
              <p className="text-sm text-rose-700">{runDetailError}</p>
            ) : selectedRun ? (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={statusVariant(selectedRun.status)}>{selectedRun.status}</Badge>
                  {selectedRun.trigger_type ? (
                    <Badge variant="outline">
                      {selectedRun.trigger_type === "saved_search" ? "Saved search" : "Manual run"}
                    </Badge>
                  ) : null}
                  <Badge variant="outline">{selectedRun.jobs_found} jobs found</Badge>
                  <Badge variant="outline">{selectedRun.jobs_applied} applied</Badge>
                  <Badge variant="outline">Started {formatDateTime(selectedRun.started_at)}</Badge>
                  {selectedRun.finished_at ? (
                    <Badge variant="outline">Finished {formatDateTime(selectedRun.finished_at)}</Badge>
                  ) : null}
                </div>

                {selectedRun.error_message ? (
                  <p className="text-sm text-rose-700">{selectedRun.error_message}</p>
                ) : null}

                <div className="rounded-lg border p-4 space-y-3">
                  <p className="font-medium text-sm">Search criteria</p>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 text-sm">
                    {criteriaRows(selectedRunCriteria).map((row) => (
                      <div key={row.label}>
                        <span className="font-medium">{row.label}:</span> {row.value}
                      </div>
                    ))}
                  </div>
                </div>

                <div className="rounded-lg border p-4 space-y-3">
                  <p className="font-medium text-sm">Job summary</p>
                  <SummaryPills summary={selectedRun.job_summary} />
                </div>

                <div className="rounded-lg border p-4 space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <p className="font-medium text-sm">Jobs found in this run</p>
                    <Badge variant="secondary">{selectedRun.jobs.length}</Badge>
                  </div>
                  {selectedRun.jobs.length === 0 ? (
                    <p className="text-sm text-muted-foreground">
                      No job records were attached to this run.
                    </p>
                  ) : (
                    <div className="space-y-3">
                      {selectedRun.jobs.map((job) => (
                        <div key={job.id} className="rounded-lg border p-3">
                          <div className="flex flex-wrap items-start justify-between gap-3">
                            <div>
                              <p className="font-medium text-sm">{job.title}</p>
                              <p className="text-sm text-muted-foreground">
                                {job.company}
                                {job.location ? ` · ${job.location}` : ""}
                              </p>
                            </div>
                            <div className="flex flex-wrap items-center gap-2">
                              <Badge variant={statusVariant(job.application_status)}>{job.application_status}</Badge>
                              <Badge variant="outline" className="capitalize">{job.source_board}</Badge>
                              {job.match_score != null ? (
                                <Badge variant="outline">Match {Math.round(job.match_score)}%</Badge>
                              ) : null}
                            </div>
                          </div>
                          <div className="mt-3 flex flex-wrap gap-3 text-xs text-muted-foreground">
                            <span>Scraped {formatDateTime(job.scraped_at)}</span>
                            {job.posted_at ? <span>Posted {formatDateTime(job.posted_at)}</span> : null}
                            {job.applied_at ? <span>Applied {formatDateTime(job.applied_at)}</span> : null}
                          </div>
                          <div className="mt-3">
                            <a
                              href={job.url}
                              target="_blank"
                              rel="noreferrer"
                              className="text-sm text-blue-600 hover:text-blue-800 hover:underline"
                            >
                              Open job posting
                            </a>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">Run details are not available for the selected run.</p>
            )}
          </CardContent>
        </Card>

        {Object.keys(stats.by_board).length > 0 && (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Jobs by Board</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex flex-wrap gap-3">
                {Object.entries(stats.by_board).map(([board, count]) => (
                  <div key={board} className="flex items-center gap-2 bg-gray-50 rounded-lg px-3 py-2">
                    <span className="text-sm font-medium capitalize">{board}</span>
                    <Badge variant="secondary">{count}</Badge>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
