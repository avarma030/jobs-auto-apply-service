"use client";

import { useCallback, useEffect, useState } from "react";
import { statsApi, runs as runsApi } from "@/lib/api";
import type { Run, Stats } from "@/lib/types";
import { TopBar } from "@/components/layout/TopBar";
import { StatsCards } from "@/components/dashboard/StatsCards";
import { RunProgress } from "@/components/dashboard/RunProgress";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { formatDistanceToNow } from "date-fns";

const EMPTY_STATS: Stats = {
  total_scraped: 0, total_applied: 0, total_skipped: 0, total_failed: 0,
  total_interviewing: 0, total_offered: 0, total_rejected: 0,
  success_rate: 0, this_week_applied: 0, by_board: {},
};

export default function DashboardPage() {
  const [stats, setStats] = useState<Stats>(EMPTY_STATS);
  const [recentRuns, setRecentRuns] = useState<Run[]>([]);

  const reload = useCallback(async () => {
    const [s, r] = await Promise.all([
      statsApi.get().catch(() => EMPTY_STATS),
      runsApi.list().catch(() => []),
    ]);
    setStats(s);
    setRecentRuns(r.slice(0, 5));
  }, []);

  useEffect(() => { reload(); }, [reload]);

  return (
    <div className="flex-1 overflow-y-auto">
      <TopBar title="Dashboard" subtitle="Your application overview" />
      <div className="p-6 space-y-6">
        <StatsCards stats={stats} />

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <RunProgress onComplete={reload} />

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
                    <div key={run.id} className="flex items-center justify-between text-sm">
                      <div>
                        <p className="font-medium">{run.keywords ?? "No keywords"}</p>
                        <p className="text-xs text-muted-foreground">
                          {formatDistanceToNow(new Date(run.started_at), { addSuffix: true })} · {run.jobs_found} jobs
                        </p>
                      </div>
                      <Badge
                        variant={
                          run.status === "done" ? "success" :
                          run.status === "failed" ? "destructive" :
                          run.status === "running" ? "info" : "secondary"
                        }
                      >
                        {run.status}
                      </Badge>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Board breakdown */}
        {Object.keys(stats.by_board).length > 0 && (
          <Card>
            <CardHeader><CardTitle className="text-base">Jobs by Board</CardTitle></CardHeader>
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
