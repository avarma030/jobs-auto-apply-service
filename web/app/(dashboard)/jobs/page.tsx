"use client";

import { useCallback, useEffect, useState } from "react";
import { jobs as jobsApi } from "@/lib/api";
import type { Job, JobsPage } from "@/lib/types";
import { TopBar } from "@/components/layout/TopBar";
import { StatusBadge } from "@/components/applications/StatusBadge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { ExternalLink, Check, X, ChevronLeft, ChevronRight, Download, ChevronDown, ChevronUp } from "lucide-react";
import { formatDistanceToNow } from "date-fns";

// "" = Active view (hides skipped/rejected). All other values are explicit status filters.
const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "Active" },
  { value: "pending", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "applied", label: "Applied" },
  { value: "failed", label: "Failed" },
  { value: "skipped", label: "Rejected (<75%)" },
];

function MatchScoreBadge({ score }: { score?: number | null }) {
  if (score == null) return <span className="text-gray-400 text-xs">–</span>;
  const pct = Math.round(score);
  const color =
    pct >= 90 ? "bg-green-100 text-green-800 border-green-300"
    : pct >= 75 ? "bg-yellow-100 text-yellow-800 border-yellow-300"
    : "bg-red-100 text-red-700 border-red-300";
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold border ${color}`}>
      {pct}%
    </span>
  );
}

function AtsBadge({ score, atsType }: { score?: number | null; atsType?: string | null }) {
  if (score == null && !atsType) return <span className="text-gray-400 text-xs">–</span>;
  const pct = score != null ? Math.round(score) : null;
  const color =
    pct == null ? "bg-gray-100 text-gray-600 border-gray-300"
    : pct >= 90 ? "bg-green-100 text-green-800 border-green-300"
    : "bg-orange-100 text-orange-800 border-orange-300";
  return (
    <div className="flex flex-col gap-0.5">
      {pct != null && (
        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold border ${color}`}>
          ATS {pct}%
        </span>
      )}
      {atsType && <span className="text-xs text-gray-500 capitalize">{atsType}</span>}
    </div>
  );
}

function hasDownloadableArtifacts(job: Job) {
  return (
    job.application_status === "applied" &&
    Boolean(job.tailored_resume_path || job.cover_letter_path)
  );
}

export default function JobsPage() {
  const [data, setData] = useState<JobsPage>({ items: [], total: 0, page: 1, page_size: 25 });
  const [status, setStatus] = useState("");   // "" = Active view (no skipped)
  const [keywords, setKeywords] = useState("");
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await jobsApi.list({ status: status || undefined, keywords: keywords || undefined, page });
      setData(res);
    } finally {
      setLoading(false);
    }
  }, [status, keywords, page]);

  useEffect(() => { load(); }, [load]);

  async function updateStatus(id: number, newStatus: string) {
    await jobsApi.updateStatus(id, newStatus);
    setData((prev) => ({
      ...prev,
      items: prev.items.map((j) => j.id === id ? { ...j, application_status: newStatus } : j),
    }));
  }

  function exportExcel() {
    const url = jobsApi.exportUrl({ status: status || undefined });
    window.open(url, "_blank");
  }

  const totalPages = Math.ceil(data.total / data.page_size);

  return (
    <div className="flex-1 overflow-y-auto">
      <TopBar
        title="Job Board"
        subtitle={`${data.total} jobs`}
      />
      <div className="p-6 space-y-4">
        {/* Filters */}
        <div className="flex gap-3 flex-wrap items-center">
          <Input
            placeholder="Search title…"
            value={keywords}
            onChange={(e) => { setKeywords(e.target.value); setPage(1); }}
            className="w-56"
          />
          <div className="flex gap-2 flex-wrap">
            {STATUS_OPTIONS.map(({ value, label }) => (
              <button
                key={value}
                onClick={() => { setStatus(value); setPage(1); }}
                className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
                  status === value
                    ? "bg-blue-600 text-white border-blue-600"
                    : "border-gray-300 text-gray-600 hover:border-blue-400"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="ml-auto">
            <Button variant="outline" size="sm" onClick={exportExcel}>
              <Download className="h-4 w-4 mr-1.5" />
              Export Excel
            </Button>
          </div>
        </div>

        {/* Table */}
        <div className="rounded-lg border bg-white overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-gray-700">Job</th>
                <th className="px-4 py-3 text-left font-medium text-gray-700 hidden lg:table-cell">Match</th>
                <th className="px-4 py-3 text-left font-medium text-gray-700 hidden lg:table-cell">ATS</th>
                <th className="px-4 py-3 text-left font-medium text-gray-700 hidden md:table-cell">Board</th>
                <th className="px-4 py-3 text-left font-medium text-gray-700 hidden lg:table-cell">Posted</th>
                <th className="px-4 py-3 text-left font-medium text-gray-700">Status</th>
                <th className="px-4 py-3 text-left font-medium text-gray-700">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {loading ? (
                <tr><td colSpan={7} className="px-4 py-8 text-center text-muted-foreground">Loading…</td></tr>
              ) : data.items.length === 0 ? (
                <tr><td colSpan={7} className="px-4 py-8 text-center text-muted-foreground">
                  {status === "" ? "No active jobs. Start a scrape from the Dashboard." : "No jobs found for this filter."}
                </td></tr>
              ) : (
                data.items.flatMap((job) => {
                  const isExpanded = expandedId === job.id;
                  const showArtifacts = hasDownloadableArtifacts(job);
                  return [
                    <tr key={job.id} className="hover:bg-gray-50 cursor-pointer"
                      onClick={() => setExpandedId(isExpanded ? null : job.id)}>
                      <td className="px-4 py-3">
                        <div>
                          <div className="flex items-center gap-1.5">
                            <p className="font-medium text-gray-900">{job.title}</p>
                            {job.description && (
                              isExpanded
                                ? <ChevronUp className="h-3.5 w-3.5 text-gray-400 flex-shrink-0" />
                                : <ChevronDown className="h-3.5 w-3.5 text-gray-400 flex-shrink-0" />
                            )}
                          </div>
                          <p className="text-gray-500 text-xs">{job.company} · {job.location ?? "Remote"}</p>
                          <div className="flex gap-1.5 mt-1">
                            {job.easy_apply && <Badge variant="info" className="text-xs py-0">Easy Apply</Badge>}
                            {job.work_mode && <Badge variant="outline" className="text-xs py-0 capitalize">{job.work_mode}</Badge>}
                            {job.experience_level && <Badge variant="outline" className="text-xs py-0 capitalize">{job.experience_level}</Badge>}
                            {job.salary_min && (
                              <Badge variant="outline" className="text-xs py-0 text-green-700 border-green-300">
                                {job.salary_currency ?? "$"}{(job.salary_min / 1000).toFixed(0)}k{job.salary_max ? `–${(job.salary_max / 1000).toFixed(0)}k` : "+"}
                              </Badge>
                            )}
                            {job.tailored_resume_path && <Badge variant="outline" className="text-xs py-0 text-purple-700 border-purple-300">Tailored</Badge>}
                            {showArtifacts && <Badge variant="outline" className="text-xs py-0 text-blue-700 border-blue-300">Downloads</Badge>}
                          </div>
                        </div>
                      </td>
                      <td className="px-4 py-3 hidden lg:table-cell">
                        <MatchScoreBadge score={job.match_score} />
                      </td>
                      <td className="px-4 py-3 hidden lg:table-cell">
                        <AtsBadge score={job.ats_score} atsType={job.ats_type} />
                      </td>
                      <td className="px-4 py-3 hidden md:table-cell">
                        <span className="capitalize text-gray-600">{job.source_board}</span>
                      </td>
                      <td className="px-4 py-3 hidden lg:table-cell text-gray-500 text-xs">
                        {job.posted_at ? formatDistanceToNow(new Date(job.posted_at), { addSuffix: true }) : "–"}
                      </td>
                      <td className="px-4 py-3">
                        <StatusBadge status={job.application_status} />
                      </td>
                      <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                        <div className="flex gap-1">
                          {job.application_status === "pending" && (
                            <>
                              <Button size="icon" variant="ghost" className="h-7 w-7 text-green-600 hover:bg-green-50"
                                onClick={() => updateStatus(job.id, "approved")} title="Approve">
                                <Check className="h-4 w-4" />
                              </Button>
                              <Button size="icon" variant="ghost" className="h-7 w-7 text-gray-400 hover:bg-gray-100"
                                onClick={() => updateStatus(job.id, "skipped")} title="Skip">
                                <X className="h-4 w-4" />
                              </Button>
                            </>
                          )}
                          <a href={job.url} target="_blank" rel="noopener noreferrer">
                            <Button size="icon" variant="ghost" className="h-7 w-7 text-blue-500 hover:bg-blue-50" title="Open job">
                              <ExternalLink className="h-4 w-4" />
                            </Button>
                          </a>
                        </div>
                      </td>
                    </tr>,
                    isExpanded && (job.description || showArtifacts) ? (
                      <tr key={`${job.id}-desc`} className="bg-blue-50/40">
                        <td colSpan={7} className="px-6 py-4">
                          <div className="space-y-4">
                            {showArtifacts && (
                              <div>
                                <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Application Files</p>
                                <div className="flex flex-wrap gap-2">
                                  {job.tailored_resume_path && (
                                    <a
                                      href={jobsApi.artifactUrl(job.id, "resume")}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                    >
                                      <Button variant="outline" size="sm" className="gap-2">
                                        <Download className="h-4 w-4" />
                                        Tailored Resume
                                      </Button>
                                    </a>
                                  )}
                                  {job.cover_letter_path && (
                                    <a
                                      href={jobsApi.artifactUrl(job.id, "cover-letter")}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                    >
                                      <Button variant="outline" size="sm" className="gap-2">
                                        <Download className="h-4 w-4" />
                                        Cover Letter
                                      </Button>
                                    </a>
                                  )}
                                </div>
                              </div>
                            )}

                            {job.description && (
                              <div>
                                <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Job Description</p>
                                <p className="text-sm text-gray-700 whitespace-pre-line leading-relaxed max-h-64 overflow-y-auto">
                                  {job.description}
                                </p>
                              </div>
                            )}
                          </div>
                        </td>
                      </tr>
                    ) : null,
                  ].filter(Boolean);
                })
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between text-sm text-gray-600">
            <span>Page {page} of {totalPages} ({data.total} total)</span>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" disabled={page === 1} onClick={() => setPage(p => p - 1)}>
                <ChevronLeft className="h-4 w-4" />
              </Button>
              <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => setPage(p => p + 1)}>
                <ChevronRight className="h-4 w-4" />
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
