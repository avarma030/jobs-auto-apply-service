"use client";

import { useState } from "react";
import { jobs as jobsApi } from "@/lib/api";
import { useRunStream } from "@/lib/sse";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Play, Square } from "lucide-react";

interface Props {
  onComplete?: () => void;
}

export function RunProgress({ onComplete }: Props) {
  const [runId, setRunId] = useState<string | null>(null);
  const [keywords, setKeywords] = useState("software engineer");
  const [location, setLocation] = useState("");
  const [loading, setLoading] = useState(false);

  const { latest, done } = useRunStream(runId);

  async function startRun() {
    setLoading(true);
    try {
      const res = await jobsApi.scrape({
        keywords: keywords.split(",").map((k) => k.trim()).filter(Boolean),
        location: location || undefined,
        boards: ["linkedin"],
        max_age_days: 7,
      });
      setRunId(res.run_id);
    } catch (err: any) {
      alert(err.message);
    } finally {
      setLoading(false);
    }
  }

  if (done && onComplete) {
    setTimeout(onComplete, 1000);
  }

  const isRunning = !!runId && !done;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Start New Scrape</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {!isRunning && (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="text-xs">Keywords (comma-separated)</Label>
              <Input
                value={keywords}
                onChange={(e) => setKeywords(e.target.value)}
                placeholder="software engineer, python developer"
                className="mt-1"
              />
            </div>
            <div>
              <Label className="text-xs">Location (optional)</Label>
              <Input
                value={location}
                onChange={(e) => setLocation(e.target.value)}
                placeholder="New York, NY"
                className="mt-1"
              />
            </div>
          </div>
        )}

        {latest && (
          <div className="rounded-lg bg-gray-50 border p-3">
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium">Run in progress…</span>
              <Badge variant={done ? "success" : "info"}>
                {done ? "Complete" : "Running"}
              </Badge>
            </div>
            <div className="grid grid-cols-2 gap-2 text-sm">
              <div>
                <span className="text-muted-foreground">Jobs found: </span>
                <span className="font-semibold">{latest.jobs_found}</span>
              </div>
              <div>
                <span className="text-muted-foreground">Applied: </span>
                <span className="font-semibold">{latest.jobs_applied}</span>
              </div>
            </div>
            {latest.error_message && (
              <p className="text-xs text-red-600 mt-2">{latest.error_message}</p>
            )}
          </div>
        )}

        <Button
          onClick={startRun}
          disabled={loading || isRunning}
          className="w-full"
        >
          <Play className="h-4 w-4 mr-2" />
          {isRunning ? "Running…" : loading ? "Starting…" : "Start Scraping"}
        </Button>
      </CardContent>
    </Card>
  );
}
