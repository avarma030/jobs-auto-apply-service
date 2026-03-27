"use client";

import { useCallback, useEffect, useState } from "react";
import { jobs as jobsApi } from "@/lib/api";
import type { Job, JobsPage } from "@/lib/types";
import { TopBar } from "@/components/layout/TopBar";
import { StatusBadge } from "@/components/applications/StatusBadge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { ExternalLink, Check, X, ChevronLeft, ChevronRight } from "lucide-react";
import { formatDistanceToNow } from "date-fns";

const STATUS_OPTIONS = ["", "pending", "approved", "applied", "skipped", "failed"];

function MatchScoreBadge({ score }: { score?: number | null }) {
  if (score == null) return <span className="text-gray-400 text-xs">–</span>;
  const pct = Math.round(score);
  const color =
    pct >= 90 ? "bg-green-100 text-green-800 border-green-300"
    : pct >= 75 ? "bg-yellow-100 text-yellow-800 border-yellow-300"
    : "bg-red-100 text-red-700 border-red-300";
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold border ${color}`}>
      {pct}% match
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

export default function JobsPage() {
  const [data, setData] = useState<JobsPage>({ items: [], total: 0, page: 1, page_size: 25 });
  const [status, setStatus] = useState("");
  const [keywords, setKeywords] = useState("");
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);

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

  const totalPages = Math.ceil(data.total / data.page_size);

  return (
    <div className="flex-1 overflow-y-auto">
      <TopBar
        title="Job Board"
        subtitle={`${data.total} jobs found`}
      />
      <div className="p-6 space-y-4">
        {/* Filters */}
        <div className="flex gap-3 flex-wrap">
          <Input
            placeholder="Search title…"
            value={keywords}
            onChange={(e) => { setKeywords(e.target.value); setPage(1); }}
            className="w-56"
          />
          <div className="flex gap-2">
            {STATUS_OPTIONS.map((s) => (
              <button
                key={s}
                onClick={() => { setStatus(s); setPage(1); }}
                className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
                  status === s
                    ? "bg-blue-600 text-white border-blue-600"
                    : "border-gray-300 text-gray-600 hover:border-blue-400"
                }`}
              >
                {s || "All"}
              </button>
            ))}
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
                <tr><td colSpan={7} className="px-4 py-8 text-center text-muted-foreground">No jobs found. Start a scrape from the Dashboard.</td></tr>
              ) : (
                data.items.map((job) => (
                  <tr key={job.id} className="hover:bg-gray-50">
                    <td className="px-4 py-3">
                      <div className="flex items-start gap-2">
                        <div>
                          <p className="font-medium text-gray-900">{job.title}</p>
                          <p className="text-gray-500 text-xs">{job.company} · {job.location ?? "Remote"}</p>
                          <div className="flex gap-1.5 mt-1">
                            {job.easy_apply && <Badge variant="info" className="text-xs py-0">Easy Apply</Badge>}
                            {job.work_mode && <Badge variant="outline" className="text-xs py-0 capitalize">{job.work_mode}</Badge>}
                            {job.tailored_resume_path && <Badge variant="outline" className="text-xs py-0 text-purple-700 border-purple-300">Tailored</Badge>}
                          </div>
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
                    <td className="px-4 py-3">
                      <div className="flex gap-1">
                        {job.application_status === "pending" && (
                          <>
                            <Button size="icon" variant="ghost" className="h-7 w-7 text-green-600 hover:bg-green-50"
                              onClick={() => updateStatus(job.id, "approved")}>
                              <Check className="h-4 w-4" />
                            </Button>
                            <Button size="icon" variant="ghost" className="h-7 w-7 text-gray-400 hover:bg-gray-100"
                              onClick={() => updateStatus(job.id, "skipped")}>
                              <X className="h-4 w-4" />
                            </Button>
                          </>
                        )}
                        <a href={job.url} target="_blank" rel="noopener noreferrer">
                          <Button size="icon" variant="ghost" className="h-7 w-7 text-blue-500 hover:bg-blue-50">
                            <ExternalLink className="h-4 w-4" />
                          </Button>
                        </a>
                      </div>
                    </td>
                  </tr>
                ))
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
