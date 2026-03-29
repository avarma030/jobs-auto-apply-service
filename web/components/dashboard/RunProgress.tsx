"use client";

import { useEffect, useRef, useState } from "react";
import { jobs as jobsApi } from "@/lib/api";
import { useRunStream } from "@/lib/sse";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Play, ChevronDown, ChevronUp } from "lucide-react";

interface Props {
  onComplete?: () => void;
}

const WORK_MODES = ["remote", "hybrid", "onsite"];
const JOB_TYPES = [
  { value: "full_time", label: "Full-time" },
  { value: "part_time", label: "Part-time" },
  { value: "contract", label: "Contract" },
  { value: "internship", label: "Internship" },
];
const EXP_LEVELS = [
  { value: "entry", label: "Entry" },
  { value: "mid", label: "Mid" },
  { value: "senior", label: "Senior" },
  { value: "lead", label: "Lead" },
  { value: "executive", label: "Executive" },
];
const AGE_OPTIONS = [
  { value: 1, label: "24h" },
  { value: 3, label: "3 days" },
  { value: 7, label: "7 days" },
  { value: 14, label: "14 days" },
  { value: 30, label: "30 days" },
];
const ALL_BOARDS = ["linkedin", "indeed", "glassdoor", "ziprecruiter", "dice", "monster"];

function ToggleChip({
  active, onClick, children,
}: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-2.5 py-1 rounded-full text-xs font-medium border transition-colors ${
        active
          ? "bg-blue-600 text-white border-blue-600"
          : "border-gray-300 text-gray-600 hover:border-blue-400 bg-white"
      }`}
    >
      {children}
    </button>
  );
}

export function RunProgress({ onComplete }: Props) {
  const [runId, setRunId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  const [keywords, setKeywords] = useState("software engineer");
  const [location, setLocation] = useState("");
  const [workModes, setWorkModes] = useState<string[]>([]);
  const [jobTypes, setJobTypes] = useState<string[]>([]);
  const [expLevels, setExpLevels] = useState<string[]>([]);
  const [easyApplyOnly, setEasyApplyOnly] = useState(false);
  const [maxAgeDays, setMaxAgeDays] = useState(7);
  const [maxJobs, setMaxJobs] = useState<string>("");  // empty = no limit
  const [minMatchScore, setMinMatchScore] = useState<number>(75);
  const [boards, setBoards] = useState<string[]>(["linkedin"]);

  const { latest, messages, done } = useRunStream(runId);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [messages]);

  useEffect(() => {
    if (done && onComplete) {
      const t = setTimeout(onComplete, 1500);
      return () => clearTimeout(t);
    }
  }, [done, onComplete]);

  function toggle(arr: string[], val: string, set: (v: string[]) => void) {
    set(arr.includes(val) ? arr.filter((x) => x !== val) : [...arr, val]);
  }

  async function startRun() {
    if (boards.length === 0) { alert("Select at least one job board."); return; }
    setLoading(true);
    try {
      const res = await jobsApi.scrape({
        keywords: keywords.split(",").map((k) => k.trim()).filter(Boolean),
        location: location || undefined,
        work_modes: workModes,
        job_types: jobTypes,
        experience_levels: expLevels,
        easy_apply_only: easyApplyOnly,
        boards,
        max_age_days: maxAgeDays,
        max_jobs: maxJobs ? parseInt(maxJobs, 10) : undefined,
        min_match_score: minMatchScore,
      });
      setRunId(res.run_id);
    } catch (err: any) {
      alert(err.message);
    } finally {
      setLoading(false);
    }
  }

  const isRunning = !!runId && !done;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Start New Scrape</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {!isRunning && (
          <>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="text-xs text-muted-foreground">Keywords (comma-separated)</Label>
                <Input value={keywords} onChange={(e) => setKeywords(e.target.value)}
                  placeholder="software engineer, python dev" className="mt-1" />
              </div>
              <div>
                <Label className="text-xs text-muted-foreground">Location (optional)</Label>
                <Input value={location} onChange={(e) => setLocation(e.target.value)}
                  placeholder="New York, NY" className="mt-1" />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="text-xs text-muted-foreground">Max Jobs to Scrape (blank = all)</Label>
                <Input
                  type="number"
                  min="1"
                  value={maxJobs}
                  onChange={(e) => setMaxJobs(e.target.value)}
                  placeholder="e.g. 50"
                  className="mt-1"
                />
              </div>
              <div>
                <Label className="text-xs text-muted-foreground">Min Match % (0–100)</Label>
                <Input
                  type="number"
                  min="0"
                  max="100"
                  value={minMatchScore}
                  onChange={(e) => setMinMatchScore(Number(e.target.value))}
                  placeholder="75"
                  className="mt-1"
                />
              </div>
            </div>

            <div>
              <Label className="text-xs text-muted-foreground">Work Mode</Label>
              <div className="flex gap-1.5 mt-1 flex-wrap">
                {WORK_MODES.map((m) => (
                  <ToggleChip key={m} active={workModes.includes(m)}
                    onClick={() => toggle(workModes, m, setWorkModes)}>
                    {m.charAt(0).toUpperCase() + m.slice(1)}
                  </ToggleChip>
                ))}
              </div>
            </div>

            <div>
              <Label className="text-xs text-muted-foreground">Experience Level</Label>
              <div className="flex gap-1.5 mt-1 flex-wrap">
                {EXP_LEVELS.map(({ value, label }) => (
                  <ToggleChip key={value} active={expLevels.includes(value)}
                    onClick={() => toggle(expLevels, value, setExpLevels)}>
                    {label}
                  </ToggleChip>
                ))}
              </div>
            </div>

            <button type="button" onClick={() => setShowAdvanced((v) => !v)}
              className="flex items-center gap-1 text-xs text-blue-600 hover:text-blue-800">
              {showAdvanced ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
              {showAdvanced ? "Hide advanced filters" : "More filters (job type, date, easy apply, boards)"}
            </button>

            {showAdvanced && (
              <div className="space-y-3 border rounded-lg p-3 bg-gray-50">
                <div>
                  <Label className="text-xs text-muted-foreground">Job Type</Label>
                  <div className="flex gap-1.5 mt-1 flex-wrap">
                    {JOB_TYPES.map(({ value, label }) => (
                      <ToggleChip key={value} active={jobTypes.includes(value)}
                        onClick={() => toggle(jobTypes, value, setJobTypes)}>
                        {label}
                      </ToggleChip>
                    ))}
                  </div>
                </div>

                <div>
                  <Label className="text-xs text-muted-foreground">Posted Within</Label>
                  <div className="flex gap-1.5 mt-1 flex-wrap">
                    {AGE_OPTIONS.map(({ value, label }) => (
                      <ToggleChip key={value} active={maxAgeDays === value}
                        onClick={() => setMaxAgeDays(value)}>
                        {label}
                      </ToggleChip>
                    ))}
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  <input id="easy-apply" type="checkbox" checked={easyApplyOnly}
                    onChange={(e) => setEasyApplyOnly(e.target.checked)}
                    className="h-4 w-4 rounded border-gray-300 text-blue-600 cursor-pointer" />
                  <Label htmlFor="easy-apply" className="text-xs cursor-pointer">
                    Easy Apply only (LinkedIn one-click apply)
                  </Label>
                </div>

                <div>
                  <Label className="text-xs text-muted-foreground">Job Boards</Label>
                  <div className="flex gap-1.5 mt-1 flex-wrap">
                    {ALL_BOARDS.map((b) => (
                      <ToggleChip key={b} active={boards.includes(b)}
                        onClick={() => toggle(boards, b, setBoards)}>
                        {b.charAt(0).toUpperCase() + b.slice(1)}
                      </ToggleChip>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </>
        )}

        {(isRunning || (done && latest)) && (
          <div className="rounded-lg bg-gray-50 border p-3 space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">
                {done ? "Pipeline complete" : "Pipeline running…"}
              </span>
              <Badge variant={done ? (latest?.event === "failed" ? "destructive" : "success") : "info"}>
                {done ? (latest?.event === "failed" ? "Failed" : "Done") : "Running"}
              </Badge>
            </div>
            <div className="grid grid-cols-2 gap-2 text-sm">
              <div>
                <span className="text-muted-foreground">Scraped: </span>
                <span className="font-semibold">{latest?.jobs_found ?? 0}</span>
              </div>
              <div>
                <span className="text-muted-foreground">Applied: </span>
                <span className="font-semibold">{latest?.jobs_applied ?? 0}</span>
              </div>
            </div>
            {messages.length > 0 && (
              <div ref={logRef}
                className="max-h-48 overflow-y-auto border-t pt-2 space-y-0.5">
                {messages.map((m, i) => {
                  const isError = m.includes("⚠️") || m.includes("❌") || m.includes("failed") || m.includes("ERROR");
                  const isSuccess = m.includes("✅") || m.includes("🎉") || m.includes("🏁");
                  return (
                    <p key={i} className={`text-xs font-mono leading-relaxed ${
                      isError ? "text-red-600" : isSuccess ? "text-green-700" : "text-gray-600"
                    }`}>{m}</p>
                  );
                })}
              </div>
            )}
            {latest?.error_message && (
              <p className="text-xs text-red-600">{latest.error_message}</p>
            )}
          </div>
        )}

        <Button onClick={startRun} disabled={loading || isRunning} className="w-full">
          <Play className="h-4 w-4 mr-2" />
          {isRunning ? "Running…" : loading ? "Starting…" : "Start Scraping"}
        </Button>

        {done && !loading && (
          <Button variant="outline" className="w-full" onClick={() => {
            setRunId(null); setLoading(false);
          }}>
            Start Another Run
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
