"use client";

import { useEffect, useRef, useState } from "react";
import { jobs as jobsApi } from "@/lib/api";
import { useRunStream } from "@/lib/sse";
import type { SavedSearchState } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock3,
  FileText,
  Play,
  Search,
  Send,
} from "lucide-react";

interface Props {
  onComplete?: () => void;
  savedSearchStateOverride?: SavedSearchState | null;
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
  { value: 1, label: "1h" },
  { value: 3, label: "3h" },
  { value: 24, label: "24h" },
  { value: 72, label: "3 days" },
  { value: 168, label: "7 days" },
  { value: 336, label: "14 days" },
  { value: 720, label: "30 days" },
];
const ALL_BOARDS = ["linkedin", "indeed", "glassdoor", "ziprecruiter", "dice", "monster"];

type LogTone = "success" | "warning" | "error" | "info";

type ParsedLog = {
  timestamp: string | null;
  tags: string[];
  text: string;
  tone: LogTone;
};

function parseLogMessage(raw: string): ParsedLog {
  let remaining = raw.trim();
  let timestamp: string | null = null;
  const timeMatch = remaining.match(/^\[(\d{2}:\d{2}:\d{2})\]\s*/);
  if (timeMatch) {
    timestamp = timeMatch[1];
    remaining = remaining.slice(timeMatch[0].length);
  }

  const tags: string[] = [];
  while (remaining.startsWith("[")) {
    const tagMatch = remaining.match(/^\[([^\]]+)\]\s*/);
    if (!tagMatch) break;
    tags.push(tagMatch[1]);
    remaining = remaining.slice(tagMatch[0].length);
  }

  const lower = raw.toLowerCase();
  let tone: LogTone = "info";
  if (
    lower.includes("failed") ||
    lower.includes("error") ||
    lower.includes("could not") ||
    lower.includes("validation")
  ) {
    tone = "error";
  } else if (
    lower.includes("warning") ||
    lower.includes("unanswered") ||
    lower.includes("stale") ||
    lower.includes("skipped")
  ) {
    tone = "warning";
  } else if (
    lower.includes("applied successfully") ||
    lower.includes("application submitted") ||
    lower.includes("complete") ||
    lower.includes("[saved]") ||
    lower.includes("learned")
  ) {
    tone = "success";
  }

  return {
    timestamp,
    tags,
    text: remaining || raw,
    tone,
  };
}

function formatScheduleTime(value: string | null | undefined) {
  if (!value) return null;
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return null;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(dt);
}

function logToneClasses(tone: LogTone) {
  switch (tone) {
    case "success":
      return {
        border: "border-emerald-200",
        iconWrap: "bg-emerald-100 text-emerald-700",
        text: "text-emerald-900",
        badge: "success" as const,
      };
    case "warning":
      return {
        border: "border-amber-200",
        iconWrap: "bg-amber-100 text-amber-700",
        text: "text-amber-900",
        badge: "warning" as const,
      };
    case "error":
      return {
        border: "border-rose-200",
        iconWrap: "bg-rose-100 text-rose-700",
        text: "text-rose-900",
        badge: "destructive" as const,
      };
    default:
      return {
        border: "border-slate-200",
        iconWrap: "bg-slate-100 text-slate-700",
        text: "text-slate-900",
        badge: "info" as const,
      };
  }
}

function logIcon(entry: ParsedLog) {
  const joinedTags = entry.tags.join(" ").toLowerCase();
  if (joinedTags.includes("question")) return Bot;
  if (joinedTags.includes("resume")) return FileText;
  if (joinedTags.includes("submit")) return Send;
  if (joinedTags.includes("apply") || joinedTags.includes("step")) return Search;
  if (entry.tone === "success") return CheckCircle2;
  if (entry.tone === "warning" || entry.tone === "error") return AlertTriangle;
  return Clock3;
}

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

export function RunProgress({ onComplete, savedSearchStateOverride }: Props) {
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
  const [maxAgeHours, setMaxAgeHours] = useState(168);
  const [maxJobs, setMaxJobs] = useState<string>("");  // empty = no limit
  const [tailorDocuments, setTailorDocuments] = useState(false);
  const [minMatchScore, setMinMatchScore] = useState<number>(75);
  const [boards, setBoards] = useState<string[]>(["linkedin"]);
  const [savedSearchEnabled, setSavedSearchEnabled] = useState(false);
  const [savedSearchIntervalHours, setSavedSearchIntervalHours] = useState<1 | 3>(3);
  const [savedSearchState, setSavedSearchState] = useState<SavedSearchState | null>(null);

  const { latest, messages, done } = useRunStream(runId);
  const trimmedLocation = location.trim();

  useEffect(() => {
    jobsApi.getSavedSearch()
      .then((saved) => {
        setSavedSearchState(saved);
        setSavedSearchEnabled(saved.enabled);
        setSavedSearchIntervalHours(saved.interval_hours);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (savedSearchStateOverride === undefined) return;
    setSavedSearchState(savedSearchStateOverride);
    if (savedSearchStateOverride) {
      setSavedSearchEnabled(savedSearchStateOverride.enabled);
      setSavedSearchIntervalHours(savedSearchStateOverride.interval_hours);
    } else {
      setSavedSearchEnabled(false);
      setSavedSearchIntervalHours(3);
    }
  }, [savedSearchStateOverride]);

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
    if (!trimmedLocation) { alert("Location is required."); return; }
    setLoading(true);
    try {
      const res = await jobsApi.scrape({
        keywords: keywords.split(",").map((k) => k.trim()).filter(Boolean),
        location: trimmedLocation,
        work_modes: workModes,
        job_types: jobTypes,
        experience_levels: expLevels,
        easy_apply_only: easyApplyOnly,
        boards,
        max_age_hours: maxAgeHours,
        max_jobs: maxJobs ? parseInt(maxJobs, 10) : undefined,
        tailor_documents: tailorDocuments,
        min_match_score: tailorDocuments ? minMatchScore : undefined,
        save_search: true,
        saved_search_enabled: savedSearchEnabled,
        saved_search_interval_hours: savedSearchEnabled ? savedSearchIntervalHours : undefined,
      });
      const startedAt = new Date().toISOString();
      setSavedSearchState({
        enabled: savedSearchEnabled,
        interval_hours: savedSearchIntervalHours,
        criteria: {
          keywords: keywords.split(",").map((k) => k.trim()).filter(Boolean),
          location: trimmedLocation,
          work_modes: workModes,
          job_types: jobTypes,
          experience_levels: expLevels,
          easy_apply_only: easyApplyOnly,
          boards,
          max_age_hours: maxAgeHours,
          max_jobs: maxJobs ? parseInt(maxJobs, 10) : undefined,
          tailor_documents: tailorDocuments,
          min_match_score: tailorDocuments ? minMatchScore : undefined,
        },
        last_triggered_at: savedSearchEnabled ? startedAt : savedSearchState?.last_triggered_at ?? null,
        last_run_id: res.run_id,
        next_trigger_at: savedSearchEnabled
          ? new Date(Date.now() + savedSearchIntervalHours * 60 * 60 * 1000).toISOString()
          : null,
        run_count: savedSearchState?.run_count ?? 0,
        runs: savedSearchState?.runs ?? [],
      });
      setRunId(res.run_id);
    } catch (err: any) {
      alert(err.message);
    } finally {
      setLoading(false);
    }
  }

  const isRunning = !!runId && !done;
  const canStartRun = !loading && !isRunning && boards.length > 0 && trimmedLocation.length > 0;
  const parsedMessages = messages.map(parseLogMessage);

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
                <Label className="text-xs text-muted-foreground">Location</Label>
                <Input
                  required
                  value={location}
                  onChange={(e) => setLocation(e.target.value)}
                  placeholder="New York, NY"
                  className="mt-1"
                />
                <p className="mt-1 text-xs text-muted-foreground">
                  Enter the location you want this search run to target.
                </p>
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
                  disabled={!tailorDocuments}
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
              <Label className="text-xs text-muted-foreground">Application Mode</Label>
              <div className="mt-1 flex flex-wrap gap-1.5">
                <ToggleChip active={!tailorDocuments} onClick={() => setTailorDocuments(false)}>
                  Use Uploaded Resume
                </ToggleChip>
                <ToggleChip active={tailorDocuments} onClick={() => setTailorDocuments(true)}>
                  Tailor Before Apply
                </ToggleChip>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                {tailorDocuments
                  ? "Scores jobs, tailors your resume and cover letter, then applies to qualified matches."
                  : "Applies directly to the scraped jobs using your uploaded resume as-is."}
              </p>
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
                      <ToggleChip key={value} active={maxAgeHours === value}
                        onClick={() => setMaxAgeHours(value)}>
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

            <div className="rounded-lg border p-3 bg-gray-50 space-y-3">
              <div>
                <Label className="text-xs text-muted-foreground">Recurring Search</Label>
                <p className="mt-1 text-xs text-muted-foreground">
                  Save this search and let the backend rerun the same auto-apply pipeline for newly posted jobs.
                </p>
              </div>
              <div className="flex items-center gap-2">
                <input
                  id="saved-search-enabled"
                  type="checkbox"
                  checked={savedSearchEnabled}
                  onChange={(e) => setSavedSearchEnabled(e.target.checked)}
                  className="h-4 w-4 rounded border-gray-300 text-blue-600 cursor-pointer"
                />
                <Label htmlFor="saved-search-enabled" className="text-xs cursor-pointer">
                  Re-run this saved search automatically
                </Label>
              </div>
              {savedSearchEnabled && (
                <div>
                  <Label className="text-xs text-muted-foreground">Run Every</Label>
                  <div className="mt-1 flex gap-1.5 flex-wrap">
                    {[1, 3].map((hours) => (
                      <ToggleChip
                        key={hours}
                        active={savedSearchIntervalHours === hours}
                        onClick={() => setSavedSearchIntervalHours(hours as 1 | 3)}
                      >
                        {hours}h
                      </ToggleChip>
                    ))}
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">
                    For the safest coverage, keep the repeat interval aligned with your Posted Within window.
                  </p>
                </div>
              )}
              {savedSearchState?.enabled && (
                <p className="text-xs text-muted-foreground">
                  {savedSearchState.next_trigger_at
                    ? `Next automatic run: ${formatScheduleTime(savedSearchState.next_trigger_at)}`
                    : "This search is saved and ready for automatic reruns."}
                </p>
              )}
            </div>
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
            {parsedMessages.length > 0 && (
              <div
                ref={logRef}
                className="max-h-72 overflow-y-auto border-t pt-3 space-y-2"
              >
                {parsedMessages.map((entry, i) => {
                  const classes = logToneClasses(entry.tone);
                  const Icon = logIcon(entry);
                  return (
                    <div
                      key={`${entry.text}-${i}`}
                      className={`rounded-xl border bg-white/90 p-3 shadow-sm ${classes.border}`}
                    >
                      <div className="flex items-start gap-3">
                        <div className={`mt-0.5 rounded-full p-1.5 ${classes.iconWrap}`}>
                          <Icon className="h-3.5 w-3.5" />
                        </div>
                        <div className="min-w-0 flex-1 space-y-2">
                          <div className="flex flex-wrap items-center gap-1.5">
                            {entry.timestamp && (
                              <Badge variant="outline" className="bg-slate-50 text-slate-600">
                                {entry.timestamp}
                              </Badge>
                            )}
                            {entry.tags.map((tag) => (
                              <Badge key={`${tag}-${i}`} variant={classes.badge}>
                                {tag}
                              </Badge>
                            ))}
                          </div>
                          <p className={`text-sm leading-relaxed ${classes.text}`}>
                            {entry.text}
                          </p>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
            {false && messages.length > 0 && (
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

        <Button onClick={startRun} disabled={!canStartRun} className="w-full">
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
