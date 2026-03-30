"use client";

import { useCallback, useEffect, useState } from "react";
import { applications as appsApi } from "@/lib/api";
import type { Application, ApplicationsPage } from "@/lib/types";
import { TopBar } from "@/components/layout/TopBar";
import { StatusBadge } from "@/components/applications/StatusBadge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { formatDistanceToNow } from "date-fns";

const STATUSES = ["applied", "interviewing", "offered", "rejected", "failed"];

export default function ApplicationsPage() {
  const [data, setData] = useState<ApplicationsPage>({ items: [], total: 0, page: 1, page_size: 25 });
  const [statusFilter, setStatusFilter] = useState("");
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await appsApi.list({ status: statusFilter || undefined, page });
      setData(res);
    } finally {
      setLoading(false);
    }
  }, [statusFilter, page]);

  useEffect(() => { load(); }, [load]);

  async function changeStatus(id: number, newStatus: string) {
    await appsApi.update(id, newStatus);
    setData((prev) => ({
      ...prev,
      items: prev.items.map((a) => a.id === id ? { ...a, status: newStatus } : a),
    }));
  }

  const totalPages = Math.ceil(data.total / data.page_size);

  return (
    <div className="flex-1 overflow-y-auto">
      <TopBar title="Applications" subtitle={`${data.total} total`} />
      <div className="p-6 space-y-4">
        {/* Filter */}
        <div className="flex gap-2">
          {["", ...STATUSES].map((s) => (
            <button
              key={s}
              onClick={() => { setStatusFilter(s); setPage(1); }}
              className={`px-3 py-1.5 rounded-full text-xs font-medium border capitalize transition-colors ${
                statusFilter === s
                  ? "bg-blue-600 text-white border-blue-600"
                  : "border-gray-300 text-gray-600 hover:border-blue-400"
              }`}
            >
              {s || "All"}
            </button>
          ))}
        </div>

        {/* Table */}
        <div className="rounded-lg border bg-white overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-gray-700">Application</th>
                <th className="px-4 py-3 text-left font-medium text-gray-700 hidden md:table-cell">Attempted</th>
                <th className="px-4 py-3 text-left font-medium text-gray-700">Status</th>
                <th className="px-4 py-3 text-left font-medium text-gray-700">Update</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {loading ? (
                <tr><td colSpan={4} className="px-4 py-8 text-center text-muted-foreground">Loading…</td></tr>
              ) : data.items.length === 0 ? (
                <tr><td colSpan={4} className="px-4 py-8 text-center text-muted-foreground">No applications yet.</td></tr>
              ) : (
                data.items.map((app) => (
                  <tr key={app.id} className="hover:bg-gray-50">
                    <td className="px-4 py-3">
                      <p className="font-medium">{app.job_title || `Job #${app.job_id}`}</p>
                      {app.company_name && (
                        <p className="text-xs text-muted-foreground mt-0.5">{app.company_name}</p>
                      )}
                      {app.confirmation_id && (
                        <p className="text-xs text-muted-foreground">Ref: {app.confirmation_id}</p>
                      )}
                      {app.message && (
                        <p className="text-xs text-muted-foreground mt-0.5 line-clamp-1">{app.message}</p>
                      )}
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell text-gray-500 text-xs">
                      {formatDistanceToNow(new Date(app.attempted_at), { addSuffix: true })}
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={app.status} />
                    </td>
                    <td className="px-4 py-3">
                      <Select value={app.status} onValueChange={(v) => changeStatus(app.id, v)}>
                        <SelectTrigger className="w-36 h-8 text-xs">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {STATUSES.map((s) => (
                            <SelectItem key={s} value={s} className="text-xs capitalize">{s}</SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {totalPages > 1 && (
          <div className="flex items-center justify-between text-sm text-gray-600">
            <span>Page {page} of {totalPages}</span>
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
