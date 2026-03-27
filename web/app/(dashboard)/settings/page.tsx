"use client";

import { useEffect, useState } from "react";
import { settings as settingsApi, profile as profileApi } from "@/lib/api";
import type { Settings } from "@/lib/types";
import { TopBar } from "@/components/layout/TopBar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Save, Plus, Trash2 } from "lucide-react";

const ALL_BOARDS = ["linkedin", "indeed", "glassdoor", "ziprecruiter", "dice", "monster", "lever", "greenhouse", "workday"];
const WORK_MODES = ["remote", "hybrid", "onsite"];

const EMPTY: Settings = {
  auto_apply: true,
  max_applications_per_day: 50,
  easy_apply_only: false,
  headless_browser: true,
  dry_run: false,
  enabled_boards: ["linkedin"],
  preferred_work_modes: ["remote", "hybrid"],
  blacklisted_companies: [],
  request_delay_seconds: 2.0,
  custom_answers: {},
};

export default function SettingsPage() {
  const [s, setS] = useState<Settings>(EMPTY);
  const [linkedinUsername, setLinkedinUsername] = useState("");
  const [linkedinPassword, setLinkedinPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [newAnswer, setNewAnswer] = useState({ q: "", a: "" });
  const [newBlacklist, setNewBlacklist] = useState("");

  useEffect(() => {
    settingsApi.get().then(setS).catch(() => {});
    profileApi.get().then((r) => {
      const creds = r.profile?.job_board_accounts?.linkedin;
      if (creds?.username) setLinkedinUsername(creds.username);
    }).catch(() => {});
  }, []);

  async function save() {
    setSaving(true);
    try {
      await settingsApi.update(s);
      // Save LinkedIn credentials to profile
      if (linkedinUsername) {
        const p = await profileApi.get();
        await profileApi.update({
          ...p.profile,
          job_board_accounts: {
            ...(p.profile.job_board_accounts ?? {}),
            linkedin: { username: linkedinUsername, password: linkedinPassword || undefined },
          } as any,
        });
      }
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setSaving(false);
    }
  }

  function toggleBoard(board: string) {
    setS((prev) => ({
      ...prev,
      enabled_boards: prev.enabled_boards.includes(board)
        ? prev.enabled_boards.filter((b) => b !== board)
        : [...prev.enabled_boards, board],
    }));
  }

  function toggleMode(mode: string) {
    setS((prev) => ({
      ...prev,
      preferred_work_modes: prev.preferred_work_modes.includes(mode)
        ? prev.preferred_work_modes.filter((m) => m !== mode)
        : [...prev.preferred_work_modes, mode],
    }));
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <TopBar
        title="Settings"
        subtitle="Configure scraping and applying preferences"
        action={
          <Button onClick={save} disabled={saving} size="sm">
            <Save className="h-4 w-4 mr-2" />
            {saved ? "Saved!" : saving ? "Saving…" : "Save Settings"}
          </Button>
        }
      />
      <div className="p-6 space-y-6 max-w-3xl">

        {/* Auto-apply toggles */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Application Behaviour</CardTitle>
            <CardDescription>Control how AutoApply handles job applications</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <SwitchField
              label="Auto-apply"
              description="Automatically apply to approved jobs without manual review"
              checked={s.auto_apply}
              onChange={(v) => setS((p) => ({ ...p, auto_apply: v }))}
            />
            <SwitchField
              label="Easy Apply only"
              description="Skip jobs that require external applications"
              checked={s.easy_apply_only}
              onChange={(v) => setS((p) => ({ ...p, easy_apply_only: v }))}
            />
            <SwitchField
              label="Dry run mode"
              description="Scrape jobs but do not submit any applications"
              checked={s.dry_run}
              onChange={(v) => setS((p) => ({ ...p, dry_run: v }))}
            />
            <SwitchField
              label="Headless browser"
              description="Run the browser in the background (disable to see what's happening)"
              checked={s.headless_browser}
              onChange={(v) => setS((p) => ({ ...p, headless_browser: v }))}
            />
            <div>
              <Label className="text-sm font-medium">Max applications per day</Label>
              <Input
                type="number"
                value={s.max_applications_per_day}
                onChange={(e) => setS((p) => ({ ...p, max_applications_per_day: Number(e.target.value) }))}
                className="mt-1 w-32"
                min={1}
                max={200}
              />
            </div>
            <div>
              <Label className="text-sm font-medium">Request delay (seconds)</Label>
              <Input
                type="number"
                value={s.request_delay_seconds}
                onChange={(e) => setS((p) => ({ ...p, request_delay_seconds: Number(e.target.value) }))}
                className="mt-1 w-32"
                min={0.5}
                step={0.5}
              />
            </div>
          </CardContent>
        </Card>

        {/* Job Boards */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Enabled Job Boards</CardTitle>
            <CardDescription>Select which boards to scrape (LinkedIn is fully implemented)</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-2">
              {ALL_BOARDS.map((b) => (
                <button
                  key={b}
                  onClick={() => toggleBoard(b)}
                  className={`px-3 py-1.5 rounded-full text-sm font-medium border capitalize transition-colors ${
                    s.enabled_boards.includes(b)
                      ? "bg-blue-600 text-white border-blue-600"
                      : "border-gray-300 text-gray-600 hover:border-blue-400"
                  }`}
                >
                  {b}
                </button>
              ))}
            </div>
          </CardContent>
        </Card>

        {/* Work Modes */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Preferred Work Modes</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex gap-2">
              {WORK_MODES.map((m) => (
                <button
                  key={m}
                  onClick={() => toggleMode(m)}
                  className={`px-3 py-1.5 rounded-full text-sm font-medium border capitalize transition-colors ${
                    s.preferred_work_modes.includes(m)
                      ? "bg-green-600 text-white border-green-600"
                      : "border-gray-300 text-gray-600 hover:border-green-400"
                  }`}
                >
                  {m}
                </button>
              ))}
            </div>
          </CardContent>
        </Card>

        {/* LinkedIn Credentials */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">LinkedIn Credentials</CardTitle>
            <CardDescription>Required for Easy Apply automation</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div>
              <Label className="text-xs text-muted-foreground">Email / Username</Label>
              <Input value={linkedinUsername} onChange={(e) => setLinkedinUsername(e.target.value)} className="mt-1" type="email" />
            </div>
            <div>
              <Label className="text-xs text-muted-foreground">Password</Label>
              <Input value={linkedinPassword} onChange={(e) => setLinkedinPassword(e.target.value)} className="mt-1" type="password" placeholder="Leave blank to keep existing" />
            </div>
          </CardContent>
        </Card>

        {/* Blacklist */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Blacklisted Companies</CardTitle>
            <CardDescription>Jobs from these companies will be automatically skipped</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap gap-2">
              {s.blacklisted_companies.map((c, i) => (
                <span key={i} className="flex items-center gap-1 bg-red-50 text-red-700 rounded-full px-3 py-1 text-sm">
                  {c}
                  <button onClick={() => setS((p) => ({ ...p, blacklisted_companies: p.blacklisted_companies.filter((_, j) => j !== i) }))}>
                    ×
                  </button>
                </span>
              ))}
            </div>
            <div className="flex gap-2">
              <Input
                value={newBlacklist}
                onChange={(e) => setNewBlacklist(e.target.value)}
                placeholder="Company name"
                onKeyDown={(e) => {
                  if (e.key === "Enter" && newBlacklist.trim()) {
                    setS((p) => ({ ...p, blacklisted_companies: [...p.blacklisted_companies, newBlacklist.trim()] }));
                    setNewBlacklist("");
                  }
                }}
              />
              <Button variant="outline" size="sm" onClick={() => {
                if (newBlacklist.trim()) {
                  setS((p) => ({ ...p, blacklisted_companies: [...p.blacklisted_companies, newBlacklist.trim()] }));
                  setNewBlacklist("");
                }
              }}>Add</Button>
            </div>
          </CardContent>
        </Card>

        {/* Custom Answers */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Custom Q&A</CardTitle>
            <CardDescription>Pre-fill answers to common application questions</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {Object.entries(s.custom_answers).map(([q, a], i) => (
              <div key={i} className="flex items-center gap-2 bg-gray-50 rounded p-2">
                <div className="flex-1 text-sm">
                  <span className="font-medium">{q}</span>
                  <span className="text-muted-foreground mx-1">→</span>
                  <span>{a}</span>
                </div>
                <button
                  onClick={() => {
                    const updated = { ...s.custom_answers };
                    delete updated[q];
                    setS((p) => ({ ...p, custom_answers: updated }));
                  }}
                  className="text-gray-400 hover:text-red-500"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
            <div className="flex gap-2">
              <Input
                value={newAnswer.q}
                onChange={(e) => setNewAnswer((n) => ({ ...n, q: e.target.value }))}
                placeholder="Question…"
                className="flex-1"
              />
              <Input
                value={newAnswer.a}
                onChange={(e) => setNewAnswer((n) => ({ ...n, a: e.target.value }))}
                placeholder="Answer…"
                className="flex-1"
              />
              <Button variant="outline" size="sm" onClick={() => {
                if (newAnswer.q && newAnswer.a) {
                  setS((p) => ({ ...p, custom_answers: { ...p.custom_answers, [newAnswer.q]: newAnswer.a } }));
                  setNewAnswer({ q: "", a: "" });
                }
              }}>
                <Plus className="h-4 w-4" />
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function SwitchField({ label, description, checked, onChange }: {
  label: string; description: string; checked: boolean; onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between">
      <div>
        <p className="text-sm font-medium">{label}</p>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
      <Switch checked={checked} onCheckedChange={onChange} />
    </div>
  );
}
