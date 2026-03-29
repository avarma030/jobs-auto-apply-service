"use client";

import { useEffect, useRef, useState } from "react";
import { profile as profileApi } from "@/lib/api";
import type { Profile } from "@/lib/types";
import { TopBar } from "@/components/layout/TopBar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Save, Upload, Plus, Trash2 } from "lucide-react";

type ExtractionToast = { type: "success" | "error"; message: string } | null;

export default function ProfilePage() {
  const [profile, setProfile] = useState<Profile>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [extractionToast, setExtractionToast] = useState<ExtractionToast>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    profileApi.get().then((r) => setProfile(r.profile)).catch(() => {});
  }, []);

  function set<K extends keyof Profile>(key: K, value: Profile[K]) {
    setProfile((p) => ({ ...p, [key]: value }));
  }

  async function save() {
    setSaving(true);
    try {
      await profileApi.update(profile);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setSaving(false);
    }
  }

  async function uploadResume(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setExtractionToast(null);
    try {
      const res = await profileApi.uploadResume(file);
      // If the server auto-extracted a profile, pre-fill the form fields
      if (res.extracted_profile && res.profile_updated) {
        setProfile(res.extracted_profile as Profile);
        setExtractionToast({
          type: "success",
          message: `Profile auto-filled from resume — please review and click Save.`,
        });
      } else {
        set("resume_path" as any, `data/uploads/${file.name}`);
        if (res.extracted_profile === null || res.extracted_profile === undefined) {
          const msg = res.ai_extraction_enabled
            ? res.extraction_error
              ? `Resume uploaded. Profile auto-extraction failed: ${res.extraction_error} — please fill in your profile manually.`
              : "Resume uploaded. Profile auto-extraction returned no data — please fill in your profile manually."
            : "Resume uploaded, but auto-extraction is disabled (set ANTHROPIC_API_KEY to enable).";
          setExtractionToast({ type: "error", message: msg });
        }
      }
    } catch {
      setExtractionToast({ type: "error", message: "Upload failed — please try again." });
    } finally {
      setUploading(false);
    }
  }

  const skills = profile.skills ?? [];
  const experience = profile.work_experience ?? [];
  const education = profile.education ?? [];

  return (
    <div className="flex-1 overflow-y-auto">
      <TopBar
        title="Profile"
        subtitle="Your information used for job applications"
        action={
          <Button onClick={save} disabled={saving} size="sm">
            <Save className="h-4 w-4 mr-2" />
            {saved ? "Saved!" : saving ? "Saving…" : "Save Profile"}
          </Button>
        }
      />
      {/* Extraction Toast */}
      {extractionToast && (
        <div className={`mx-6 mt-4 px-4 py-3 rounded-lg flex items-center justify-between text-sm ${
          extractionToast.type === "success"
            ? "bg-green-50 text-green-800 border border-green-200"
            : "bg-yellow-50 text-yellow-800 border border-yellow-200"
        }`}>
          <span>{extractionToast.type === "success" ? "✓ " : "⚠ "}{extractionToast.message}</span>
          <button onClick={() => setExtractionToast(null)} className="ml-4 opacity-60 hover:opacity-100">×</button>
        </div>
      )}

      <div className="p-6 space-y-6 max-w-4xl">

        {/* Personal Info */}
        <Card>
          <CardHeader><CardTitle className="text-base">Personal Info</CardTitle></CardHeader>
          <CardContent className="grid grid-cols-2 gap-4">
            <Field label="First Name" value={profile.first_name ?? ""} onChange={(v) => set("first_name", v)} />
            <Field label="Last Name" value={profile.last_name ?? ""} onChange={(v) => set("last_name", v)} />
            <Field label="Email" value={profile.email ?? ""} onChange={(v) => set("email", v)} type="email" />
            <Field label="Phone" value={profile.phone ?? ""} onChange={(v) => set("phone", v)} />
            <Field label="City" value={profile.address?.city ?? ""} onChange={(v) => set("address", { ...profile.address, city: v })} />
            <Field label="State / Country" value={profile.address?.state ?? ""} onChange={(v) => set("address", { ...profile.address, state: v })} />
            <Field label="LinkedIn URL" value={profile.social_links?.linkedin ?? ""} onChange={(v) => set("social_links", { ...profile.social_links, linkedin: v })} className="col-span-2" />
          </CardContent>
        </Card>

        {/* Professional */}
        <Card>
          <CardHeader><CardTitle className="text-base">Professional Summary</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <Field label="Headline" value={profile.headline ?? ""} onChange={(v) => set("headline", v)} placeholder="e.g. Senior Software Engineer" />
            <div>
              <Label className="text-xs text-muted-foreground">Summary</Label>
              <Textarea
                value={profile.summary ?? ""}
                onChange={(e) => set("summary", e.target.value)}
                placeholder="Brief professional bio…"
                className="mt-1"
                rows={3}
              />
            </div>
            <Field label="Years of Experience" value={String(profile.years_of_experience ?? "")} onChange={(v) => set("years_of_experience", Number(v))} type="number" />
          </CardContent>
        </Card>

        {/* Skills */}
        <Card>
          <CardHeader><CardTitle className="text-base">Skills</CardTitle></CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-2 mb-3">
              {skills.map((s, i) => (
                <span key={i} className="flex items-center gap-1 bg-blue-50 text-blue-700 rounded-full px-3 py-1 text-sm">
                  {s}
                  <button onClick={() => set("skills", skills.filter((_, j) => j !== i))} className="text-blue-400 hover:text-blue-700">×</button>
                </span>
              ))}
            </div>
            <Input
              placeholder="Type a skill and press Enter"
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  const v = (e.target as HTMLInputElement).value.trim();
                  if (v && !skills.includes(v)) set("skills", [...skills, v]);
                  (e.target as HTMLInputElement).value = "";
                }
              }}
            />
          </CardContent>
        </Card>

        {/* Work Experience */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-base">Work Experience</CardTitle>
            <Button size="sm" variant="outline" onClick={() => set("work_experience", [...experience, { company: "", title: "", start_date: "" }])}>
              <Plus className="h-4 w-4 mr-1" /> Add
            </Button>
          </CardHeader>
          <CardContent className="space-y-6">
            {experience.map((exp, i) => (
              <div key={i} className="border rounded-lg p-4 space-y-3 relative">
                <button
                  className="absolute top-3 right-3 text-gray-400 hover:text-red-500"
                  onClick={() => set("work_experience", experience.filter((_, j) => j !== i))}
                >
                  <Trash2 className="h-4 w-4" />
                </button>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Company" value={exp.company} onChange={(v) => { const e2 = [...experience]; e2[i] = { ...exp, company: v }; set("work_experience", e2); }} />
                  <Field label="Title" value={exp.title} onChange={(v) => { const e2 = [...experience]; e2[i] = { ...exp, title: v }; set("work_experience", e2); }} />
                  <Field label="Start Date" value={exp.start_date} onChange={(v) => { const e2 = [...experience]; e2[i] = { ...exp, start_date: v }; set("work_experience", e2); }} placeholder="YYYY-MM" />
                  <Field label="End Date" value={exp.end_date ?? ""} onChange={(v) => { const e2 = [...experience]; e2[i] = { ...exp, end_date: v || undefined }; set("work_experience", e2); }} placeholder="YYYY-MM or leave blank" />
                </div>
              </div>
            ))}
            {experience.length === 0 && <p className="text-sm text-muted-foreground text-center py-4">No experience added yet.</p>}
          </CardContent>
        </Card>

        {/* Resume Upload */}
        <Card>
          <CardHeader><CardTitle className="text-base">Resume</CardTitle></CardHeader>
          <CardContent>
            <input ref={fileRef} type="file" accept=".pdf" className="hidden" onChange={uploadResume} />
            <div className="flex items-center gap-3">
              <Button variant="outline" onClick={() => fileRef.current?.click()} disabled={uploading}>
                <Upload className="h-4 w-4 mr-2" />
                {uploading ? "Uploading…" : "Upload PDF"}
              </Button>
              {profile.resume_path && (
                <span className="text-sm text-green-700 bg-green-50 rounded px-2 py-1">
                  ✓ Resume uploaded
                </span>
              )}
            </div>
          </CardContent>
        </Card>

        {/* Job Board Credentials */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Job Board Credentials</CardTitle>
            <p className="text-xs text-muted-foreground mt-1">
              Used to log in and apply to jobs on your behalf. Stored securely in your profile.
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <p className="text-sm font-medium text-gray-700 mb-2">LinkedIn</p>
              <div className="grid grid-cols-2 gap-3">
                <Field
                  label="Email / Username"
                  value={(profile.job_board_accounts as any)?.linkedin?.username ?? ""}
                  onChange={(v) => set("job_board_accounts", {
                    ...(profile.job_board_accounts ?? {}),
                    linkedin: { ...(((profile.job_board_accounts ?? {}) as any).linkedin ?? {}), username: v },
                  } as any)}
                  placeholder="you@example.com"
                />
                <Field
                  label="Password"
                  type="password"
                  value={(profile.job_board_accounts as any)?.linkedin?.password ?? ""}
                  onChange={(v) => set("job_board_accounts", {
                    ...(profile.job_board_accounts ?? {}),
                    linkedin: { ...(((profile.job_board_accounts ?? {}) as any).linkedin ?? {}), password: v },
                  } as any)}
                  placeholder="••••••••"
                />
              </div>
              <p className="text-xs text-muted-foreground mt-1">
                Enables authenticated scraping (bypasses CAPTCHA) and Easy Apply.
              </p>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function Field({
  label, value, onChange, type = "text", placeholder, className,
}: {
  label: string; value: string; onChange: (v: string) => void;
  type?: string; placeholder?: string; className?: string;
}) {
  return (
    <div className={className}>
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <Input type={type} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} className="mt-1" />
    </div>
  );
}
